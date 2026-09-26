#!/usr/bin/env python3
"""Run a scripted Claude Code session through llm-trunk and report each step.

    python3 scenarios/run.py                  # scenarios/qa-day.yaml
    python3 scenarios/run.py --quick          # skip idle steps
    python3 scenarios/run.py --dry-run        # list the steps, send nothing

Drives a real, interactive Claude Code session in the client repo (so it
produces the same traffic a person would, background calls included), with a
fixed session id so its requests can be picked out of the gateway log. A step
is done when its answer has been logged and the session has gone quiet.
Afterwards it prints what each step cost, what it would have cost on the
model Claude Code asked for, and whether the answer passed its check, and
saves the raw events to scenarios/results/.

Costs real money on the gateway's API key (about $1-2 for qa-day).
Keep the dashboard open next to it to watch the run live.
"""
import argparse
import fcntl
import json
import os
import pty
import queue
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import dashboard  # noqa: E402
from watch import pretty_model  # noqa: E402

GATEWAY = "http://127.0.0.1:4000"
QUIET_SECONDS = 8  # a suggestion lands a few seconds after the reply
STEP_TIMEOUT = 300
# Set inside a Claude Code session; a nested `claude` would inherit them.
NESTED_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")


def load(path: Path) -> dict:
    scenario = yaml.safe_load(path.read_text())
    for step in scenario["steps"]:
        if step["type"] == "idle":
            assert isinstance(step.get("wait_seconds"), int), f"{step['name']}: idle steps need wait_seconds"
        else:
            assert step.get("send"), f"{step['name']}: needs send"
    return scenario


def step_finished(events: list[tuple[float, str, dict]], now: float, quiet: float = QUIET_SECONDS) -> bool:
    """events: (arrival time, kind, event) for this step, oldest first."""
    answered = any(
        kind in ("deny", "failed") or (kind == "spend" and not event.get("background"))
        for _, kind, event in events
    )
    return answered and now - events[-1][0] >= quiet


def summarize(steps: list[dict], collected: list[tuple[float, str, dict, int]], prices: dict, routes: dict) -> list[dict]:
    rows = []
    for index, step in enumerate(steps):
        events = [(kind, event) for _, kind, event, step_index in collected if step_index == index]
        spend = [event for kind, event in events if kind == "spend"]
        cost = without = 0.0
        comparable = True
        lanes, models = [], []
        for event in spend:
            logged = event.get("cost")
            actual = float(logged) if isinstance(logged, (int, float)) else 0.0
            served = dashboard.served_model(event, routes, prices)
            baseline = dashboard.without_trunk(event, served, prices)
            cost += actual
            without += baseline if baseline is not None else actual
            comparable &= baseline is not None
            if not event.get("background"):
                lane = event.get("skill_id") or "untagged"
                model = pretty_model(served) if served else "?"
                if (lane, model) not in zip(lanes, models):
                    lanes.append(lane)
                    models.append(model)
        rows.append(
            {
                "step": step["name"],
                "type": step["type"],
                "routes": [f"{lane} → {model}" for lane, model in zip(lanes, models)],
                "requests": len(spend),
                "background": sum(1 for event in spend if event.get("background")),
                "denied": sum(1 for kind, _ in events if kind == "deny"),
                "failed": sum(1 for kind, _ in events if kind == "failed"),
                "cost": cost,
                "without": without,
                "comparable": comparable and bool(spend),
            }
        )
    return rows


# --- Checking the answers --------------------------------------------------------


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def transcript_entries(path: Path) -> list[tuple[str, str]]:
    """(role, text) for each message in a Claude Code transcript, in order."""
    entries = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if record.get("type") in ("user", "assistant") and isinstance(message, dict):
            entries.append((record["type"], _text(message.get("content"))))
    return entries


def check_answers(steps: list[dict], entries: list[tuple[str, str]]) -> list[str | None]:
    """PASS/FAIL per step with an `expect`, None otherwise; '?' if the step's
    prompt can't be found in the transcript."""
    def marker(step: dict) -> str:
        send = step.get("send", "")
        # For a /command, look for its arguments: that's what the transcript keeps verbatim.
        text = send.split(" ", 1)[1] if send.startswith("/") and " " in send else send
        return text[:40]

    starts = []
    for step in steps:
        found = next((i for i, (role, text) in enumerate(entries) if role == "user" and step.get("send") and marker(step) in text), None)
        starts.append(found)
    results = []
    for index, step in enumerate(steps):
        if not step.get("expect"):
            results.append(None)
            continue
        start = starts[index]
        if start is None:
            results.append("?")
            continue
        later = [s for s in starts[index + 1 :] if s is not None and s > start]
        end = min(later) if later else len(entries)
        answer = "\n".join(text for role, text in entries[start:end] if role == "assistant")
        results.append("PASS" if step["expect"] in answer else "FAIL")
    return results


def find_transcript(session_id: str) -> Path | None:
    matches = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


# --- Driving Claude Code -----------------------------------------------------------


class Terminal:
    """Claude Code in a pseudo-terminal, as if a person were typing."""

    def __init__(self, command: list[str], cwd: Path, rows: int = 40, cols: int = 140) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = {k: v for k, v in os.environ.items() if k not in NESTED_ENV and not k.startswith("CLAUDE_CODE_")}
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.master = master
        self.output = bytearray()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        # Keep reading, or Claude Code blocks once the terminal buffer fills.
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.output += data
            del self.output[:-100_000]

    def type(self, text: str) -> None:
        os.write(self.master, text.encode())
        time.sleep(0.4)
        os.write(self.master, b"\r")

    def close(self) -> None:
        try:
            os.write(self.master, b"/exit\r")
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
        os.close(self.master)


def gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/health/liveliness", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def run(scenario: dict, client: Path, quick: bool) -> tuple[str, list[dict], list[tuple[float, str, dict, int]]]:
    steps = [step for step in scenario["steps"] if not (quick and step["type"] == "idle")]
    session_id = str(uuid.uuid4())
    short = session_id[:8]
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    threading.Thread(target=dashboard.stream, args=(since, events, stop), daemon=True).start()

    collected: list[tuple[float, str, dict, int]] = []
    current = 0

    def pump() -> None:
        while not events.empty():
            _, kind, event = events.get()
            if event.get("session") == short:
                collected.append((time.time(), kind, event, current))

    print(f"session {short} · {len(steps)} steps · client {client}")
    terminal = Terminal(["claude", "--session-id", session_id], client)
    try:
        time.sleep(6)  # let Claude Code start
        for current, step in enumerate(steps):
            started = time.time()
            label = f"[{current + 1}/{len(steps)}] {step['name']}"
            if step["type"] == "idle":
                print(f"{label}: waiting {step['wait_seconds']} s", flush=True)
                while time.time() - started < step["wait_seconds"]:
                    time.sleep(1)
                    pump()
                continue
            print(f"{label}: {step['send'][:70]}", flush=True)
            first = len(collected)
            terminal.type(step["send"])
            while time.time() - started < STEP_TIMEOUT:
                time.sleep(0.5)
                pump()
                mine = [(arrived, kind, event) for arrived, kind, event, _ in collected[first:]]
                if mine and step_finished(mine, time.time()):
                    break
            else:
                print(f"      timed out after {STEP_TIMEOUT} s", flush=True)
        current = len(steps)  # anything still arriving belongs to no step
        time.sleep(2)
        pump()
    finally:
        terminal.close()
        stop.set()
    return session_id, steps, collected


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a scripted Claude Code session through llm-trunk.")
    parser.add_argument("scenario", nargs="?", default=str(REPO / "scenarios" / "qa-day.yaml"))
    parser.add_argument("--client", default=str(REPO.parent / "company-client"), help="repo Claude Code runs in")
    parser.add_argument("--quick", action="store_true", help="skip idle steps")
    parser.add_argument("--dry-run", action="store_true", help="list the steps and exit")
    args = parser.parse_args()

    scenario = load(Path(args.scenario))
    if args.dry_run:
        for number, step in enumerate(scenario["steps"], 1):
            detail = f"wait {step['wait_seconds']} s" if step["type"] == "idle" else step["send"]
            print(f"{number:>2}. [{step['type']}] {step['name']}: {detail}")
        return
    if not gateway_up():
        sys.exit(f"error: gateway not reachable at {GATEWAY} — start it with docker compose up -d")

    started = datetime.now()
    session_id, steps, collected = run(scenario, Path(args.client), args.quick)
    prices, routes = dashboard.load_prices(), dashboard.load_routes()
    rows = summarize(steps, collected, prices, routes)
    transcript = find_transcript(session_id)
    checks = check_answers(steps, transcript_entries(transcript)) if transcript else [None] * len(steps)
    for row, check in zip(rows, checks):
        row["check"] = check

    print_table(scenario["name"], rows)
    out = REPO / "scenarios" / "results" / f"{started:%Y%m%d-%H%M%S}-{scenario['name']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "scenario": scenario["name"],
        "session": session_id,
        "started": started.isoformat(),
        "quick": args.quick,
        "steps": rows,
        "events": [{"arrived": arrived, "kind": kind, "step": index, **event} for arrived, kind, event, index in collected],
    }, indent=1))
    print(f"\nsaved {out.relative_to(REPO)}")


def print_table(name: str, rows: list[dict]) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    table = Table(title=f"scenario · {name}", title_justify="left", header_style="dim")
    for column, justify in (("STEP", "left"), ("TYPE", "left"), ("ROUTED TO", "left"), ("REQS", "right"),
                            ("COST", "right"), ("WITHOUT", "right"), ("VS ASKED", "right"), ("CHECK", "center")):
        table.add_column(column, justify=justify)
    for row in rows:
        saving = (row["without"] - row["cost"]) / row["without"] if row["comparable"] and row["without"] else None
        requests = f"{row['requests']}" + (f" ({row['background']} bg)" if row["background"] else "")
        if row["denied"] or row["failed"]:
            requests += f" {row['denied'] + row['failed']}✗"
        check = row.get("check")
        check_text = Text(check or "", style={"PASS": "green", "FAIL": "bold red"}.get(check or "", "dim"))
        table.add_row(row["step"], row["type"], "\n".join(row["routes"]) or "—", requests, f"${row['cost']:.3f}",
                      f"${row['without']:.3f}" if row["comparable"] else "—", dashboard.vs_asked(saving), check_text)
    cost = sum(row["cost"] for row in rows)
    without = sum(row["without"] for row in rows)
    checked = [row["check"] for row in rows if row.get("check")]
    table.add_section()
    table.add_row("TOTAL", "", "", str(sum(row["requests"] for row in rows)), f"${cost:.3f}", f"${without:.3f}",
                  dashboard.vs_asked((without - cost) / without if without else None),
                  f"{checked.count('PASS')}/{len(checked)}" if checked else "")
    Console().print(table)


if __name__ == "__main__":
    main()
