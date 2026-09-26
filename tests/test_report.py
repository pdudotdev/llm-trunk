"""scripts/report.py: totals from the same log lines the watcher reads."""
import json
import sys

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


def test_summarize_totals():
    summary = report.summarize(report.parse(LINES))
    plan = summary["lanes"]["plan"]
    assert (plan["requests"], plan["input"], plan["cached"], plan["output"]) == (3, 81900, 40000, 1510)
    assert round(plan["cost"], 3) == 0.281
    assert summary["lanes"]["untagged"]["requests"] == 1
    assert summary["background"] == {"title": 1}
    assert summary["denies"] == {"input_cap": 1}
    assert summary["failures"] == {"529": 1}
    assert len(summary["days"]) == 2
    assert round(summary["sessions"]["s1"], 3) == 0.281
    # Every spend line's estimate was 10-11% above the billed input.
    assert all(0.09 < error < 0.12 for error in summary["estimate_errors"])


def test_render_shows_the_headline_numbers():
    text = report.render(report.summarize(report.parse(LINES)), {"plan-lane": "Opus 5", "untagged": "Haiku 4.5"})
    assert "plan" in text and "$0.281" in text
    assert "TOTAL" in text and "$0.301" in text
    assert "title ×1" in text
    assert "input_cap ×1" in text and "529 ×1" in text
    assert "Input-size estimate vs billed: median +10%" in text
    assert "Top sessions by cost: s1 $0.281, s2 $0.020" in text


def test_render_with_no_events():
    assert "No llm-trunk events" in report.render(report.summarize([]), {})


def test_missing_fields_count_as_zero():
    line = _line("2026-09-23T09:00:00", "spend", skill_id=None, session="s3")
    summary = report.summarize(report.parse([line]))
    assert summary["lanes"]["untagged"] == {"requests": 1, "input": 0, "cached": 0, "output": 0, "cost": 0.0}
    assert summary["estimate_errors"] == []
