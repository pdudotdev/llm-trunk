"""Shared test setup.

The callback imports two names from LiteLLM and FastAPI -- a base class and an
exception. Small stand-ins replace them here, so the tests exercise llm-trunk's
own logic without installing LiteLLM (and can't be affected by its version).
"""
import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_stubs() -> None:
    class CustomLogger:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail=None) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = CustomLogger
    integrations = types.ModuleType("litellm.integrations")
    integrations.custom_logger = custom_logger
    litellm = types.ModuleType("litellm")
    litellm.integrations = integrations
    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = HTTPException
    sys.modules.update(
        {
            "litellm": litellm,
            "litellm.integrations": integrations,
            "litellm.integrations.custom_logger": custom_logger,
            "fastapi": fastapi,
        }
    )


_install_stubs()

from policy import litellm_callback as cb  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException

# Synthetic skills, one per tier: the tests don't depend on the real skill
# files (those are checked separately in test_catalog.py).
BODIES = {
    "plan": "# Design review\nReview the proposed design and list its risks.\n",
    "review": "# Code review\nReview the change for bugs and missing tests.\n",
    "verify": "# Commit message\nWrite a short commit message for the change.\n",
}
TIERS = {"plan": "complex", "review": "moderate", "verify": "light"}
LIGHT_CAP = 64000


def make_catalog() -> dict:
    catalog = {
        "version": 2,
        "order": ["light", "moderate", "complex"],
        "tiers": {
            "light": {"effort": "low", "max_input": LIGHT_CAP, "max_output": 4000},
            "moderate": {"effort": "medium", "max_input": 128000, "max_output": 4000},
            "complex": {"effort": "high", "max_input": 180000, "max_output": 8000},
        },
        "skills": {},
    }
    for name, body in BODIES.items():
        raw = body.encode()
        catalog["skills"][name] = {"tier": TIERS[name], "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return catalog


@pytest.fixture
def catalog(tmp_path, monkeypatch) -> dict:
    data = make_catalog()
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(cb, "CATALOG_PATH", str(path))
    monkeypatch.setattr(cb, "CONFIG_PATH", str(REPO / "litellm" / "config.yaml"))
    return data


class FakeClock:
    """Stands in for the `time` module inside the callback."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000 + self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cb, "time", fake)
    return fake


def key(**metadata) -> types.SimpleNamespace:
    return types.SimpleNamespace(token="hashed-test-key", metadata=metadata)


class Gateway:
    """One callback instance, i.e. one running gateway with its sticky table."""

    def __init__(self) -> None:
        self.callback = cb.SkillRoutingCallback()

    def send(self, data: dict, api_key=None) -> dict:
        return asyncio.run(
            self.callback.async_pre_call_hook(api_key or key(), None, data, "anthropic_messages")
        )


@pytest.fixture
def gateway(catalog, clock) -> Gateway:
    return Gateway()


# --- Claude Code-shaped request builders -------------------------------------


def user(*texts: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text} for text in texts]}


def assistant(text: str = "Done.") -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def skill_turn(name: str, body: str | None = None, arguments: str | None = None) -> dict:
    """A typed /skill as Claude Code sends it: a command tag, then the body."""
    body = BODIES[name] if body is None else body
    text = f"Base directory for this skill: /repo/.claude/skills/{name}\n\n{body}"
    if arguments:
        text += f"\n\nARGUMENTS: {arguments}"
    return user(
        f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>",
        text,
    )


def request(*messages: dict, session: str = "session-1", tools: bool = True, claude_code: bool = True, **extra) -> dict:
    data = {
        "model": "claude-sonnet-5",
        "max_tokens": 32000,
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": list(messages),
        "metadata": {"user_id": json.dumps({"session_id": session})},
        "litellm_metadata": {"headers": {"x-claude-code-session-id": session} if claude_code else {}},
    }
    if tools:
        data["tools"] = [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}}]
    data.update(extra)
    return data


def routing(data: dict) -> dict:
    return data["litellm_metadata"]["skill_routing"]


def unregistered_turn(name: str = "personal-notes", arguments: str = "rotate the keys on friday") -> dict:
    """An invoked skill that isn't in the catalog: same shape as a registered one."""
    return skill_turn(name, body="A personal skill that isn't in the catalog.\n", arguments=arguments)


def subagent_request(agent_id: str = "a02164c76a605652b", *messages: dict, session: str = "session-1") -> dict:
    data = request(*(messages or (user("List the folders in .claude/skills."),)), session=session)
    data["system"] = [{"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true;"},
                      {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]
    data["litellm_metadata"]["headers"]["x-claude-code-agent-id"] = agent_id
    return data


COMPACTION_INSTRUCTION = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\nYour task is to create a detailed summary "
    "of the conversation so far, followed by a <summary> block. Tool calls will be rejected and you will fail the task."
)


def compaction_request(*history: dict, session: str = "session-1", **extra) -> dict:
    """Compaction resends the conversation, appending its instruction to the newest user turn."""
    history = list(history)
    last = dict(history[-1])
    last["content"] = list(last["content"]) + [{"type": "text", "text": COMPACTION_INSTRUCTION}]
    return request(*history[:-1], last, session=session, **extra)


def permission_check_request(session: str = "session-1", model: str = "claude-sonnet-5") -> dict:
    data = request(user("<transcript>\n{\"user\":\"list the folders\"}\n</transcript>"), session=session, tools=False)
    data["model"] = model
    data["max_tokens"] = 64
    data["system"] = [{"type": "text", "text": "You are a security monitor for autonomous AI coding agents.\n\n## Context"}]
    return data
