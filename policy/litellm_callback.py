import json
import logging
import re
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from policy.decide import decide
from policy.hash import hash_skill_md

logger = logging.getLogger("llm_trunk.policy")

CATALOG_PATH = "/app/catalog.yaml"

# Frontmatter must open the extracted chunk exactly like a real SKILL.md file
# on disk. This is a heuristic for locating an embedded skill file inside a
# Messages API request body — verify the actual wire format against a live
# Claude Code request before trusting it (see LLM-TRUNK.md > Callback).
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


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


def _candidate_texts(data: dict) -> list[str]:
    texts = [_message_text(data.get("system"))]
    for message in data.get("messages", []):
        texts.append(_message_text(message.get("content")))
    return texts


def _extract_skill(data: dict, catalog: dict) -> tuple[str, bytes] | None:
    for text in _candidate_texts(data):
        idx = text.find("---")
        if idx == -1:
            continue
        candidate = text[idx:]
        match = FRONTMATTER_RE.match(candidate)
        if not match:
            continue
        frontmatter = yaml.safe_load(match.group(1)) or {}
        name = frontmatter.get("name")
        if name in catalog.get("skills", {}):
            return name, candidate.encode()
    return None


class SkillRoutingCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self._sticky: dict[str, str] = {}

    def _session_key(self, user_api_key_dict) -> str:
        return (
            getattr(user_api_key_dict, "token", None)
            or getattr(user_api_key_dict, "api_key", None)
            or "default"
        )

    def _estimate_input_tokens(self, data: dict) -> int:
        try:
            import litellm

            return litellm.token_counter(
                model=data.get("model", ""), messages=data.get("messages", [])
            )
        except Exception:
            text = "".join(_candidate_texts(data))
            return len(text) // 4

    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data: dict, call_type: str
    ) -> dict:
        catalog = _load_catalog()
        session_key = self._session_key(user_api_key_dict)

        headers = data.get("metadata", {}).get("headers", {}) or {}
        skill_id = headers.get("x-skill-id")
        skill_hash = headers.get("x-skill-hash")

        if not skill_id:
            extracted = _extract_skill(data, catalog)
            if extracted:
                skill_id, raw_bytes = extracted
                skill_hash = hash_skill_md(raw_bytes)

        sticky_skill_id = self._sticky.get(session_key)
        estimated_input_tokens = self._estimate_input_tokens(data)

        decision = decide(catalog, skill_id, skill_hash, sticky_skill_id, estimated_input_tokens)

        if decision.action == "deny":
            logger.warning("llm-trunk deny: %s", decision.reason)
            too_large = "max_input" in decision.reason or "price cliff" in decision.reason
            status_code = 413 if too_large else 403
            raise HTTPException(status_code=status_code, detail=decision.reason)

        effective_skill_id = skill_id or sticky_skill_id
        if effective_skill_id is not None:
            self._sticky[session_key] = effective_skill_id

        data["model"] = decision.alias
        data["effort"] = decision.effort
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
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None),
                    "cost": kwargs.get("response_cost"),
                }
            ),
        )


proxy_handler_instance = SkillRoutingCallback()
