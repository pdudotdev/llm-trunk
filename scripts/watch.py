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

STAMP_RE = re.compile(r"^(?P<stamp>(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*)\s+(?P<message>.*)$")
SPEND_RE = re.compile(r"llm-trunk spend: (\{.*\})\s*$")
DENY_RE = re.compile(r"llm-trunk deny \[(?P<code>\w+)\](?: session=(?P<session>\S+))?: (?P<reason>.*)$")
EXPIRED_RE = re.compile(
    r"llm-trunk: sticky route for '(?P<skill>[^']+)' expired \((?P<why>[\w-]+)\)"
    r"(?: session=(?P<session>\S+))?"
)

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
RED, GREEN, YELLOW, BLUE, MAGENTA, DIM, BOLD, ITALIC = "31", "32", "33", "34", "35", "2", "1", "3"
MODEL_COLORS = {"Opus": MAGENTA, "Sonnet": BLUE, "Haiku": GREEN}
SESSION_COLORS = ["36", "33", "35", "32", "34", "96", "93", "95"]

# Each icon is a single wide (2-cell) code point, so columns stay aligned.
ICONS = {"invoked": "🔖", "sticky": "📌", "untagged": "⚪", "denied": "⛔", "expired": "⏳"}

# (header, visible width, right-aligned)
COLUMNS = [
    ("TIME", 8, False),
    ("SESSION", 8, False),
    ("ROUTE", 15, False),
    ("MODEL · EFFORT", 18, False),
    ("LANE", 22, False),
    ("STICKY", 8, True),
    ("INPUT est→real", 14, True),
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
    match = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d+)?)(?:-\d{8})?", name)
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
    return color(code, label) + " " + color(DIM + ";" + ITALIC, "(i)") + " " * (width - len(label) - 5)


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1000:.0f}k" if value >= 10_000 else f"{value / 1000:.1f}k"


def fmt_left(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{(seconds + 59) // 60}m left" if seconds >= 60 else f"{seconds}s left"


def render_spend(clock: str, event: dict, models: dict[str, str]) -> str:
    if event.get("skill_id") is None:
        kind = "untagged"
    elif event.get("skill_hash"):
        kind = "invoked"
    else:
        kind = "sticky"
    alias = event.get("alias") or "?"
    model = models.get(alias, alias)
    cost = event.get("cost")
    return GAP.join(
        [
            cell(0, clock, DIM),
            session_cell(event.get("session")),
            route_cell(kind, BOLD if kind == "invoked" else None, background=bool(event.get("background"))),
            cell(3, f"{model} · {event['effort']}" if event.get("effort") else model, MODEL_COLORS.get(model.split(" ")[0])),
            cell(4, event.get("skill_id") or "untagged"),
            cell(5, fmt_left(event.get("sticky_left_s")), DIM if kind == "untagged" else YELLOW),
            cell(6, f"{fmt_tokens(event.get('estimated_input_tokens'))} → {fmt_tokens(event.get('input_tokens'))}"),
            cell(7, f"${cost:.3f}" if isinstance(cost, (int, float)) else "—"),
        ]
    )


def render(message: str, clock: str, models: dict[str, str]) -> str | None:
    if match := SPEND_RE.search(message):
        try:
            return render_spend(clock, json.loads(match.group(1)), models)
        except ValueError:
            return None
    if match := DENY_RE.search(message):
        reason = match.group("reason").removeprefix("llm-trunk: ")
        return GAP.join(
            [cell(0, clock, DIM), session_cell(match.group("session")),
             route_cell("denied", RED), color(RED, reason)]
        )
    if match := EXPIRED_RE.search(message):
        detail = f"{match.group('skill')} ({match.group('why')}) → back to untagged"
        return GAP.join(
            [cell(0, clock, DIM), session_cell(match.group("session")),
             route_cell("expired", YELLOW), color(YELLOW, detail)]
        )
    return None


def header() -> str:
    legend = "   ".join(f"{icon} {kind}" for kind, icon in ICONS.items()) + "   (i) Claude Code background call"
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


def follow(since: str, resume_after: str | None, models: dict[str, str]) -> str | None:
    """Stream the log; return the stamp of the last line seen when it ends."""
    command = ["docker", "compose", "logs", "-f", "-t", "--no-log-prefix", "--since", since, "litellm"]
    process = subprocess.Popen(
        command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            match = STAMP_RE.match(line.rstrip("\n"))
            if not match or (resume_after and match.group("stamp") <= resume_after):
                continue
            resume_after = match.group("stamp")
            rendered = render(match.group("message"), local_time(match.group("second")), models)
            if rendered:
                print(rendered, flush=True)
    finally:
        process.terminate()
        process.wait()
    return resume_after


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
            last = follow(since, resume_after, models)
            if last and last != resume_after:
                since = resume_after = last
                message = "gateway log ended — reconnecting"
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
