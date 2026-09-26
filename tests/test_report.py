"""scripts/report.py: totals from the same log lines the watcher reads."""
import json
import sys
import time

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "scripts"))

import report  # noqa: E402


def _line(stamp: str, kind: str, **event) -> str:
    return f"{stamp}.000000000Z llm-trunk {kind}: {json.dumps(event)}"


LINES = [
    _line("2026-09-22T10:00:00", "spend", skill_id="plan", skill_hash="abc", alias="plan-lane", session="s1",
          input_tokens=40000, cache_read_tokens=0, output_tokens=1000, cost=0.25, estimated_input_tokens=44000, background=None),
    _line("2026-09-22T10:01:00", "spend", skill_id="plan", skill_hash=None, alias="plan-lane", session="s1",
          input_tokens=41000, cache_read_tokens=40000, output_tokens=500, cost=0.03, estimated_input_tokens=45100, background=None),
    _line("2026-09-22T10:02:00", "spend", skill_id="plan", skill_hash=None, alias="plan-lane", session="s1",
          input_tokens=900, cache_read_tokens=0, output_tokens=10, cost=0.001, estimated_input_tokens=1000, background="title"),
    _line("2026-09-23T09:00:00", "spend", skill_id=None, skill_hash=None, alias="untagged", session="s2",
          input_tokens=20000, cache_read_tokens=0, output_tokens=200, cost=0.02, estimated_input_tokens=22000, background=None),
    _line("2026-09-23T09:05:00", "deny", code="input_cap", reason="too big", session="s2"),
    _line("2026-09-23T09:06:00", "failed", skill_id=None, alias="untagged", status=529, error="overloaded", session="s2"),
    _line("2026-09-23T09:07:00", "expired", skill_id="plan", why="idle", session="s1"),
    "2026-09-23T09:08:00.000000000Z some unrelated LiteLLM log line",
    "not even timestamped",
]


def test_parse_keeps_only_llm_trunk_events():
    kinds = [kind for _, kind, _ in report.parse(LINES)]
    assert kinds == ["spend", "spend", "spend", "spend", "deny", "failed", "expired"]


@pytest.fixture
def utc(monkeypatch):
    # Days are bucketed in local time; pin it so dates hold in any timezone.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_summarize_totals(utc):
    summary = report.summarize(report.parse(LINES))
    # Old-format lines: an invocation is a skill, a sticky follow-up is normal, the title is background.
    assert summary["types"]["skill"]["requests"] == 1 and round(summary["types"]["skill"]["cost"], 3) == 0.25
    normal = summary["types"]["normal"]
    assert (normal["requests"], normal["input"], normal["cached"]) == (2, 61000, 40000)
    assert summary["types"]["background"]["requests"] == 1
    assert summary["tiers"]["(old lane) plan-lane"]["requests"] == 3
    assert summary["background"] == {"title": 1}
    assert summary["denies"] == {"input_cap": 1}
    assert summary["failures"] == {"529": 1}
    assert summary["days"]["2026-09-22"]["requests"] == 3 and summary["days"]["2026-09-23"]["requests"] == 1
    assert round(summary["sessions"]["s1"], 3) == 0.281
    # Every spend line's estimate was 10-11% above the billed input.
    assert all(0.09 < error < 0.12 for error in summary["estimate_errors"])


def test_render_shows_the_headline_numbers():
    text = report.render(report.summarize(report.parse(LINES)))
    assert "REQUEST TYPE" in text and "skill" in text and "$0.250" in text
    assert "TOTAL" in text and "$0.301" in text
    assert "By tier:" in text
    assert "title ×1" in text
    assert "input_cap ×1" in text and "529 ×1" in text
    assert "Input-size estimate vs billed: median +10%" in text
    assert "Top sessions by cost: s1 $0.281, s2 $0.020" in text


def test_render_with_no_events():
    assert "No llm-trunk events" in report.render(report.summarize([]))


def test_missing_fields_count_as_zero():
    line = _line("2026-09-23T09:00:00", "spend", skill_id=None, session="s3")
    summary = report.summarize(report.parse([line]))
    assert summary["types"]["normal"] == {"requests": 1, "input": 0, "cached": 0, "output": 0, "cost": 0.0}
    assert summary["estimate_errors"] == []


def test_new_lines_group_by_request_type_and_tier():
    lines = [
        _line("2026-09-26T12:00:00", "spend", request_type="subagent", tier="moderate", session="s9", cost=0.02, input_tokens=100, output_tokens=5),
        _line("2026-09-26T12:00:01", "spend", request_type="compaction", tier="complex", session="s9", cost=0.07, input_tokens=100, output_tokens=5),
    ]
    summary = report.summarize(report.parse(lines))
    assert set(summary["types"]) == {"subagent", "compaction"}
    assert set(summary["tiers"]) == {"moderate", "complex"}


def test_token_amounts_are_readable():
    assert (report._tokens(1_500_000), report._tokens(40_000), report._tokens(900)) == ("1.5M", "40k", "0.9k")
