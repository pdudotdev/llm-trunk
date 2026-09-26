#!/usr/bin/env python3
"""Live view of llm-trunk routing decisions, read from the gateway's log.

Run on the machine hosting the gateway, from anywhere in the repo:

    python3 scripts/watch.py              # last 10 minutes, then live
    python3 scripts/watch.py --since 1h

Read-only: it follows `docker compose logs litellm` and renders the
`llm-trunk` lines, reconnecting if the gateway restarts. Set NO_COLOR=1 to
disable colors.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from policy.models import model_key  # noqa: E402  (re-exported for the other scripts)

STAMP_RE = re.compile(r"^(?P<stamp>(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*)\s+(?P<message>.*)$")
# The gateway writes one JSON line per outcome: "llm-trunk <kind>: {...}".
EVENT_RE = re.compile(r"llm-trunk (?P<kind>spend|deny|expired|failed): (?P<json>\{.*\})\s*$")

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
RED, GREEN, YELLOW, BLUE, MAGENTA, DIM, BOLD, ITALIC = "31", "32", "33", "34", "35", "2", "1", "3"
MODEL_COLORS = {"Opus": MAGENTA, "Sonnet": BLUE, "Haiku": GREEN}
SESSION_COLORS = ["36", "33", "35", "32", "34", "96", "93", "95"]

# Each icon is a single wide (2-cell) code point, so columns stay aligned.
ICONS = {
    "invoked": "🔖", "sticky": "📌", "untagged": "⚪",
    "unregistered": "🔹", "subagent": "🤖", "compaction": "🧹", "permission": "🔒", "title": "📛",
    "denied": "⛔", "failed": "❌", "expired": "⏳",
}

# (header, visible width, right-aligned)
COLUMNS = [
    ("TIME", 8, False),
    ("SESSION", 8, False),
    ("ROUTE", 15, False),
    ("MODEL · EFFORT", 18, False),
    ("TIER · SKILL", 22, False),
    ("STICKY", 8, True),
    ("INPUT", 9, True),
    ("COST", 7, True),
]
GAP = "  "


def color(code: str | None, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR and code else text


def cell(index: int, text: str, code: str | None = None, *, wide_chars: int = 0) -> str:
    # Pad to the column's visible width (a wide icon takes 2 cells, 1 char).
    _, width, right = COLUMNS[index]
    width -= wide_chars
    return color(code, text.rjust(width) if right else text.ljust(width))


def pretty_model(model_id: str) -> str:
    # "anthropic/claude-haiku-4-5" -> "Haiku 4.5"; anything else as-is.
    name = model_id.split("/")[-1]
    # The minor version is 1-2 digits, so a trailing -YYYYMMDD date isn't
    # mistaken for one ("claude-opus-4-20250514" -> "Opus 4").
    match = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?", name)
    if not match:
        return name
    return f"{match.group(1).capitalize()} {match.group(2).replace('-', '.')}"


def load_models() -> dict[str, str]:
    # Alias -> display name, from litellm/config.yaml (stdlib-only parse).
    models, alias = {}, None
    for line in (REPO / "litellm" / "config.yaml").read_text().splitlines():
        if match := re.match(r"\s*- model_name:\s*(\S+)", line):
            alias = match.group(1)
        elif (match := re.match(r"\s+model:\s*(\S+)", line)) and alias:
            models[alias] = pretty_model(match.group(1))
            alias = None
    return models


def local_time(second: str) -> str:
    utc = datetime.strptime(second, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return utc.astimezone().strftime("%H:%M:%S")


def session_cell(session: str | None) -> str:
    if not session:
        return cell(1, "—", DIM)
    return cell(1, session, SESSION_COLORS[zlib.crc32(session.encode()) % len(SESSION_COLORS)])


def route_cell(kind: str, code: str | None = None, *, background: bool = False) -> str:
    # "(i)" marks Claude Code's own background calls (session title, away
    # summary): they never refresh a sticky timer.
    label = f"{ICONS[kind]} {kind}"
    if not background:
        return cell(2, label, code, wide_chars=1)
    _, width, _ = COLUMNS[2]
    visible = len(label) + 1 + len(" (i)")  # the icon is 2 cells wide
    return color(code, label) + " " + color(DIM + ";" + ITALIC, "(i)") + " " * max(0, width - visible)


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1000:.0f}k" if value >= 10_000 else f"{value / 1000:.1f}k"


def input_cell(tokens: int | None, cached: int | None) -> str:
    # "(c)": most of the input was read from Anthropic's prompt cache (billed
    # at ~10%). In practice a request is either ~0% or ~90-100% cached.
    _, width, _ = COLUMNS[6]
    value = fmt_tokens(tokens).rjust(width - 4)
    if tokens and cached and cached * 2 >= tokens:
        return value + " " + color(DIM, "(c)")
    return value + "    "


def fmt_left(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{(seconds + 59) // 60}m left" if seconds >= 60 else f"{seconds}s left"


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


def model_cells(event: dict, models: dict[str, str]) -> list[str]:
    # The model that answered, as logged; the tier's current model only for
    # old lines that didn't log one.
    alias = event.get("alias") or "?"
    model = pretty_model(event["model"]) if event.get("model") else models.get(alias) or pretty_model(alias)
    text = f"{model} · {event['effort']}" if event.get("effort") else model
    _, width, _ = COLUMNS[4]
    where = tier_text(event)
    return [
        cell(3, text, MODEL_COLORS.get(model.split(" ")[0])),
        cell(4, where if len(where) <= width else where[: width - 1] + "…"),
    ]


def render_spend(clock: str, event: dict, models: dict[str, str]) -> str:
    kind = route_kind(event)
    cost = event.get("cost")
    return GAP.join(
        [
            cell(0, clock, DIM),
            session_cell(event.get("session")),
            route_cell(kind, BOLD if kind == "invoked" else None, background=bool(event.get("background"))),
            *model_cells(event, models),
            cell(5, fmt_left(event.get("sticky_left_s")), DIM if kind == "untagged" else YELLOW),
            input_cell(event.get("input_tokens"), event.get("cache_read_tokens")),
            cell(7, f"${cost:.3f}" if isinstance(cost, (int, float)) else "—"),
        ]
    )


def render(message: str, clock: str, models: dict[str, str]) -> str | None:
    match = EVENT_RE.search(message)
    if not match:
        return None
    try:
        event = json.loads(match.group("json"))
    except ValueError:
        return None
    kind = match.group("kind")
    background = bool(event.get("background"))
    lead = [cell(0, clock, DIM), session_cell(event.get("session"))]
    if kind == "spend":
        return render_spend(clock, event, models)
    if kind == "deny":
        reason = str(event.get("reason", "")).removeprefix("llm-trunk: ")
        return GAP.join([*lead, route_cell("denied", RED, background=background), color(RED, reason)])
    if kind == "failed":
        status = event.get("status")
        error = f"{status}: " if status else ""
        error += " ".join(str(event.get("error") or "upstream error").split())[:120]
        return GAP.join(
            [*lead, route_cell("failed", RED, background=background), *model_cells(event, models), color(RED, error)]
        )
    detail = f"{event.get('skill_id')} ({event.get('why')}) → back to untagged"
    return GAP.join([*lead, route_cell("expired", YELLOW), color(YELLOW, detail)])


def header() -> str:
    legend = "   ".join(f"{icon} {kind}" for kind, icon in ICONS.items()) + "   (i) background call   (c) cached input"
    columns = GAP.join(
        name.rjust(width) if right else name.ljust(width) for name, width, right in COLUMNS
    )
    return "\n".join(
        [
            color(BOLD, "🔀 llm-trunk · live routing") + color(DIM, "   Ctrl+C to stop"),
            color(DIM, legend),
            "",
            color(DIM, columns),
            color(DIM, "─" * len(columns)),
        ]
    )


def follow(since: str, resume_after: str | None, models: dict[str, str]) -> tuple[str | None, str | None]:
    """Stream the log; return the last stamp seen and docker's last error line."""
    command = ["docker", "compose", "logs", "-f", "-t", "--no-log-prefix", "--since", since, "litellm"]
    process = subprocess.Popen(
        command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert process.stdout is not None
    last_error = None
    try:
        for line in process.stdout:
            match = STAMP_RE.match(line.rstrip("\n"))
            if not match:
                # Log lines are always timestamped (-t); anything else is
                # docker itself talking -- e.g. an error.
                last_error = line.strip() or last_error
                continue
            if resume_after and match.group("stamp") <= resume_after:
                continue
            resume_after = match.group("stamp")
            rendered = render(match.group("message"), local_time(match.group("second")), models)
            if rendered:
                print(rendered, flush=True)
    finally:
        process.terminate()
        process.wait()
    return resume_after, last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Live view of llm-trunk routing decisions.")
    parser.add_argument("--since", default="10m", help="show history this far back (default: 10m)")
    args = parser.parse_args()

    models = load_models()
    print(header(), flush=True)
    since, resume_after, notice = args.since, None, None
    try:
        while True:
            # The stream ends when the gateway restarts (or isn't up yet):
            # resume after the last line shown, so nothing repeats or is lost.
            last, error = follow(since, resume_after, models)
            if last and last != resume_after:
                since = resume_after = last
                message = "gateway log ended — reconnecting"
            elif error:
                message = f"docker: {error}"
            else:
                message = "waiting for the gateway (docker compose up -d)"
            if message != notice:
                print(color(DIM, f"   … {message}"), flush=True)
                notice = message
            time.sleep(2)
    except KeyboardInterrupt:
        print()
    except FileNotFoundError:
        sys.exit("error: docker not found — run this on the machine hosting the gateway")


if __name__ == "__main__":
    main()
