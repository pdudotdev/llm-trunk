"""The gateway's log format: the `llm-trunk` event lines and how to read them.

Shared by the dashboard, the report, the rule checker and the scenario runner.
The callback writes one JSON line per outcome (policy/litellm_callback.py,
`_log_event`); tests/test_events.py keeps both sides in step.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # the other scripts import policy.* after importing this

STAMP_RE = re.compile(r"^(?P<stamp>(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*)\s+(?P<message>.*)$")
# The gateway writes one JSON line per outcome: "llm-trunk <kind>: {...}".
EVENT_RE = re.compile(r"llm-trunk (?P<kind>spend|deny|expired|failed): (?P<json>\{.*\})\s*$")

# Each icon is a single wide (2-cell) code point, so columns stay aligned.
ICONS = {
    "invoked": "🔖", "sticky": "📌", "untagged": "⚪",
    "unregistered": "🔹", "subagent": "🤖", "compaction": "🧹", "permission": "🔒", "title": "📛",
    "denied": "⛔", "failed": "❌", "expired": "⏳",
}


def pretty_model(model_id: str) -> str:
    # "anthropic/claude-haiku-4-5" -> "Haiku 4.5"; anything else as-is.
    name = model_id.split("/")[-1]
    # The minor version is 1-2 digits, so a trailing -YYYYMMDD date isn't
    # mistaken for one ("claude-opus-4-20250514" -> "Opus 4").
    match = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?", name)
    if not match:
        return name
    return f"{match.group(1).capitalize()} {match.group(2).replace('-', '.')}"


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1000:.0f}k" if value >= 10_000 else f"{value / 1000:.1f}k"


def route_kind(event: dict) -> str:
    # Events logged before request types existed have no request_type.
    request_type = event.get("request_type")
    if request_type == "subagent":
        return "subagent"
    if request_type == "compaction":
        return "compaction"
    if event.get("background") == "permission_check":
        return "permission"
    if event.get("background") == "title":
        return "title"
    if request_type == "skill" and event.get("unregistered_skill"):
        return "unregistered"
    if event.get("skill_id") is None:
        return "untagged"
    return "invoked" if event.get("skill_hash") else "sticky"


def request_type_of(event: dict) -> str:
    """normal / skill / subagent / compaction / background; events logged
    before request types existed are classified from what they do carry."""
    if event.get("request_type"):
        return event["request_type"]
    if event.get("background"):
        return "background"
    return "skill" if event.get("skill_hash") else "normal"


def tier_text(event: dict) -> str:
    """Where the request went: the tier, plus the skill that put it there."""
    tier = event.get("tier")
    if event.get("background") == "permission_check":
        return f"{tier} · permission" if tier else "passed through"
    skill = event.get("unregistered_skill") or event.get("skill_id")
    if not tier:
        # Old events (per-skill lanes), or a passed-through request.
        return skill or "untagged"
    return f"{tier} · {skill}" if skill else tier
