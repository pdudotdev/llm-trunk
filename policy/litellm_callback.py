import json
import re
import time
from collections import OrderedDict
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from policy.decide import decide
from policy.hash import sha256_hex


def _log(message: str) -> None:
    # The host process (LiteLLM) owns its own logging config, which we don't
    # control and can't rely on: its handlers accept WARNING but silently
    # drop INFO regardless of our own logger's level. print() sidesteps that
    # entirely -- these lines only need to land in `docker compose logs`.
    print(message, flush=True)


CATALOG_PATH = "/app/catalog.yaml"
MAX_STICKY_SESSIONS = 1024

# Input-size estimate: a plain character count over everything the model is
# billed for -- system, messages AND tool definitions. litellm.token_counter
# only sees `messages` and undercounts Anthropic-format blocks: live traffic
# had ~52k-token requests estimated under a 16k cap. Claude Code's tool
# definitions alone are ~21k tokens on every request. Calibrated on live
# traffic: real counts ran ~3.3 chars/token on Haiku 4.5 and ~2.5 on Sonnet 5
# (different tokenizers); 3 lands within ~15% of both.
CHARS_PER_TOKEN = 3
# Base64 image/PDF payloads are billed by content, not encoded length --
# counting their characters would deny any screenshot.
MEDIA_BLOCK_TOKENS = 1600

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

# Claude Code's actual wire format for a slash-invoked project skill: the
# YAML frontmatter is stripped client-side and replaced with this tag plus a
# "Base directory" line, immediately followed by the body verbatim. Confirmed
# by inspecting a real request -- the raw "---\nname: ...\n---\n" frontmatter
# never appears on the wire for a skill invoked this way.
COMMAND_NAME_RE = re.compile(r"<command-name>/(?P<name>[\w-]+)</command-name>")
BASE_DIR_RE = re.compile(r"Base directory for this skill: [^\n]*\n\n")
# The only thing Claude Code puts after the body, in the body's own content
# block: this line when the skill was invoked with arguments, else nothing.
ARGUMENTS_MARKER = "\n\nARGUMENTS: "

# One status for every policy deny. Claude Code shows a 422's message as-is;
# it replaces any 413 with its own "Request too large (max 32MB)" text and
# prefixes a 403 with "Failed to authenticate", hiding the real reason.
DENY_STATUS = 422


def _header(headers: dict, name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name), None)


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


def _count_chars(node: Any) -> int:
    if isinstance(node, str):
        return len(node)
    if isinstance(node, dict):
        if node.get("type") == "base64":
            return MEDIA_BLOCK_TOKENS * CHARS_PER_TOKEN
        # Earlier turns' thinking blocks are stripped upstream, not billed.
        if node.get("type") in ("thinking", "redacted_thinking"):
            return 0
        return sum(len(key) + _count_chars(value) for key, value in node.items())
    if isinstance(node, list):
        return sum(_count_chars(item) for item in node)
    return 0 if node is None else len(str(node))


def _estimate_input_tokens(data: dict) -> int:
    billed = [data.get("system"), data.get("messages"), data.get("tools")]
    return _count_chars(billed) // CHARS_PER_TOKEN


def _content_blocks(content: Any) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content) if isinstance(content, list) else []


def _fold_system_messages(messages: list[dict]) -> list[dict]:
    # Claude Code sends some context (e.g. @-referenced files) as a
    # mid-conversation role:"system" message when it believes the model
    # supports that -- but the routed model may not (Haiku 4.5 rejects it
    # with a 400). Folding it into the adjacent user turn is how Claude Code
    # itself sends the same content to models without that support. Prefer
    # the preceding user turn; otherwise append to the following one, after
    # its blocks, since tool_result blocks must lead the turn after a
    # tool_use. Leading system messages are left alone: that's where
    # OpenAI-format clients put their system prompt, which LiteLLM maps to
    # Anthropic's top-level `system` -- folding it would demote it to user
    # text.
    if not any(message.get("role") == "system" for message in messages):
        return messages
    folded: list[dict] = []
    pending: list = []
    for message in messages:
        if message.get("role") == "system" and all(m.get("role") == "system" for m in folded):
            folded.append(message)
            continue
        if message.get("role") == "system":
            blocks = _content_blocks(message.get("content"))
            if folded and folded[-1].get("role") == "user" and not pending:
                previous = folded[-1]
                folded[-1] = {**previous, "content": _content_blocks(previous.get("content")) + blocks}
            else:
                pending.extend(blocks)
            continue
        if pending:
            if message.get("role") == "user":
                message = {**message, "content": _content_blocks(message.get("content")) + pending}
            else:
                folded.append({"role": "user", "content": pending})
            pending = []
        folded.append(message)
    if pending:
        folded.append({"role": "user", "content": pending})
    return folded


def _latest_user_blocks(data: dict) -> list[str]:
    # Claude Code resends the full conversation on every turn, so scanning
    # every message for a skill invocation finds a stale one from an earlier
    # turn and treats it as live -- pinning the route forever (stickiness
    # timers never get a chance to fire) and making it impossible to switch
    # skills (the older, no-longer-relevant block wins first-match). Only the
    # newest user turn can contain *this* turn's real invocation; anything
    # not found there correctly falls through to the sticky path instead,
    # where it belongs. Kept as separate content blocks, not joined: the
    # body's end is checked against the end of its own block.
    for message in reversed(data.get("messages", [])):
        if message.get("role") == "user":
            return [
                block.get("text", "")
                for block in _content_blocks(message.get("content"))
                if isinstance(block, dict)
            ]
    return []


def _log_event(kind: str, fields: dict) -> None:
    # One JSON line per outcome -- spend, deny, expired, failed -- which
    # scripts/watch.py renders. Structured so a client-supplied value (e.g. a
    # conversation id with spaces) can't break or hide a line.
    _log(f"llm-trunk {kind}: " + json.dumps(fields))


def _short_session(session_key: str) -> str:
    # Log label for a conversation: a prefix of its id only -- never the
    # virtual-key half of the session key.
    return session_key.rsplit(":", 1)[-1][:8]


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return value
    return None


def _hash_body(block: str, start: int, row: dict, name: str) -> tuple[str, str] | None:
    length = row.get("bytes")
    if length is None:
        _log(f"llm-trunk: catalog row {name!r} has no 'bytes', cannot verify its hash")
        return None
    # The catalog records the canonical body's byte length so the exact
    # region can be isolated from the ARGUMENTS line that may follow it.
    raw = block[start : start + length].encode()[:length]
    end = start + len(raw.decode(errors="ignore"))
    # The body must also *end* there: lines appended to the skill file since
    # it was hashed sit past the catalog's length and would otherwise pass
    # unverified. Hash through the end of the block instead, so the extra
    # content makes the digest mismatch and the request is denied as stale.
    rest = block[end:]
    if rest and not rest.startswith(ARGUMENTS_MARKER):
        raw = block[start:].encode()
    return name, sha256_hex(raw)


def _find_body(blocks: list[str], index: int, offset: int, row: dict, name: str):
    # The body follows its tag: in a later block of the same turn (how
    # Claude Code sends it today), or further along the same block.
    for block in blocks[index:]:
        base_dir_match = BASE_DIR_RE.search(block, offset)
        if base_dir_match:
            return _hash_body(block, base_dir_match.end(), row, name)
        offset = 0
    return None


def _skill_tool_names(data: dict) -> list[str]:
    # Claude invoking a skill on its own (its Skill tool, when a skill's
    # description matches the request) sends no <command-name> tag: the name
    # is in the assistant's tool_use, and the next user turn carries the
    # same "Base directory" block + body + ARGUMENTS line as a slash command.
    for message in reversed(data.get("messages", [])):
        if message.get("role") == "assistant":
            return [
                block["input"]["skill"]
                for block in _content_blocks(message.get("content"))
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Skill"
                and isinstance(block.get("input"), dict)
                and isinstance(block["input"].get("skill"), str)
            ]
    return []


def _extract_skill(
    blocks: list[str], catalog: dict, tool_names: list[str]
) -> tuple[str, str] | None:
    skills = catalog.get("skills", {})
    # Every tag, not just the first: another slash command's tag earlier in
    # the same turn (e.g. /model) must not hide a later skill invocation.
    for index, block in enumerate(blocks):
        for match in COMMAND_NAME_RE.finditer(block):
            name = match.group("name")
            row = skills.get(name)
            if row is None:
                continue
            found = _find_body(blocks, index, match.end(), row, name)
            if found:
                return found
    for name in tool_names:
        row = skills.get(name)
        if row is None:
            continue
        found = _find_body(blocks, 0, 0, row, name)
        if found:
            return found
    return None


# Fixed instructions Claude Code appends as the last user message of its own
# housekeeping requests (checked on the wire): the "while you were away"
# recap, ~3 minutes after the user leaves the window, and the next-prompt
# suggestion, right after a reply. Matched as text, so a future Claude Code
# rewording makes these look like ordinary turns again -- today's fallback,
# never worse.
BACKGROUND_PREFIXES = {
    "The user stepped away and is coming back.": "away_summary",
    "[SUGGESTION MODE:": "prompt_suggestion",
}
# Session naming: sent without tools, its only message wrapping the user's
# text in <session> tags. Both are required -- "no tools" alone would also
# catch a real turn from Claude Code run with tools disabled.
TITLE_PREFIX = "<session>"


def _background_call(data: dict, headers: dict) -> str | None:
    # Claude Code's own housekeeping requests, as opposed to the user's work.
    # They must not count as activity: an away summary would otherwise keep
    # a sticky route alive, and both would ride (and pay for) its lane.
    # Only considered for Claude Code traffic. The header is client-settable,
    # so being classed as background must never grant anything: it only
    # withholds (timer refreshes, skill invocation, thinking).
    if _header(headers, "x-claude-code-session-id") is None:
        return None
    last_text = next((block for block in reversed(_latest_user_blocks(data)) if block.strip()), "")
    if not data.get("tools") and last_text.lstrip().startswith(TITLE_PREFIX):
        return "title"
    for prefix, kind in BACKGROUND_PREFIXES.items():
        if last_text.lstrip().startswith(prefix):
            return kind
    return None


def _claude_code_session_id(data: dict) -> str | None:
    # Claude Code sends metadata.user_id as a JSON string carrying a
    # per-session UUID: stable across compaction and distinct between
    # sessions, unlike a fingerprint of the first user message.
    user_id = (data.get("metadata") or {}).get("user_id")
    if not isinstance(user_id, str):
        return None
    try:
        parsed = json.loads(user_id)
    except ValueError:
        return None
    session_id = parsed.get("session_id") if isinstance(parsed, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


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
        # route. Prefer an explicit id, then Claude Code's session id (from
        # its metadata, else its header). The first-user-message fingerprint
        # is a last resort: it shifts if history is compacted, and two
        # sessions opening with the same text share it.
        conversation = (
            _header(headers, "x-conversation-id")
            or _claude_code_session_id(data)
            or _header(headers, "x-claude-code-session-id")
        )
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

    @staticmethod
    def _sticky_deadline(entry: tuple[str, float, float]) -> tuple[float, str]:
        # When (monotonic) a route stops sticking, and which timer ends it:
        # idle since the last real turn, or max age since the last invocation.
        _, first_invoked_at, last_used_at = entry
        idle = last_used_at + STICKY_IDLE_TIMEOUT_SECONDS
        max_age = first_invoked_at + STICKY_MAX_AGE_SECONDS
        return (idle, "idle") if idle <= max_age else (max_age, "max-age")

    def _get_sticky(self, session_key: str) -> str | None:
        entry = self._sticky.get(session_key)
        if entry is None:
            return None
        deadline, why = self._sticky_deadline(entry)
        if time.monotonic() > deadline:
            del self._sticky[session_key]
            _log_event(
                "expired",
                {"skill_id": entry[0], "why": why, "session": _short_session(session_key)},
            )
            return None
        return entry[0]

    def _touch_sticky(self, session_key: str, skill_id: str, *, reinvoked: bool) -> None:
        now = time.monotonic()
        existing = self._sticky.get(session_key)
        first_invoked_at = now if (reinvoked or existing is None) else existing[1]
        self._sticky[session_key] = (skill_id, first_invoked_at, now)
        self._sticky.move_to_end(session_key)
        while len(self._sticky) > MAX_STICKY_SESSIONS:
            self._sticky.popitem(last=False)

    def _sticky_expires_at(self, session_key: str) -> float | None:
        # Wall-clock expiry, so the time left can be worked out when the
        # outcome is logged -- after the reply, which can take minutes.
        entry = self._sticky.get(session_key)
        if entry is None:
            return None
        deadline, _ = self._sticky_deadline(entry)
        return time.time() + (deadline - time.monotonic())

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
        # Strictly the JSON boolean: a string like "false" is truthy.
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        return metadata.get("trust_skill_headers") is True

    def _allowed_skills(self, user_api_key_dict) -> set[str] | None:
        # Closes the gap header-trust gating didn't: the tag itself can be
        # forged by anyone who can read a skill file (it's not a secret), so
        # trusting a *claim* -- however it was resolved, headers or a forged
        # <command-name> block -- was never going to work. This checks who is
        # actually allowed to *reach* each lane, after extraction, before
        # decide() ever sees the claim. A key without this metadata field is
        # unrestricted (today's qa-usage key: it legitimately needs every
        # skill, so there is nothing to allow-list yet). `untagged` is never
        # restricted -- there is nothing to protect by blocking the cheap
        # default lane.
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        if "allowed_skills" not in metadata:
            return None
        allowed = metadata["allowed_skills"]
        if isinstance(allowed, list) and all(isinstance(skill, str) for skill in allowed):
            return set(allowed)
        # Present but malformed (e.g. a bare string): fail closed to
        # untagged-only rather than silently treating the key as unrestricted.
        _log(f"llm-trunk: malformed allowed_skills {allowed!r}; key restricted to untagged")
        return set()

    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data: dict, call_type: str
    ) -> dict:
        catalog = _load_catalog()
        # This LiteLLM version puts request headers under litellm_metadata,
        # not the "metadata" key some older docs/examples reference.
        headers = data.get("litellm_metadata", {}).get("headers", {}) or {}
        session_key = self._session_key(user_api_key_dict, data, headers)
        background = _background_call(data, headers)

        skill_id = skill_hash = None
        if background is None and self._headers_trusted(user_api_key_dict):
            skill_id = headers.get("x-skill-id")
            skill_hash = headers.get("x-skill-hash")

        # An id without a hash is an unverified client claim, so both headers
        # are required together; otherwise fall through to body extraction,
        # scoped to only the newest user turn (see _latest_user_blocks).
        # Background calls never invoke a skill (a title prompt quotes the
        # user's first message, which may itself be an invocation).
        if background is None and not (skill_id and skill_hash):
            skill_id = skill_hash = None
            extracted = _extract_skill(
                _latest_user_blocks(data), catalog, _skill_tool_names(data)
            )
            if extracted:
                skill_id, skill_hash = extracted

        # Background calls ride the session's current lane (untagged if it has
        # none): recaps and suggestions carry the whole conversation, already
        # cached on that lane's model, and moving them would re-send it uncached.
        sticky_skill_id = self._get_sticky(session_key)

        allowed_skills = self._allowed_skills(user_api_key_dict)
        if allowed_skills is not None:
            if skill_id is not None and skill_id not in allowed_skills:
                skill_id = skill_hash = None
            if sticky_skill_id is not None and sticky_skill_id not in allowed_skills:
                sticky_skill_id = None

        estimated_input_tokens = _estimate_input_tokens(data)

        decision = decide(catalog, skill_id, skill_hash, sticky_skill_id, estimated_input_tokens)

        if decision.action == "deny":
            _log_event(
                "deny",
                {
                    "code": decision.code,
                    "reason": decision.reason,
                    "session": _short_session(session_key),
                    "background": background,
                },
            )
            raise HTTPException(status_code=DENY_STATUS, detail=decision.reason)

        effective_skill_id = skill_id or sticky_skill_id
        # Only the user's own turns count as activity for the sticky timers.
        if effective_skill_id is not None and background is None:
            self._touch_sticky(session_key, effective_skill_id, reinvoked=skill_id is not None)

        # The model Claude Code asked for, before the lane replaces it: the
        # baseline for "what would this have cost without llm-trunk".
        requested_model = data.get("model")
        data["model"] = decision.alias
        # Claude Code sends its own `thinking` + `output_config.effort` (the
        # user's /effort), and LiteLLM lets caller-supplied values win over
        # `reasoning_effort` -- left in place, the lane's effort is silently
        # ignored. Drop the client's; LiteLLM then maps ours per model
        # (adaptive thinking + effort on Opus/Sonnet 5, a thinking budget on
        # Haiku 4.5). A title gets no thinking at all: the lane's effort
        # turned thinking on for a request that asked for none, and keeping a
        # client's own budget could exceed the capped max_tokens (a 400).
        # Recaps and suggestions do get the lane's effort -- Anthropic's
        # prompt cache doesn't match across thinking settings, and with their
        # own they re-sent the whole conversation uncached (live: 0 of 54k).
        data.pop("thinking", None)
        output_config = data.get("output_config")
        if isinstance(output_config, dict):
            output_config.pop("effort", None)
            if not output_config:
                del data["output_config"]
        if background != "title":
            data["reasoning_effort"] = decision.effort
        data["messages"] = _fold_system_messages(data.get("messages", []))

        requested_max = data.get("max_tokens")
        data["max_tokens"] = (
            min(requested_max, decision.max_output) if requested_max else decision.max_output
        )

        data.setdefault("litellm_metadata", {})["skill_routing"] = {
            "skill_id": effective_skill_id,
            "skill_hash": skill_hash,
            "alias": decision.alias,
            "effort": None if background == "title" else decision.effort,
            "estimated_input_tokens": estimated_input_tokens,
            "session": _short_session(session_key),
            "sticky_expires_at": self._sticky_expires_at(session_key) if effective_skill_id else None,
            "background": background,
            "requested_model": requested_model,
        }
        return data

    @staticmethod
    def _routing_fields(kwargs) -> dict | None:
        metadata = kwargs.get("litellm_params", {}).get("metadata", {}) or {}
        routing = metadata.get("skill_routing")
        if not routing:
            return None
        expires_at = routing.get("sticky_expires_at")
        return {
            "skill_id": routing.get("skill_id"),
            "skill_hash": routing.get("skill_hash"),
            "alias": routing.get("alias"),
            "effort": routing.get("effort"),
            "estimated_input_tokens": routing.get("estimated_input_tokens"),
            "session": routing.get("session"),
            "sticky_left_s": max(0, int(expires_at - time.time())) if expires_at else None,
            "background": routing.get("background"),
            "requested_model": routing.get("requested_model"),
        }

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        fields = self._routing_fields(kwargs)
        if fields is None:
            return
        usage = getattr(response_obj, "usage", None)
        _log_event(
            "spend",
            {
                **fields,
                "input_tokens": _usage_value(usage, "prompt_tokens", "input_tokens"),
                "output_tokens": _usage_value(usage, "completion_tokens", "output_tokens"),
                # Reads only: a cache *write* is billed at 1.25x, the opposite
                # of what the watcher's "(c)" tag claims.
                "cache_read_tokens": _usage_value(usage, "cache_read_input_tokens"),
                # Both are included in input_tokens (LiteLLM adds them in).
                "cache_write_tokens": _usage_value(usage, "cache_creation_input_tokens"),
                # The model that actually answered, as the API reports it: a
                # lane's model can change later, the log line shouldn't.
                "model": getattr(response_obj, "model", None),
                "cost": kwargs.get("response_cost"),
            },
        )

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        # A request that was routed but failed upstream (a 400, 429, 529...).
        # Denies never get here: they're raised before routing metadata exists.
        fields = self._routing_fields(kwargs)
        if fields is None:
            return
        error = kwargs.get("exception")
        _log_event(
            "failed",
            {
                **fields,
                "status": getattr(error, "status_code", None),
                "error": str(error)[:300] if error else None,
            },
        )


proxy_handler_instance = SkillRoutingCallback()
