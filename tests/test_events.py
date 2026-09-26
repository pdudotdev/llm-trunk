"""The log lines the callback writes are the contract with scripts/watch.py:
each outcome must still be picked up and rendered by the watcher."""
import asyncio
import importlib.util
import json
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

spec = importlib.util.spec_from_file_location("watch", REPO / "scripts" / "watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

MODELS = {"complex": "Opus 5.5", "moderate": "Sonnet 5", "light": "Haiku 4.5"}


def _log_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("llm-trunk ")]


def _event(line: str) -> tuple[str, dict]:
    match = watch.EVENT_RE.search(line)
    assert match, f"watcher wouldn't recognize: {line}"
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
    rendered = watch.render(line, "12:00:00", MODELS)
    assert "invoked" in rendered and "Opus 5.5 · high" in rendered and "complex · plan" in rendered
    assert "(c)" in rendered and "$0.012" in rendered


@pytest.mark.parametrize(
    ("build", "route", "where"),
    [
        (lambda: request(skill_turn("plan"), assistant(), user("Next?")), "sticky", "complex · plan"),
        (lambda: subagent_request(), "subagent", "moderate"),
        (lambda: compaction_request(skill_turn("plan"), assistant(), user("go")), "compaction", "complex · plan"),
        (lambda: request(skill_turn("plan"), assistant(), unregistered_turn()), "unregistered", "light · personal-notes"),
        (lambda: permission_check_request(), "permission", "moderate · permission"),
        (lambda: request(user("<session>fix login</session>"), tools=False), "title", "light"),
    ],
)
def test_every_request_type_is_rendered(gateway, capsys, build, route, where):
    gateway.send(request(skill_turn("plan")))  # puts the session on the complex tier
    rendered = watch.render(_spend_line(gateway, capsys, gateway.send(build())), "12:00:00", MODELS)
    assert f"{watch.ICONS[route]} {route}" in rendered
    assert where in rendered


def test_permission_check_shows_the_model_claude_code_chose(gateway, capsys):
    rendered = watch.render(_spend_line(gateway, capsys, gateway.send(permission_check_request())), "12:00:00", MODELS)
    assert "Sonnet 5" in rendered


def test_long_tier_text_is_truncated_to_the_column(gateway, capsys):
    event = {"request_type": "skill", "tier": "complex", "skill_id": "incident-postmortem", "skill_hash": "h", "alias": "complex"}
    text = watch.tier_text(event)
    assert text == "complex · incident-postmortem"
    rendered = watch.render("llm-trunk spend: " + json.dumps(event), "12:00:00", MODELS)
    _, width, _ = watch.COLUMNS[4]
    assert text[: width - 1] + "…" in rendered


def test_deny_event_is_rendered(gateway, capsys):
    capsys.readouterr()
    with pytest.raises(HTTPException):
        gateway.send(request(user("x" * 200_000)))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["code"], event["request_type"]) == ("deny", "input_cap", "normal")
    assert "light tier cap" in watch.render(line, "12:00:00", MODELS)


def test_failed_event_is_rendered(gateway, capsys):
    data = gateway.send(request(user("Hi")))
    capsys.readouterr()
    kwargs = {"litellm_params": {"metadata": data["litellm_metadata"]}, "exception": types.SimpleNamespace(status_code=529)}
    asyncio.run(gateway.callback.async_log_failure_event(kwargs, None, None, None))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["status"], event["tier"]) == ("failed", 529, "light")
    assert "529" in watch.render(line, "12:00:00", MODELS)


def test_expired_event_is_rendered(gateway, clock, capsys):
    gateway.send(request(skill_turn("plan")))
    clock.advance(10_000)
    capsys.readouterr()
    gateway.send(request(skill_turn("plan"), assistant(), user("Next?")))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["skill_id"], event["tier"]) == ("expired", "plan", "complex")
    assert "back to untagged" in watch.render(line, "12:00:00", MODELS)


def test_old_event_lines_still_render():
    # Logged before tiers: per-skill lanes, no request_type.
    old = {"skill_id": "qa-bug-logging", "skill_hash": None, "alias": "qa-bug-logging", "effort": "low",
           "session": "05887d29", "sticky_left_s": 300, "background": None, "input_tokens": 53000,
           "cache_read_tokens": 52000, "cost": 0.012}
    rendered = watch.render("llm-trunk spend: " + json.dumps(old), "11:34:16", {"qa-bug-logging": "Sonnet 5"})
    assert "sticky" in rendered and "qa-bug-logging" in rendered and "Sonnet 5 · low" in rendered


def test_watcher_ignores_unknown_fields(gateway, capsys):
    line = _spend_line(gateway, capsys, gateway.send(request(skill_turn("plan"))))
    kind, event = _event(line)
    extended = f"llm-trunk {kind}: " + json.dumps({**event, "some_future_field": 1})
    assert watch.render(extended, "12:00:00", MODELS) == watch.render(line, "12:00:00", MODELS)
