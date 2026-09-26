"""The log lines the callback writes are the contract with scripts/watch.py:
each outcome must still be picked up and rendered by the watcher."""
import asyncio
import importlib.util
import json
import types

import pytest
from conftest import REPO, HTTPException, assistant, request, skill_turn, user

spec = importlib.util.spec_from_file_location("watch", REPO / "scripts" / "watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

MODELS = {"plan-lane": "Opus 5", "untagged": "Haiku 4.5"}


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
    asyncio.run(gateway.callback.async_log_success_event(kwargs, types.SimpleNamespace(usage=usage), None, None))


def test_spend_event_is_rendered(gateway, capsys):
    data = gateway.send(request(skill_turn("plan")))
    _log_spend(gateway, data)
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert kind == "spend"
    assert event["skill_id"] == "plan" and event["alias"] == "plan-lane" and event["effort"] == "high"
    assert (event["input_tokens"], event["output_tokens"], event["cache_read_tokens"]) == (41000, 250, 40000)
    assert event["cache_write_tokens"] == 500
    assert event["requested_model"] == "claude-sonnet-5"  # what the client asked for, before the lane
    assert event["cost"] == 0.0123
    rendered = watch.render(line, "12:00:00", MODELS)
    assert "invoked" in rendered and "Opus 5 · high" in rendered and "(c)" in rendered and "$0.012" in rendered


def test_sticky_spend_event_is_rendered(gateway, capsys):
    gateway.send(request(skill_turn("plan")))
    data = gateway.send(request(skill_turn("plan"), assistant(), user("Next?")))
    _log_spend(gateway, data)
    (line,) = _log_lines(capsys.readouterr().out)
    assert "sticky" in watch.render(line, "12:00:00", MODELS)


def test_deny_event_is_rendered(gateway, capsys):
    with pytest.raises(HTTPException):
        gateway.send(request(user("x" * 200_000)))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["code"]) == ("deny", "input_cap")
    assert "untagged lane cap" in watch.render(line, "12:00:00", MODELS)


def test_failed_event_is_rendered(gateway, capsys):
    data = gateway.send(request(user("Hi")))
    error = types.SimpleNamespace(status_code=529)
    kwargs = {"litellm_params": {"metadata": data["litellm_metadata"]}, "exception": error}
    asyncio.run(gateway.callback.async_log_failure_event(kwargs, None, None, None))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["status"]) == ("failed", 529)
    assert "529" in watch.render(line, "12:00:00", MODELS)


def test_expired_event_is_rendered(gateway, clock, capsys):
    gateway.send(request(skill_turn("plan")))
    clock.advance(10_000)
    gateway.send(request(skill_turn("plan"), assistant(), user("Next?")))
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    assert (kind, event["skill_id"]) == ("expired", "plan")
    assert "back to untagged" in watch.render(line, "12:00:00", MODELS)


def test_watcher_ignores_unknown_fields(gateway, capsys):
    # New fields (e.g. cache_write_tokens) must never break the watcher.
    data = gateway.send(request(skill_turn("plan")))
    _log_spend(gateway, data)
    (line,) = _log_lines(capsys.readouterr().out)
    kind, event = _event(line)
    extended = f"llm-trunk {kind}: " + json.dumps({**event, "cache_write_tokens": 500, "request_type": "skill"})
    assert watch.render(extended, "12:00:00", MODELS) == watch.render(line, "12:00:00", MODELS)
