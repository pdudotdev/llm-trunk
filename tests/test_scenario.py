"""scenarios/run.py: the parts that don't need a live Claude Code session."""
import json
import subprocess
import sys

import pytest
from conftest import REPO

sys.path.insert(0, str(REPO / "scenarios"))

import run  # noqa: E402

PRICES = run.dashboard.load_prices()
ROUTES = {"qa-test-plan-creation": "anthropic/claude-opus-5-5", "untagged": "anthropic/claude-haiku-4-5"}
STEPS = [
    {"name": "plain question", "type": "normal", "send": "In one sentence: what is a regression test?"},
    {"name": "test plan", "type": "skill", "send": "/qa-test-plan-creation Password reset by email link.", "expect": "[DEMO TEST PLAN]"},
    {"name": "idle", "type": "idle", "wait_seconds": 330},
]


def test_qa_day_scenario_is_valid():
    scenario = run.load(REPO / "scenarios" / "qa-day.yaml")
    types = {step["type"] for step in scenario["steps"]}
    assert {"normal", "skill", "subagent", "compaction", "idle"} <= types
    skills = {step["send"].split()[0][1:] for step in scenario["steps"] if step["type"] == "skill"}
    catalog = __import__("yaml").safe_load((REPO / "catalog.yaml").read_text())
    assert skills == set(catalog["skills"])  # every catalog skill is exercised


def test_dry_run_lists_steps_without_sending():
    result = subprocess.run([sys.executable, str(REPO / "scenarios" / "run.py"), "--dry-run"], capture_output=True, text=True, check=True)
    assert " 1. [normal] plain question" in result.stdout
    assert "[idle] idle past the cache: wait 330 s" in result.stdout


def test_step_waits_for_the_answer_then_quiet():
    title = (10.0, "spend", {"background": "title"})
    answer = (12.0, "spend", {"background": None})
    assert not run.step_finished([title], now=30.0)  # background call alone isn't the answer
    assert not run.step_finished([title, answer], now=15.0)  # answer in, not quiet yet
    assert run.step_finished([title, answer], now=12.0 + run.QUIET_SECONDS)
    assert run.step_finished([(5.0, "deny", {"code": "input_cap"})], now=20.0)


def _spend(**fields):
    base = {"skill_id": None, "alias": "untagged", "session": "abcd1234", "input_tokens": 40_000,
            "cache_read_tokens": 0, "cache_write_tokens": 40_000, "output_tokens": 100, "cost": 0.0505,
            "requested_model": "claude-opus-5-5", "model": "claude-haiku-4-5-20251001", "background": None}
    return {**base, **fields}


def test_summarize_groups_events_by_step():
    collected = [
        (1.0, "spend", _spend(), 0),
        (2.0, "spend", _spend(background="title", cost=0.001, input_tokens=900, cache_write_tokens=0), 0),
        (3.0, "spend", _spend(skill_id="qa-test-plan-creation", alias="qa-test-plan-creation",
                              model="claude-opus-5-5", cost=0.27), 1),
    ]
    rows = run.summarize(STEPS, collected, PRICES, ROUTES)
    assert rows[0]["requests"] == 2 and rows[0]["background"] == 1
    assert rows[0]["routes"] == ["untagged → Haiku 4.5"]
    assert rows[0]["without"] == pytest.approx((0.0505 + 0.001) * 4)  # Opus 5.5 is 4x Haiku here
    assert rows[1]["routes"] == ["qa-test-plan-creation → Opus 5.5"]
    assert rows[1]["without"] == pytest.approx(0.27)  # lane runs the model asked for
    assert rows[2]["requests"] == 0 and not rows[2]["comparable"]


def _transcript(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps({"type": role, "message": {"role": role, "content": content}}) for role, content in entries))
    return path


def test_check_answers_from_transcript(tmp_path):
    path = _transcript(tmp_path, [
        ("user", "In one sentence: what is a regression test?"),
        ("assistant", [{"type": "text", "text": "A test that checks old behavior still works."}]),
        ("user", "<command-name>/qa-test-plan-creation</command-name>\n<command-args>Password reset by email link.</command-args>"),
        ("user", [{"type": "text", "text": "Base directory for this skill: ..."}]),
        ("assistant", [{"type": "text", "text": "[DEMO TEST PLAN]\n**Scope**: ..."}]),
    ])
    assert run.check_answers(STEPS, run.transcript_entries(path)) == [None, "PASS", None]


def test_check_answers_fail_and_missing(tmp_path):
    path = _transcript(tmp_path, [
        ("user", "<command-args>Password reset by email link.</command-args>"),
        ("assistant", [{"type": "text", "text": "Here is a plan without the tag."}]),
    ])
    assert run.check_answers(STEPS, run.transcript_entries(path))[1] == "FAIL"
    empty = _transcript(tmp_path, [("user", "something else")])
    assert run.check_answers(STEPS, run.transcript_entries(empty))[1] == "?"
