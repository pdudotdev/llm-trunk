import json
import re
import time
from collections import OrderedDict
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from policy.decide import INPUT_CAP, PRICE_CLIFF, STALE_HASH, UNKNOWN_SKILL, decide
from policy.hash import sha256_hex


def _log(message: str) -> None:
    # The host process (LiteLLM) owns its own logging config, which we don't
    # control and can't rely on: its handlers accept WARNING but silently
    # drop INFO regardless of our own logger's level. print() sidesteps that
    # entirely -- these lines only need to land in `docker compose logs`.
    print(message, flush=True)


CATALOG_PATH = "/app/catalog.yaml"
MAX_STICKY_SESSIONS = 1024
MAX_FRONTMATTER = 4096

# Bounds how long a route can ride on one invocation. Without this, any
# later message in the same conversation -- however unrelated -- keeps
# inheriting the tagged route forever: invoke a high-tier skill once, then
# use that conversation for anything else at that skill's price. Two
# independent limits, whichever elapses first evicts the entry back to "no
# sticky route" (decide() then falls through to untagged, same as if the
# skill had never been invoked).
STICKY_IDLE_TIMEOUT_SECONDS = 600  # expires if unused this long
STICKY_MAX_AGE_SECONDS = 1800  # hard cap since the last real invocation --
# not extended by continued sticky-only use, so periodically pinging the
# conversation can't keep a route alive past this.

# The bounded quantifier matters: with an unbounded `.*?` a failed match
# backtracks to end-of-text at every "---" in the payload (markdown rules,
# pasted diffs), which is O(text * fences) on the per-request hot path.
FRONTMATTER_RE = re.compile(rf"---\n(.{{0,{MAX_FRONTMATTER}}}?)\n---\n\n?", re.DOTALL)

# Claude Code's actual wire format for a slash-invoked project skill: the
# YAML frontmatter is stripped client-side and replaced with this tag plus a
# "Base directory" line, immediately followed by the body verbatim. Confirmed
# by inspecting a real request -- the raw "---\nname: ...\n---\n" frontmatter
# never appears on the wire for a skill invoked this way.
COMMAND_NAME_RE = re.compile(r"<command-name>/(?P<name>[\w-]+)</command-name>")
BASE_DIR_RE = re.compile(r"Base directory for this skill: [^\n]*\n\n")

DENY_STATUS = {
    UNKNOWN_SKILL: 403,
    STALE_HASH: 403,
    INPUT_CAP: 413,
    PRICE_CLIFF: 413,
}


def _load_catalog() -> dict:
    with open(CATALOG_PATH) as f:
        return yaml.safe_load(f)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return ""


def _input_messages(data: dict) -> list[dict]:
    """Single definition of what counts as request input: system, then messages."""
    messages = []
    system_text = _message_text(data.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.extend(data.get("messages", []))
    return messages


def _candidate_texts(messages: list[dict]) -> list[str]:
    return [_message_text(message.get("content")) for message in messages]


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return value
    return None


def _hash_body(text: str, start: int, row: dict, name: str) -> tuple[str, str] | None:
    length = row.get("bytes")
    if length is None:
        _log(f"llm-trunk: catalog row {name!r} has no 'bytes', cannot verify its hash")
        return None
    # The catalog records the canonical body's byte length so the exact
    # region can be isolated from whatever prompt text surrounds it; hashing
    # to end-of-message would never match the stored digest.
    raw = text[start : start + length].encode()[:length]
    return name, sha256_hex(raw)


def _extract_from_command(text: str, catalog: dict) -> tuple[str, str] | None:
    match = COMMAND_NAME_RE.search(text)
    if match is None:
        return None
    name = match.group("name")
    row = catalog.get("skills", {}).get(name)
    if row is None:
        return None
    base_dir_match = BASE_DIR_RE.search(text, match.end())
    if base_dir_match is None:
        return None
    return _hash_body(text, base_dir_match.end(), row, name)


def _extract_from_frontmatter(text: str, catalog: dict) -> tuple[str, str] | None:
    # Fallback for raw file content pasted or @-referenced directly (not via
    # a slash command) -- there, the YAML frontmatter is still present
    # verbatim, unlike the command-invocation path above.
    skills = catalog.get("skills", {})
    for match in FRONTMATTER_RE.finditer(text):
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        # A stray "---" block in the prompt can parse to a scalar or list.
        if not isinstance(frontmatter, dict):
            continue
        name = frontmatter.get("name")
        row = skills.get(name)
        if row is None:
            continue
        found = _hash_body(text, match.end(), row, name)
        if found:
            return found
    return None


def _extract_skill(texts: list[str], catalog: dict) -> tuple[str, str] | None:
    for text in texts:
        found = _extract_from_command(text, catalog) or _extract_from_frontmatter(text, catalog)
        if found:
            return found
    return None


class SkillRoutingCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        # session_key -> (skill_id, first_invoked_at, last_used_at)
        self._sticky: OrderedDict[str, tuple[str, float, float]] = OrderedDict()

    def _session_key(self, user_api_key_dict, data: dict, headers: dict) -> str:
        key = (
            getattr(user_api_key_dict, "token", None)
            or getattr(user_api_key_dict, "api_key", None)
            or "default"
        )
        # Stickiness is scoped to a conversation, not the whole virtual key, so
        # an unrelated later chat on the same key does not inherit a tagged
        # route. Prefer a client-supplied id: the fingerprint fallback shifts if
        # history is compacted, since the first user turn can change mid-chat.
        conversation = headers.get("x-conversation-id")
        if not conversation:
            first_user = next(
                (
                    _message_text(message.get("content"))
                    for message in data.get("messages", [])
                    if message.get("role") == "user"
                ),
                "",
            )
            conversation = sha256_hex(first_user.encode())
        return f"{key}:{conversation}"

    def _get_sticky(self, session_key: str) -> str | None:
        entry = self._sticky.get(session_key)
        if entry is None:
            return None
        skill_id, first_invoked_at, last_used_at = entry
        now = time.monotonic()
        if now - last_used_at > STICKY_IDLE_TIMEOUT_SECONDS:
            del self._sticky[session_key]
            _log(f"llm-trunk: sticky route for {skill_id!r} expired (idle)")
            return None
        if now - first_invoked_at > STICKY_MAX_AGE_SECONDS:
            del self._sticky[session_key]
            _log(f"llm-trunk: sticky route for {skill_id!r} expired (max-age)")
            return None
        return skill_id

    def _touch_sticky(self, session_key: str, skill_id: str, *, reinvoked: bool) -> None:
        now = time.monotonic()
        existing = self._sticky.get(session_key)
        first_invoked_at = now if (reinvoked or existing is None) else existing[1]
        self._sticky[session_key] = (skill_id, first_invoked_at, now)
        self._sticky.move_to_end(session_key)
        while len(self._sticky) > MAX_STICKY_SESSIONS:
            self._sticky.popitem(last=False)

    def _headers_trusted(self, user_api_key_dict) -> bool:
        # x-skill-id/x-skill-hash are a self-asserted claim: the hash proves
        # content integrity (decide() rejects a wrong one), but nothing here
        # proves authorization -- any caller who can read a skill file can
        # compute its real hash and assert it directly, bypassing Claude
        # Code's own invocation flow entirely. Real Claude Code traffic never
        # sends these headers, so only a key explicitly flagged via its own
        # metadata (set at `/key/generate` time, e.g.
        # `"metadata": {"trust_skill_headers": true}`) may use this path at
        # all. No key is flagged today -- this is a dormant mechanism for a
        # future non-interactive consumer, not something currently in use.
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        return bool(metadata.get("trust_skill_headers"))

    def _estimate_input_tokens(self, model: str, messages: list[dict], texts: list[str]) -> int:
        try:
            import litellm

            return litellm.token_counter(model=model, messages=messages)
        except Exception:
            return len("".join(texts)) // 4

    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data: dict, call_type: str
    ) -> dict:
        catalog = _load_catalog()
        # This LiteLLM version puts request headers under litellm_metadata,
        # not the "metadata" key some older docs/examples reference.
        headers = data.get("litellm_metadata", {}).get("headers", {}) or {}
        session_key = self._session_key(user_api_key_dict, data, headers)

        messages = _input_messages(data)
        texts = _candidate_texts(messages)

        skill_id = skill_hash = None
        if self._headers_trusted(user_api_key_dict):
            skill_id = headers.get("x-skill-id")
            skill_hash = headers.get("x-skill-hash")

        # An id without a hash is an unverified client claim, so both headers
        # are required together; otherwise fall through to body extraction.
        if not (skill_id and skill_hash):
            skill_id = skill_hash = None
            extracted = _extract_skill(texts, catalog)
            if extracted:
                skill_id, skill_hash = extracted

        sticky_skill_id = self._get_sticky(session_key)
        estimated_input_tokens = self._estimate_input_tokens(
            data.get("model", ""), messages, texts
        )

        decision = decide(catalog, skill_id, skill_hash, sticky_skill_id, estimated_input_tokens)

        if decision.action == "deny":
            _log(f"llm-trunk deny [{decision.code}]: {decision.reason}")
            raise HTTPException(
                status_code=DENY_STATUS.get(decision.code, 403), detail=decision.reason
            )

        effective_skill_id = skill_id or sticky_skill_id
        if effective_skill_id is not None:
            self._touch_sticky(session_key, effective_skill_id, reinvoked=skill_id is not None)

        data["model"] = decision.alias
        # LiteLLM's normalized reasoning parameter; it translates this into the
        # upstream provider's own effort/thinking configuration.
        data["reasoning_effort"] = decision.effort

        requested_max = data.get("max_tokens")
        data["max_tokens"] = (
            min(requested_max, decision.max_output) if requested_max else decision.max_output
        )

        data.setdefault("litellm_metadata", {})["skill_routing"] = {
            "skill_id": effective_skill_id,
            "skill_hash": skill_hash,
            "alias": decision.alias,
            "effort": decision.effort,
        }
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        metadata = kwargs.get("litellm_params", {}).get("metadata", {}) or {}
        routing = metadata.get("skill_routing", {})

        usage = getattr(response_obj, "usage", None)

        _log(
            "llm-trunk spend: "
            + json.dumps(
                {
                    "skill_id": routing.get("skill_id"),
                    "skill_hash": routing.get("skill_hash"),
                    "alias": routing.get("alias"),
                    "effort": routing.get("effort"),
                    "input_tokens": _usage_value(usage, "prompt_tokens", "input_tokens"),
                    "output_tokens": _usage_value(usage, "completion_tokens", "output_tokens"),
                    "cache_read_tokens": _usage_value(
                        usage, "cache_read_input_tokens", "cache_creation_input_tokens"
                    ),
                    "cost": kwargs.get("response_cost"),
                }
            )
        )


proxy_handler_instance = SkillRoutingCallback()
