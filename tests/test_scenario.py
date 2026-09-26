"""scenarios/run.py: the parts that don't need a live Claude Code session."""
import json
import subprocess
import sys

import pytest
from conftest import REPO

sys.path.insert(0, str(REPO / "scenarios"))

import run  # noqa: E402

PRICES = run.dashboard.load_prices()
ROUTES = {"complex": "anthropic/claude-opus-5-5", "moderate": "anthropic/claude-sonnet-5", "light": "anthropic/claude-haiku-4-5"}
STEPS = [
    {"name": "plain question", "type": "normal", "tier": "light", "send": "In one sentence: what is a regression test?"},
    {"name": "design review", "type": "skill", "tier": "complex", "send": "/design-review Password reset by email link.", "expect": "[DEMO DESIGN REVIEW]"},
    {"name": "idle", "type": "idle", "wait_seconds": 330},
    {"name": "subagent", "type": "subagent", "tier": "moderate", "send": "Use a subagent to list the folders."},
]


def test_dev_day_scenario_covers_every_type_skill_and_tier():
    scenario = run.load(REPO / "scenarios" / "dev-day.yaml")
    catalog = __import__("yaml").safe_load((REPO / "catalog.yaml").read_text())
    steps = scenario["steps"]
    assert {"normal", "skill", "subagent", "compaction", "idle"} <= {step["type"] for step in steps}
    invoked = {step["send"].split()[0][1:] for step in steps if step["type"] == "skill"}
    assert set(catalog["skills"]) <= invoked  # every registered skill is exercised...
    assert invoked - set(catalog["skills"])  # ...and at least one unregistered one
    tiers = {step["tier"] for step in steps if step["type"] != "idle"}
    assert tiers == set(catalog["order"])  # every tier is reached
    for step in steps:
        if step["type"] == "skill" and step["send"].split()[0][1:] in catalog["skills"]:
            assert step["tier"] == catalog["skills"][step["send"].split()[0][1:]]["tier"], step["name"]


def test_dry_run_lists_steps_without_sending():
    result = subprocess.run([sys.executable, str(REPO / "scenarios" / "run.py"), "--dry-run"], capture_output=True, text=True, check=True)
    assert " 1. [normal] plain question" in result.stdout
    assert "[idle] idle past the cache: wait 330 s" in result.stdout
    assert "/personal-notes" in result.stdout


def test_answer_and_quiet_on_the_gateway():
    title = (10.0, "spend", {"background": "title"})
    answer = (12.0, "spend", {"background": None})
    assert not run.answered([title])  # a background call alone isn't the answer
    assert run.answered([title, answer])
    assert run.answered([(5.0, "deny", {"code": "input_cap"})])
    assert not run.gateway_quiet([title, answer], now=15.0)
    assert run.gateway_quiet([title, answer], now=12.0 + run.QUIET_SECONDS)


def _records(*kinds):
    table = {
        "user": {"type": "user"},
        "assistant": {"type": "assistant"},
        "end": {"type": "system", "subtype": "turn_duration"},
        "compact": {"type": "system", "subtype": "compact_boundary"},
        "summary": {"type": "user", "isCompactSummary": True},
        "side": {"type": "assistant", "isSidechain": True},
        "snapshot": {"type": "file-history-snapshot"},
    }
    return [table[kind] for kind in kinds]


def test_transcript_state_follows_a_turn():
    before = _records("user", "assistant", "end", "snapshot")
    baseline = len(before)
    assert run.transcript_state(before, baseline) == {"submitted": False, "compacted": False, "idle": True}
    working = before + _records("user", "assistant")
    assert run.transcript_state(working, baseline) == {"submitted": True, "compacted": False, "idle": False}
    done = working + _records("end", "snapshot")
    assert run.transcript_state(done, baseline)["idle"] is True


def test_subagent_records_do_not_end_or_start_a_turn():
    records = _records("user", "assistant", "end") + _records("side", "side")
    assert run.transcript_state(records, 3) == {"submitted": False, "compacted": False, "idle": True}


def test_compaction_is_only_confirmed_by_its_boundary_record():
    before = _records("user", "assistant", "end")
    assert not run.transcript_state(before, 3)["compacted"]
    after = before + _records("compact", "summary")
    assert run.transcript_state(after, 3) == {"submitted": True, "compacted": True, "idle": False}


def test_read_records_skips_a_half_written_last_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"type": "user"}\n{"type": "assist')
    assert run.read_records(path) == [{"type": "user"}]
    assert run.read_records(tmp_path / "missing.jsonl") == []


def _spend(**fields):
    base = {"skill_id": None, "alias": "light", "tier": "light", "request_type": "normal", "session": "abcd1234", "input_tokens": 40_000,
            "cache_read_tokens": 0, "cache_write_tokens": 40_000, "output_tokens": 100, "cost": 0.0505,
            "requested_model": "claude-opus-5-5", "model": "claude-haiku-4-5-20251001", "background": None}
    return {**base, **fields}


def test_summarize_groups_events_by_step():
    collected = [
        (1.0, "spend", _spend(), 0),
        (2.0, "spend", _spend(request_type="background", background="title", cost=0.001, input_tokens=900, cache_write_tokens=0), 0),
        (3.0, "spend", _spend(request_type="skill", skill_id="design-review", skill_hash="h", tier="complex", alias="complex",
                              model="claude-opus-5-5", cost=0.27), 1),
        (4.0, "spend", _spend(request_type="normal", skill_id="design-review", tier="complex", alias="complex",
                              model="claude-opus-5-5", cost=0.02), 3),  # the turn that launched the subagent
        (5.0, "spend", _spend(request_type="subagent", tier="moderate", alias="moderate", model="claude-sonnet-5", cost=0.03), 3),
    ]
    rows = run.summarize(STEPS, collected, PRICES, ROUTES)
    assert rows[0]["requests"] == 2 and rows[0]["background"] == 1
    assert rows[0]["routes"] == ["light → Haiku 4.5"] and rows[0]["tier_ok"] is True
    assert rows[0]["without"] == pytest.approx((0.0505 + 0.001) * 4)  # Opus 5.5 is 4x Haiku here
    assert rows[1]["routes"] == ["complex · design-review → Opus 5.5"] and rows[1]["tier_ok"] is True
    assert rows[1]["without"] == pytest.approx(0.27)  # the tier runs the model asked for
    assert rows[2]["requests"] == 0 and not rows[2]["comparable"] and rows[2]["tier_ok"] is None
    assert rows[3]["routes"] == ["moderate → Sonnet 5"] and rows[3]["tier_ok"] is True  # judged on the subagent's requests


def test_step_on_the_wrong_tier_is_flagged():
    collected = [(1.0, "spend", _spend(tier="moderate", alias="moderate", model="claude-sonnet-5"), 0)]
    assert run.summarize(STEPS[:1], collected, PRICES, ROUTES)[0]["tier_ok"] is False


def test_step_without_its_main_request_is_flagged():
    collected = [(1.0, "spend", _spend(request_type="background", background="title"), 0)]
    assert run.summarize(STEPS[:1], collected, PRICES, ROUTES)[0]["tier_ok"] is False


def _transcript(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps({"type": role, "message": {"role": role, "content": content}}) for role, content in entries))
    return path


def test_check_answers_from_transcript(tmp_path):
    path = _transcript(tmp_path, [
        ("user", "In one sentence: what is a regression test?"),
        ("assistant", [{"type": "text", "text": "A test that checks old behavior still works."}]),
        ("user", "<command-name>/design-review</command-name>\n<command-args>Password reset by email link.</command-args>"),
        ("user", [{"type": "text", "text": "Base directory for this skill: ..."}]),
        ("assistant", [{"type": "text", "text": "[DEMO DESIGN REVIEW]\n**Summary**: ..."}]),
    ])
    assert run.check_answers(STEPS, run.transcript_entries(path)) == [None, "PASS", None, None]


def test_check_answers_fail_and_missing(tmp_path):
    path = _transcript(tmp_path, [
        ("user", "<command-args>Password reset by email link.</command-args>"),
        ("assistant", [{"type": "text", "text": "Here is a plan without the tag."}]),
    ])
    assert run.check_answers(STEPS, run.transcript_entries(path))[1] == "FAIL"
    empty = _transcript(tmp_path, [("user", "something else")])
    assert run.check_answers(STEPS, run.transcript_entries(empty))[1] == "?"
