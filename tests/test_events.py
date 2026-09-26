"""The log lines the callback writes are the contract with scripts/events.py:
each outcome must still be parsed, and shown on the dashboard."""
import asyncio
import json
import sys
import types

import pytest
from conftest import (
    REPO,
    HTTPException,
    assistant,
    compaction_request,
    permission_check_request,
    request,
    skill_turn,
    subagent_request,
    unregistered_turn,
    user,
)
from rich.console import Console

sys.path.insert(0, str(REPO / "scripts"))

import dashboard  # noqa: E402
import events  # noqa: E402
from report import parse  # noqa: E402

ROUTES = {"complex": "anthropic/claude-opus-5-5", "moderate": "anthropic/claude-sonnet-5", "light": "anthropic/claude-haiku-4-5"}


def render(*lines: str) -> str:
    """The dashboard's screen after these log lines, as docker prints them."""
    dash = dashboard.Dashboard(dashboard.load_prices(), ROUTES, 300)
    for item in parse(f"2026-09-26T11:00:00.000000000Z {line}" for line in lines):
        dash.add(*item)
    console = Console(record=True, width=200, height=40, color_system=None)
    console.print(dashboard.render(dash, 40))
    return console.export_text().split("─ live")[1]  # the feed


def _log_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("llm-trunk ")]


def _event(line: str) -> tuple[str, dict]:
    match = events.EVENT_RE.search(line)
    assert match, f"events.py wouldn't recognize: {line}"
    return match.group("kind"), json.loads(match.group("json"))


def _log_spend(gateway, data, cost=0.0123):
    kwargs = {"litellm_params": {"metadata": data["litellm_metadata"]}, "response_cost": cost}
    usage = types.SimpleNamespace(
        prompt_tokens=41000, completion_tokens=250, cache_read_input_tokens=40000, cache_creation_input_tokens=500
    )
    # Answered by the model the request's tier runs, as Anthropic would.
    served = {"light": "claude-haiku-4-5-20251001", "moderate": "claude-sonnet-5", "complex": "claude-opus-5-5-20260915"}
    response = types.SimpleNamespace(usage=usage, model=served.get(data["model"], "claude-opus-5-5-20260915"))
    asyncio.run(gateway.callback.async_log_success_event(kwargs, response, None, None))


def _spend_line(gateway, capsys, data) -> str:
    capsys.readouterr()
    _log_spend(gateway, data)
    (line,) = _log_lines(capsys.readouterr().out)
    return line


def test_spend_event_is_rendered(gateway, capsys):
    line = _spend_line(gateway, capsys, gateway.send(request(skill_turn("plan"))))
    kind, event = _event(line)
    assert kind == "spend"
    assert (event["request_type"], event["tier"], event["skill_id"], event["effort"]) == ("skill", "complex", "plan", "high")
    assert (event["input_tokens"], event["output_tokens"], event["cache_read_tokens"]) == (41000, 250, 40000)
    assert event["cache_write_tokens"] == 500
    assert event["model"] == "claude-opus-5-5-20260915"  # what actually answered
    assert event["requested_model"] == "claude-sonnet-5"  # what the client asked for
    assert event["session_tier"] == "light" and event["cost"] == 0.0123
    rendered = render(line)
    assert "🔖 invoked" in rendered and "skill (plan)" in rendered and "complex" in rendered
    assert "Opus 5.5" in rendered and "high" in rendered and "41k (c)" in rendered and "$0.012" in rendered


@pytest.mark.parametrize(
    ("build", "route", "request_type", "where"),
    [
        (lambda: request(skill_turn("plan"), assistant(), user("Next?")), "sticky", "normal", "complex · plan"),
        (lambda: subagent_request(), "subagent", "subagent", "moderate"),
        (lambda: compaction_request(skill_turn("plan"), assistant(), user("go")), "compaction", "compaction", "complex · plan"),
        (lambda: request(skill_turn("plan"), assistant(), unregistered_turn()), "unregistered", "skill (personal-notes)",
         "light · personal-notes"),
        (lambda: permission_check_request(), "permission (i)", "internal", "moderate · permission"),
        (lambda: request(user("<session>fix login</session>"), tools=False), "title (i)", "internal", "light"),
    ],
)
def test_every_request_type_is_rendered(gateway, capsys, build, route, request_type, where):
    gateway.send(request(skill_turn("plan")))  # puts the session on the complex tier
    line = _spend_line(gateway, capsys, gateway.send(build()))
    _, event = _event(line)
    rendered = render(line)
    assert f"{events.ICONS[route.split()[0]]} {route}" in rendered
    assert request_type in rendered and event["tier"] in rendered
    assert where in events.tier_text(event)  # the scenario runner's "tier · skill"


def test_permission_check_shows_the_model_claude_code_chose(gateway, capsys):
    assert "Sonnet 5" in render(_spend_line(gateway, capsys, gateway.send(permission_check_request())))


def test_long_skill_names_are_shown_in_full():
    event = {"request_type": "skill", "tier": "complex", "skill_id": "incident-postmortem", "skill_hash": "h", "alias": "complex"}
    assert events.tier_text(event) == "complex · incident-postmortem"
    assert "skill (incident-postmortem)" in render("llm-trunk spend: " + json.dumps(event))


def test_deny_event_is_rendered(gateway, capsys):
    capsys.readouterr()
    with pytest.raises(HTTPException):
        gateway.send(request(user("x" * 200_000)))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["code"], event["request_type"]) == ("deny", "input_cap", "normal")
    assert "light tier cap" in render(line)


def test_failed_event_is_rendered(gateway, capsys):
    data = gateway.send(request(user("Hi")))
    capsys.readouterr()
    kwargs = {"litellm_params": {"metadata": data["litellm_metadata"]}, "exception": types.SimpleNamespace(status_code=529)}
    asyncio.run(gateway.callback.async_log_failure_event(kwargs, None, None, None))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["status"], event["tier"]) == ("failed", 529, "light")
    assert "upstream 529" in render(line)


def test_expired_event_is_rendered(gateway, clock, capsys):
    gateway.send(request(skill_turn("plan")))
    clock.advance(10_000)
    capsys.readouterr()
    gateway.send(request(skill_turn("plan"), assistant(), user("Next?")))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["skill_id"], event["tier"]) == ("expired", "plan", "complex")
    assert "plan sticky route ended (idle) → back to untagged" in render(line)


def test_old_event_lines_still_render():
    # Logged before tiers: per-skill lanes, no request_type.
    old = {"skill_id": "qa-bug-logging", "skill_hash": None, "alias": "qa-bug-logging", "effort": "low",
           "session": "05887d29", "sticky_left_s": 300, "background": None, "input_tokens": 53000,
           "cache_read_tokens": 52000, "cost": 0.012}
    rendered = render("llm-trunk spend: " + json.dumps(old))
    assert "📌 sticky" in rendered and "05887d29" in rendered and "low" in rendered and "53k (c)" in rendered
    assert events.tier_text(old) == "qa-bug-logging"


def test_unknown_fields_are_ignored(gateway, capsys):
    line = _spend_line(gateway, capsys, gateway.send(request(skill_turn("plan"))))
    kind, event = _event(line)
    extended = f"llm-trunk {kind}: " + json.dumps({**event, "some_future_field": 1})
    assert render(extended) == render(line)
