"""scripts/check_rules.py: the rule checker agrees with the gateway, and
catches every kind of violation."""
import asyncio
import json
import sys
import types
from datetime import datetime, timezone

import pytest
from conftest import (
    REPO,
    assistant,
    compaction_request,
    make_catalog,
    permission_check_request,
    request,
    skill_turn,
    subagent_request,
    unregistered_turn,
    user,
)

sys.path.insert(0, str(REPO / "scripts"))

import check_rules  # noqa: E402

TIER_MODELS = {"light": "anthropic/claude-haiku-4-5", "moderate": "anthropic/claude-sonnet-5", "complex": "anthropic/claude-opus-5-5"}
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
SERVED = {"light": "claude-haiku-4-5-20251001", "moderate": "claude-sonnet-5", "complex": "claude-opus-5-5"}


def _logged(gateway, data, capsys) -> dict:
    """Send a request through the gateway and return the spend event it logs."""
    sent = gateway.send(data)
    fields = sent["litellm_metadata"]["skill_routing"]
    capsys.readouterr()
    usage = types.SimpleNamespace(prompt_tokens=1000, completion_tokens=10)
    model = SERVED.get(fields.get("tier")) or fields.get("requested_model")
    kwargs = {"litellm_params": {"metadata": sent["litellm_metadata"]}, "response_cost": 0.01}
    asyncio.run(gateway.callback.async_log_success_event(kwargs, types.SimpleNamespace(usage=usage, model=model), None, None))
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("llm-trunk spend: "))
    return json.loads(line.removeprefix("llm-trunk spend: "))


def test_a_whole_session_through_the_gateway_breaks_no_rule(gateway, capsys):
    steps = [
        request(user("hi")),
        request(user("<session>hi</session>"), tools=False),
        request(skill_turn("plan")),
        request(skill_turn("plan"), assistant(), user("next")),
        subagent_request("agent-1"),
        request(skill_turn("plan"), assistant(), user("[SUGGESTION MODE: x]")),
        permission_check_request(),
        compaction_request(skill_turn("plan"), assistant(), user("go")),
        request(skill_turn("plan"), assistant(), skill_turn("review")),
        subagent_request("agent-1"),  # keeps moderate from when it started
        subagent_request("agent-2"),  # a new one: one below moderate
        request(skill_turn("plan"), assistant(), unregistered_turn()),
        request(skill_turn("plan"), assistant(), unregistered_turn(), assistant(), user("more")),
    ]
    events = [(T0, "spend", _logged(gateway, data, capsys)) for data in steps]
    result = check_rules.check(events, make_catalog(), TIER_MODELS)
    assert result == {"checked": len(steps), "skipped": 0, "violations": []}


def _event(**fields):
    base = {"request_type": "normal", "tier": "light", "session_tier": "light", "session": "s1",
            "model": "claude-haiku-4-5-20251001", "background": None}
    return (T0, "spend", {**base, **fields})


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"tier": "complex", "model": "claude-opus-5-5"}, "expected tier light, got complex"),
        ({"request_type": "skill", "skill_id": "plan", "tier": "light"}, "expected tier complex, got light"),
        ({"request_type": "skill", "unregistered_skill": "mine", "session_tier": "complex", "tier": "complex", "model": "claude-opus-5-5"}, "expected tier light"),
        ({"request_type": "subagent", "agent": "a1", "session_tier": "complex", "tier": "complex", "model": "claude-opus-5-5"}, "expected tier moderate"),
        ({"request_type": "compaction", "session_tier": "complex", "tier": "light"}, "expected tier complex"),
        ({"request_type": "background", "background": "title", "session_tier": "complex", "tier": "complex", "model": "claude-opus-5-5"}, "expected tier light"),
        ({"request_type": "background", "background": "prompt_suggestion", "session_tier": "moderate", "tier": "light"}, "expected tier moderate"),
        ({"background": "permission_check", "request_type": "background", "tier": None, "requested_model": "claude-sonnet-5", "model": "claude-haiku-4-5"}, "expected passthrough"),
        ({"model": "claude-sonnet-5"}, "tier light should run claude-haiku-4-5, but claude-sonnet-5 answered"),
    ],
)
def test_each_violation_is_caught(fields, problem):
    result = check_rules.check([_event(**fields)], make_catalog(), TIER_MODELS)
    assert len(result["violations"]) == 1
    assert problem in result["violations"][0]["problem"]


def test_subagent_that_changes_tier_midway_is_caught():
    first = _event(request_type="subagent", agent="a1", session_tier="complex", tier="moderate", model="claude-sonnet-5")
    later = _event(request_type="subagent", agent="a1", session_tier="moderate", tier="light")
    result = check_rules.check([first, later], make_catalog(), TIER_MODELS)
    assert [v["rule"] for v in result["violations"]] == ["subagent keeps its tier"]


def test_old_lines_are_skipped_not_judged():
    old = (T0, "spend", {"skill_id": "qa-bug-logging", "alias": "qa-bug-logging", "cost": 0.01})
    assert check_rules.check([old], make_catalog(), TIER_MODELS) == {"checked": 0, "skipped": 1, "violations": []}


def test_real_config_loads():
    catalog, tier_models = check_rules.load_config()
    assert set(tier_models) >= set(catalog["order"])
