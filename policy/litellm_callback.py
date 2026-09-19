import json
import logging
import re
from collections import OrderedDict
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from policy.decide import INPUT_CAP, PRICE_CLIFF, STALE_HASH, UNKNOWN_SKILL, decide
from policy.hash import sha256_hex

logger = logging.getLogger("llm_trunk.policy")

CATALOG_PATH = "/app/catalog.yaml"
MAX_STICKY_SESSIONS = 1024
MAX_FRONTMATTER = 4096

# The bounded quantifier matters: with an unbounded `.*?` a failed match
# backtracks to end-of-text at every "---" in the payload (markdown rules,
# pasted diffs), which is O(text * fences) on the per-request hot path.
FRONTMATTER_RE = re.compile(rf"---\n(.{{0,{MAX_FRONTMATTER}}}?)\n---\n", re.DOTALL)

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


def _extract_skill(texts: list[str], catalog: dict) -> tuple[str, str] | None:
    skills = catalog.get("skills", {})
    for text in texts:
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
            length = row.get("bytes")
            if length is None:
                logger.warning(
                    "llm-trunk: catalog row %r has no 'bytes', cannot verify its hash", name
                )
                continue
            # The catalog records the canonical file's byte length so the exact
            # SKILL.md region can be isolated from the prompt text that follows
            # it; hashing to end-of-message would never match the file digest.
            start = match.start()
            raw = text[start : start + length].encode()[:length]
            return name, sha256_hex(raw)
    return None


class SkillRoutingCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self._sticky: OrderedDict[str, str] = OrderedDict()

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

    def _set_sticky(self, session_key: str, skill_id: str) -> None:
        self._sticky[session_key] = skill_id
        self._sticky.move_to_end(session_key)
        while len(self._sticky) > MAX_STICKY_SESSIONS:
            self._sticky.popitem(last=False)

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
        headers = data.get("metadata", {}).get("headers", {}) or {}
        session_key = self._session_key(user_api_key_dict, data, headers)

        messages = _input_messages(data)
        texts = _candidate_texts(messages)

        skill_id = headers.get("x-skill-id")
        skill_hash = headers.get("x-skill-hash")

        # An id without a hash is an unverified client claim, so both headers
        # are required together; otherwise fall through to body extraction.
        if not (skill_id and skill_hash):
            skill_id = skill_hash = None
            extracted = _extract_skill(texts, catalog)
            if extracted:
                skill_id, skill_hash = extracted

        sticky_skill_id = self._sticky.get(session_key)
        estimated_input_tokens = self._estimate_input_tokens(
            data.get("model", ""), messages, texts
        )

        decision = decide(catalog, skill_id, skill_hash, sticky_skill_id, estimated_input_tokens)

        if decision.action == "deny":
            logger.warning("llm-trunk deny [%s]: %s", decision.code, decision.reason)
            raise HTTPException(
                status_code=DENY_STATUS.get(decision.code, 403), detail=decision.reason
            )

        effective_skill_id = skill_id or sticky_skill_id
        if effective_skill_id is not None:
            self._set_sticky(session_key, effective_skill_id)

        data["model"] = decision.alias
        # LiteLLM's normalized reasoning parameter; it translates this into the
        # upstream provider's own effort/thinking configuration.
        data["reasoning_effort"] = decision.effort

        requested_max = data.get("max_tokens")
        data["max_tokens"] = (
            min(requested_max, decision.max_output) if requested_max else decision.max_output
        )

        data.setdefault("metadata", {})["skill_routing"] = {
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

        logger.info(
            "llm-trunk spend: %s",
            json.dumps(
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
            ),
        )


proxy_handler_instance = SkillRoutingCallback()
