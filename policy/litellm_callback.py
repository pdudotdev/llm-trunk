import json
import os
import re
import time
from collections import OrderedDict
from typing import Any

import yaml
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from policy.decide import BACKGROUND, COMPACTION, NORMAL, SKILL, SUBAGENT, decide, lowest
from policy.hash import sha256_hex
from policy.models import model_key


def _log(message: str) -> None:
    # The host process (LiteLLM) owns its own logging config, which we don't
    # control and can't rely on: its handlers accept WARNING but silently
    # drop INFO regardless of our own logger's level. print() sidesteps that
    # entirely -- these lines only need to land in `docker compose logs`.
    print(message, flush=True)


CATALOG_PATH = "/app/catalog.yaml"
CONFIG_PATH = "/app/config.yaml"
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


_tier_models_cache: tuple[float, dict[str, str]] | None = None


def _tier_models() -> dict[str, str]:
    """Tier -> the model family it runs, from LiteLLM's own config. Re-read
    only when the file changes: permission checks run before most tool calls."""
    global _tier_models_cache
    mtime = os.path.getmtime(CONFIG_PATH)
    if _tier_models_cache is None or _tier_models_cache[0] != mtime:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        models = {entry["model_name"]: model_key(entry["litellm_params"]["model"]) for entry in config["model_list"]}
        _tier_models_cache = (mtime, models)
    return _tier_models_cache[1]


def passthrough_tier(catalog: dict, tier_models: dict[str, str], requested_model: str | None, ceiling: str) -> str:
    """The tier that runs the model the client asked for, up to `ceiling` (the
    highest tier the key may reach). LiteLLM only serves the tiers it's
    configured with, so a request can't keep a raw model name; when no tier
    runs that model, the ceiling -- the most capable the key allows, never a
    weaker one."""
    order = catalog["order"]
    reachable = order[: order.index(ceiling) + 1]
    wanted = model_key(requested_model)
    for tier in reversed(reachable):
        if tier_models.get(tier) == wanted:
            return tier
    return ceiling


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
# Auto mode's permission classifier (checked on the wire): Claude Code picks
# its model on purpose, and a weaker one would weaken the safety check, so it
# passes through untouched.
PERMISSION_CHECK_MARKER = "You are a security monitor for autonomous AI coding agents"
# /compact and auto-compaction resend the conversation with this block
# appended to the newest user turn (checked on the wire).
COMPACTION_PREFIX = "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."
# Claude Code's permission checks ask for at most this many tokens (checked on
# the wire); a larger ask is not one of them.
PERMISSION_CHECK_MAX_OUTPUT = 8192
# Set on every request a subagent makes, one id per subagent.
AGENT_HEADER = "x-claude-code-agent-id"
MAX_AGENTS = 1024


def _system_text(data: dict) -> str:
    return _message_text(data.get("system"))


def _background_call(data: dict, headers: dict) -> str | None:
    # Claude Code's own housekeeping requests, as opposed to the user's work.
    # They must not count as activity: an away summary would otherwise keep
    # a sticky route alive. Only considered for Claude Code traffic. The
    # header is client-settable, so being classed as background must never
    # grant anything: it only withholds (timer refreshes, skill invocation,
    # thinking) or leaves the request as the client sent it.
    if _header(headers, "x-claude-code-session-id") is None:
        return None
    if not data.get("tools") and PERMISSION_CHECK_MARKER in _system_text(data):
        return "permission_check"
    last_text = next((block for block in reversed(_latest_user_blocks(data)) if block.strip()), "")
    if not data.get("tools") and last_text.lstrip().startswith(TITLE_PREFIX):
        return "title"
    for prefix, kind in BACKGROUND_PREFIXES.items():
        if last_text.lstrip().startswith(prefix):
            return kind
    return None


def _is_compaction(data: dict, headers: dict) -> bool:
    # Checked before skills: compaction resends the newest user turn, so the
    # turn it compacts may itself be a skill invocation. Compaction is exempt
    # from the input cap, so like background calls it needs the Claude Code
    # header; the key's budget cap is the backstop against a forged one.
    if _header(headers, "x-claude-code-session-id") is None:
        return False
    last_text = next((block for block in reversed(_latest_user_blocks(data)) if block.strip()), "")
    return last_text.lstrip().startswith(COMPACTION_PREFIX) and "<summary>" in last_text


def _invoked_skill_name(blocks: list[str], tool_names: list[str]) -> str | None:
    """The name of a skill invoked this turn, registered or not.

    A skill arrives with its "Base directory for this skill" block; built-in
    commands such as /model or /compact never do, so they don't count."""
    body = next((index for index, block in enumerate(blocks) if BASE_DIR_RE.search(block)), None)
    if body is None:
        return None
    # The skill's own tag is the last one before its body; an earlier tag in
    # the same turn may be a built-in command such as /model.
    tags = [match.group("name") for block in blocks[: body + 1] for match in COMMAND_NAME_RE.finditer(block)]
    if tags:
        return tags[-1]
    return tool_names[0] if tool_names else "unknown"


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
        # session_key -> (tier, skill_id, first_invoked_at, last_used_at)
        self._sticky: OrderedDict[str, tuple[str, str, float, float]] = OrderedDict()
        # (session_key, agent id) -> tier: a subagent keeps its tier (and cache)
        self._agents: OrderedDict[tuple[str, str], str] = OrderedDict()

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
    def _sticky_deadline(entry: tuple[str, str, float, float]) -> tuple[float, str]:
        # When (monotonic) a route stops sticking, and which timer ends it:
        # idle since the last real turn, or max age since the last invocation.
        _, _, first_invoked_at, last_used_at = entry
        idle = last_used_at + STICKY_IDLE_TIMEOUT_SECONDS
        max_age = first_invoked_at + STICKY_MAX_AGE_SECONDS
        return (idle, "idle") if idle <= max_age else (max_age, "max-age")

    def _get_sticky(self, session_key: str) -> tuple[str, str] | None:
        """(tier, skill_id) of the conversation's live route, if any."""
        entry = self._sticky.get(session_key)
        if entry is None:
            return None
        deadline, why = self._sticky_deadline(entry)
        if time.monotonic() > deadline:
            del self._sticky[session_key]
            _log_event(
                "expired",
                {"skill_id": entry[1], "tier": entry[0], "why": why, "session": _short_session(session_key)},
            )
            return None
        return entry[0], entry[1]

    def _touch_sticky(self, session_key: str, tier: str, skill_id: str, *, reinvoked: bool) -> None:
        now = time.monotonic()
        existing = self._sticky.get(session_key)
        first_invoked_at = now if (reinvoked or existing is None) else existing[2]
        self._sticky[session_key] = (tier, skill_id, first_invoked_at, now)
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

    def _remember_agent(self, agent_key: tuple[str, str], tier: str) -> None:
        self._agents[agent_key] = tier
        self._agents.move_to_end(agent_key)
        while len(self._agents) > MAX_AGENTS:
            self._agents.popitem(last=False)

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
        # actually allowed to *reach* each skill's tier, after extraction,
        # before decide() ever sees the claim. A key without this metadata
        # field is unrestricted. A skill a key may not use is routed like an
        # unregistered one: to the lowest tier.
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        if "allowed_skills" not in metadata:
            return None
        allowed = metadata["allowed_skills"]
        if isinstance(allowed, list) and all(isinstance(skill, str) for skill in allowed):
            return set(allowed)
        # Present but malformed (e.g. a bare string): fail closed to the
        # lowest tier rather than silently treating the key as unrestricted.
        _log(f"llm-trunk: malformed allowed_skills {allowed!r}; key restricted to the lowest tier")
        return set()

    @staticmethod
    def _highest_tier(catalog: dict, allowed: set[str] | None) -> str:
        """The highest tier a key can reach: any tier when unrestricted, else
        the highest tier among the skills it may use (the lowest if none)."""
        order = catalog["order"]
        if allowed is None:
            return order[-1]
        tiers = [catalog["skills"][skill]["tier"] for skill in allowed if skill in catalog["skills"]]
        return max(tiers, key=order.index, default=order[0])

    def _classify(self, data: dict, headers: dict, catalog: dict, user_api_key_dict) -> dict:
        """What this request is: request type plus the facts routing needs."""
        background = _background_call(data, headers)
        if background == "permission_check":
            # First, even when a subagent's action is being checked: the check
            # must not be demoted along with the subagent.
            return {"request_type": BACKGROUND, "background": background}
        agent_id = _header(headers, AGENT_HEADER)
        if agent_id:
            # A subagent's own skill invocations don't lift it above its tier.
            return {"request_type": SUBAGENT, "agent_id": agent_id}
        if _is_compaction(data, headers):
            return {"request_type": COMPACTION}
        if background:
            return {"request_type": BACKGROUND, "background": background}

        skill_id = skill_hash = None
        if self._headers_trusted(user_api_key_dict):
            skill_id, skill_hash = headers.get("x-skill-id"), headers.get("x-skill-hash")
        # An id without a hash is an unverified client claim, so both headers
        # are required together; otherwise fall through to body extraction,
        # scoped to only the newest user turn (see _latest_user_blocks).
        if not (skill_id and skill_hash):
            skill_id = skill_hash = None
            blocks, tool_names = _latest_user_blocks(data), _skill_tool_names(data)
            extracted = _extract_skill(blocks, catalog, tool_names)
            if extracted:
                skill_id, skill_hash = extracted
            else:
                unregistered = _invoked_skill_name(blocks, tool_names)
                if unregistered:
                    return {"request_type": SKILL, "registered": False, "skill_name": unregistered}
        if skill_id is None:
            return {"request_type": NORMAL}
        allowed = self._allowed_skills(user_api_key_dict)
        if allowed is not None and skill_id not in allowed:
            return {"request_type": SKILL, "registered": False, "skill_name": skill_id}
        return {"request_type": SKILL, "registered": True, "skill_id": skill_id, "skill_hash": skill_hash}

    async def async_pre_call_hook(
        self, user_api_key_dict, cache, data: dict, call_type: str
    ) -> dict:
        catalog = _load_catalog()
        # This LiteLLM version puts request headers under litellm_metadata,
        # not the "metadata" key some older docs/examples reference.
        headers = data.get("litellm_metadata", {}).get("headers", {}) or {}
        session_key = self._session_key(user_api_key_dict, data, headers)
        request = self._classify(data, headers, catalog, user_api_key_dict)
        request_type, background = request["request_type"], request.get("background")
        # The model Claude Code asked for, before the tier replaces it: the
        # baseline for "what would this have cost without llm-trunk".
        requested_model = data.get("model")

        sticky = self._get_sticky(session_key)
        allowed = self._allowed_skills(user_api_key_dict)
        if sticky is not None and allowed is not None and sticky[1] not in allowed:
            sticky = None
        # Sticky and subagent tiers live in memory, but catalog.yaml is re-read
        # on every request: a tier renamed or removed since is forgotten, not
        # a crash.
        if sticky is not None and sticky[0] not in catalog["tiers"]:
            self._sticky.pop(session_key, None)
            sticky = None
        session_tier = sticky[0] if sticky else None
        agent_key = (session_key, request.get("agent_id") or "")
        if self._agents.get(agent_key) not in (None, *catalog["tiers"]):
            del self._agents[agent_key]
        routing = {
            # A skill request names its own skill (None when unregistered);
            # other requests show the session's sticky skill.
            "skill_id": request.get("skill_id") if request_type in (SKILL, SUBAGENT) else (sticky[1] if sticky else None),
            "skill_hash": request.get("skill_hash"),
            "request_type": request_type,
            "session_tier": session_tier or lowest(catalog),
            "estimated_input_tokens": _estimate_input_tokens(data),
            "session": _short_session(session_key),
            "background": background,
            "requested_model": requested_model,
            "agent": (request.get("agent_id") or "")[:8] or None,
            "unregistered_skill": request.get("skill_name"),
        }

        if background == "permission_check":
            # Claude Code picks this model on purpose; a weaker one would weaken
            # the safety check. It goes to the tier running that model, with
            # Claude Code's own effort. Its shape can be forged, though, so it
            # never reaches a tier the key couldn't reach with a skill, and
            # keeps that tier's input cap and a max_tokens ceiling.
            ceiling = self._highest_tier(catalog, allowed)
            tier = passthrough_tier(catalog, _tier_models(), requested_model, ceiling)
            cap = catalog["tiers"][tier]["max_input"]
            if routing["estimated_input_tokens"] >= cap:
                reason = (
                    f"llm-trunk: ~{routing['estimated_input_tokens']} input tokens exceeds the {tier} tier cap "
                    f"({cap}) for a permission check"
                )
                _log_event("deny", {"code": "input_cap", "reason": reason, "session": routing["session"],
                                    "background": background, "request_type": request_type})
                raise HTTPException(status_code=DENY_STATUS, detail=reason)
            data["max_tokens"] = min(data.get("max_tokens") or PERMISSION_CHECK_MAX_OUTPUT, PERMISSION_CHECK_MAX_OUTPUT)
            data["model"] = tier
            data.setdefault("litellm_metadata", {})["skill_routing"] = {
                **routing, "skill_id": None, "alias": tier, "tier": tier, "effort": None, "sticky_expires_at": None,
            }
            return data

        decision = decide(
            catalog,
            request_type,
            routing["estimated_input_tokens"],
            # A title is ~1k tokens with nothing cached: the lowest tier is free savings.
            session_tier=None if background == "title" else session_tier,
            skill_id=request.get("skill_id"),
            skill_hash=request.get("skill_hash"),
            registered=request.get("registered", False),
            agent_tier=self._agents.get(agent_key),
        )

        if decision.action == "deny":
            _log_event(
                "deny",
                {
                    "code": decision.code,
                    "reason": decision.reason,
                    "session": routing["session"],
                    "background": background,
                    "request_type": request_type,
                },
            )
            raise HTTPException(status_code=DENY_STATUS, detail=decision.reason)

        # Only the user's own turns move the sticky route.
        if request_type == SKILL and request.get("registered"):
            self._touch_sticky(session_key, decision.tier, request["skill_id"], reinvoked=True)
        elif request_type == SKILL:
            # An unregistered skill drops the conversation to the lowest tier,
            # and its follow-ups stay there.
            self._sticky.pop(session_key, None)
        elif request_type == NORMAL and sticky is not None:
            self._touch_sticky(session_key, sticky[0], sticky[1], reinvoked=False)
        elif request_type == SUBAGENT:
            self._remember_agent(agent_key, decision.tier)

        data["model"] = decision.tier
        # Claude Code sends its own `thinking` + `output_config.effort` (the
        # user's /effort), and LiteLLM lets caller-supplied values win over
        # `reasoning_effort` -- left in place, the tier's effort is silently
        # ignored. Drop the client's; LiteLLM then maps ours per model
        # (adaptive thinking + effort on Opus/Sonnet 5, a thinking budget on
        # Haiku 4.5). A title gets no thinking at all: the tier's effort
        # turned thinking on for a request that asked for none, and keeping a
        # client's own budget could exceed the capped max_tokens (a 400).
        # Suggestions, recaps and compaction do get the tier's effort --
        # Anthropic's prompt cache doesn't match across thinking settings.
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

        live = self._sticky.get(session_key)
        data.setdefault("litellm_metadata", {})["skill_routing"] = {
            **routing,
            "skill_id": routing["skill_id"] if live or request_type == SKILL else None,
            "alias": decision.tier,
            "tier": decision.tier,
            "effort": None if background == "title" else decision.effort,
            "sticky_expires_at": self._sticky_expires_at(session_key) if live and request_type != SUBAGENT else None,
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
            "request_type": routing.get("request_type"),
            "tier": routing.get("tier"),
            "session_tier": routing.get("session_tier"),
            "agent": routing.get("agent"),
            "unregistered_skill": routing.get("unregistered_skill"),
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
                # tier's model can change later, the log line shouldn't.
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
