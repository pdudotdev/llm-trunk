#!/usr/bin/env python3
"""Run a scripted Claude Code session through llm-trunk and report each step.

    python3 scenarios/run.py                  # scenarios/dev-day.yaml
    python3 scenarios/run.py --quick          # skip idle steps
    python3 scenarios/run.py --cold-start     # first wait until the prompt cache is cold
    python3 scenarios/run.py --dry-run        # list the steps, send nothing

Drives a real, interactive Claude Code session in the client repo (so it
produces the same traffic a person would, background calls included), with a
fixed session id so its requests can be picked out of the gateway log. A step
is done when its answer has been logged and the session has gone quiet.
Afterwards it prints what each step cost, what it would have cost on the
model Claude Code asked for, and whether the answer passed its check, and
saves the raw events to scenarios/results/.

Every step is also checked for the tier its main request should reach, and
the whole session is run through scripts/check_rules.py.

Costs real money on the gateway's API key (about $1-2 for dev-day).
Keep the dashboard open next to it to watch the run live.
"""
import argparse
import fcntl
import json
import os
import pty
import queue
import re
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

import check_rules  # noqa: E402
import dashboard  # noqa: E402
from report import parse  # noqa: E402
from events import pretty_model, tier_text  # noqa: E402

GATEWAY = "http://127.0.0.1:4000"
QUIET_SECONDS = 8  # a suggestion lands a few seconds after the reply
STEP_TIMEOUT = 300
RESEND_AFTER = 15  # a prompt that hasn't registered by then gets one more Enter
CACHE_SECONDS = 330  # the 5-minute prompt cache, plus a margin
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


def answered(events: list[tuple[float, str, dict]]) -> bool:
    """events: (arrival time, kind, event) for this step, oldest first."""
    return any(
        kind in ("deny", "failed") or (kind == "spend" and not event.get("background"))
        for _, kind, event in events
    )


def gateway_quiet(events: list[tuple[float, str, dict]], now: float, quiet: float = QUIET_SECONDS) -> bool:
    return not events or now - events[-1][0] >= quiet


def transcript_state(records: list[dict], since: int) -> dict:
    """What the main conversation has done since record `since`.

    Claude Code writes a `turn_duration` record when a turn ends and a
    `compact_boundary` record when compaction really ran -- firmer signals
    than timing, and the only way to tell a prompt that never registered.
    Subagent records (isSidechain) don't count.
    """
    main = [(index, record) for index, record in enumerate(records) if not record.get("isSidechain")]
    new = [record for index, record in main if index >= since]
    compacted = any(record.get("subtype") == "compact_boundary" for record in new)
    last_turn_end = max((index for index, record in main if record.get("subtype") == "turn_duration"), default=-1)
    last_message = max((index for index, record in main if record.get("type") in ("user", "assistant")), default=-1)
    return {
        "submitted": compacted or any(record.get("type") == "user" for record in new),
        "compacted": compacted,
        "idle": last_turn_end > last_message,
    }


def main_events(step: dict, spend: list[dict]) -> list[dict]:
    """The requests a step is about: its subagent's, its compaction, or the
    user's own turn -- not the background calls around it."""
    if step["type"] in ("subagent", "compaction"):
        return [event for event in spend if event.get("request_type") == step["type"]]
    return [event for event in spend if event.get("request_type") in ("normal", "skill")]


def summarize(steps: list[dict], collected: list[tuple[float, str, dict, int]], prices: dict, routes: dict) -> list[dict]:
    rows = []
    for index, step in enumerate(steps):
        events = [(kind, event) for _, kind, event, step_index in collected if step_index == index]
        spend = [event for kind, event in events if kind == "spend"]
        mains = main_events(step, spend)
        tiers = sorted({event.get("tier") for event in mains if event.get("tier")})
        tier_ok = None if "tier" not in step else (bool(mains) and tiers == [step["tier"]])
        cost = without = 0.0
        comparable = True
        places, models = [], []
        for event in spend:
            logged = event.get("cost")
            actual = float(logged) if isinstance(logged, (int, float)) else 0.0
            served = dashboard.served_model(event, routes, prices)
            baseline = dashboard.without_trunk(event, served, prices)
            cost += actual
            without += baseline if baseline is not None else actual
            comparable &= baseline is not None
            if event in mains:
                where = tier_text(event)
                model = pretty_model(served) if served else "?"
                if (where, model) not in zip(places, models):
                    places.append(where)
                    models.append(model)
        rows.append(
            {
                "step": step["name"],
                "type": step["type"],
                "routes": [f"{where} → {model}" for where, model in zip(places, models)],
                "expected_tier": step.get("tier"),
                "tiers": tiers,
                "tier_ok": tier_ok,
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


def read_records(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # the last line can be half-written
        if isinstance(record, dict):
            records.append(record)
    return records


def subagents_last_active(path: Path | None) -> float:
    """When any of the session's subagents last wrote to its transcript."""
    if path is None:
        return 0.0
    files = list(path.with_suffix("").glob("subagents/*.jsonl"))
    return max((file.stat().st_mtime for file in files), default=0.0)


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
        self.enter()

    def enter(self) -> None:
        os.write(self.master, b"\r")

    def screen(self, lines: int = 25) -> str:
        """The tail of what Claude Code last drew, escape codes stripped."""
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?", b"", bytes(self.output[-8000:]))
        return "\n".join([line for line in text.decode(errors="replace").splitlines() if line.strip()][-lines:])

    def close(self) -> None:
        try:
            os.write(self.master, b"/exit\r")
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
        os.close(self.master)


def last_gateway_activity() -> float | None:
    """When the gateway last answered a request (epoch seconds), from its log."""
    output = subprocess.run(
        ["docker", "compose", "logs", "-t", "--no-log-prefix", "--since", "15m", "litellm"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout
    spend = [when for when, kind, _ in parse(output.splitlines()) if kind == "spend"]
    return spend[-1].timestamp() if spend else None


def wait_for_cold_cache() -> float | None:
    """Claude Code's tools and system prompt are the same in every session, so
    a run started soon after other traffic reads them from cache. Waiting until
    the cache has expired makes runs comparable. Returns how long ago the
    gateway was last active when the run started (None if not in 15 min)."""
    last = last_gateway_activity()
    if last is None:
        return None
    remaining = CACHE_SECONDS - (time.time() - last)
    while remaining > 0:
        print(f"cold start: waiting {int(remaining)} s for the prompt cache to expire", flush=True)
        time.sleep(min(30, remaining))
        last = last_gateway_activity() or last
        remaining = CACHE_SECONDS - (time.time() - last)
    return time.time() - last


def gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/health/liveliness", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def run(scenario: dict, client: Path, quick: bool) -> tuple[str, list[dict], list[tuple[float, str, dict, int]], list[str]]:
    steps = [step for step in scenario["steps"] if not (quick and step["type"] == "idle")]
    session_id = str(uuid.uuid4())
    short = session_id[:8]
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    threading.Thread(target=dashboard.stream, args=(since, events, stop), daemon=True).start()

    collected: list[tuple[float, str, dict, int]] = []
    statuses: list[str] = []
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
                statuses.append("ok")
                continue
            print(f"{label}: {step['send'][:70]}", flush=True)
            first = len(collected)
            transcript = find_transcript(session_id)
            baseline = len(read_records(transcript))
            terminal.type(step["send"])
            resent, status = False, "timeout"
            while time.time() - started < STEP_TIMEOUT:
                time.sleep(0.5)
                pump()
                now = time.time()
                transcript = transcript or find_transcript(session_id)
                state = transcript_state(read_records(transcript), baseline)
                if not state["submitted"]:
                    if not resent and now - started > RESEND_AFTER:
                        terminal.enter()  # e.g. Enter only closed the command menu
                        resent = True
                        print("      prompt not registered yet, pressed Enter again", flush=True)
                    continue
                mine = [(arrived, kind, event) for arrived, kind, event, _ in collected[first:]]
                agents_quiet = now - subagents_last_active(transcript) >= QUIET_SECONDS
                if step["type"] == "compaction":
                    done = state["compacted"] and gateway_quiet(mine, now) and agents_quiet
                else:
                    done = state["idle"] and answered(mine) and gateway_quiet(mine, now) and agents_quiet
                if done:
                    status = "ok"
                    break
            else:
                state = transcript_state(read_records(transcript), baseline)
                status = "timeout" if state["submitted"] else "not run"
                print(f"      {status} after {STEP_TIMEOUT} s", flush=True)
            statuses.append(status)
            if status == "not run":
                # Claude Code isn't taking input (a dialog, a menu left open):
                # every later step would wait out its timeout too.
                print("      Claude Code's screen:\n" + "\n".join(f"        | {line}" for line in terminal.screen().splitlines()), flush=True)
                print(f"      stopping: {len(steps) - len(statuses)} steps left unrun", flush=True)
                break
        current = len(steps)  # anything still arriving belongs to no step
        time.sleep(2)
        pump()
    finally:
        terminal.close()
        stop.set()
    return session_id, steps, collected, statuses


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a scripted Claude Code session through llm-trunk.")
    parser.add_argument("scenario", nargs="?", default=str(REPO / "scenarios" / "dev-day.yaml"))
    parser.add_argument("--client", default=str(REPO.parent / "company-client"), help="repo Claude Code runs in")
    parser.add_argument("--quick", action="store_true", help="skip idle steps")
    parser.add_argument("--cold-start", action="store_true", help="first wait until the prompt cache is cold")
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

    idle_before = wait_for_cold_cache() if args.cold_start else None
    started = datetime.now()
    session_id, steps, collected, statuses = run(scenario, Path(args.client), args.quick)
    prices, routes = dashboard.load_prices(), dashboard.load_routes()
    rows = summarize(steps, collected, prices, routes)
    transcript = find_transcript(session_id)
    checks = check_answers(steps, transcript_entries(transcript)) if transcript else [None] * len(steps)
    statuses += ["not run"] * (len(rows) - len(statuses))  # steps left after a stop
    for row, check, status in zip(rows, checks, statuses):
        row["check"] = check
        row["status"] = status

    catalog, tier_models = check_rules.load_config()
    stamped = [(datetime.fromtimestamp(arrived, timezone.utc), kind, event) for arrived, kind, event, _ in collected]
    rules = check_rules.check(stamped, catalog, tier_models)
    print_table(scenario["name"], rows)
    for violation in rules["violations"]:
        print(f"✗ rule: {violation['rule']}: {violation['problem']}")
    tier_misses = [row["step"] for row in rows if row["tier_ok"] is False]
    print(f"rules: {rules['checked']} requests checked, {len(rules['violations'])} violation(s)"
          f" · tiers: {'all as expected' if not tier_misses else 'unexpected in ' + ', '.join(tier_misses)}")
    out = REPO / "scenarios" / "results" / f"{started:%Y%m%d-%H%M%S}-{scenario['name']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "scenario": scenario["name"],
        "session": session_id,
        "started": started.isoformat(),
        "quick": args.quick,
        "cold_start": args.cold_start,
        "idle_before_seconds": idle_before,
        "rule_violations": rules["violations"],
        "steps": rows,
        "events": [{"arrived": arrived, "kind": kind, "step": index, **event} for arrived, kind, event, index in collected],
    }, indent=1))
    print(f"\nsaved {out.relative_to(REPO)}")


def print_table(name: str, rows: list[dict]) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    table = Table(title=f"scenario · {name}", title_justify="left", header_style="dim")
    for column, justify in (("STEP", "left"), ("TYPE", "left"), ("ROUTED TO", "left"), ("TIER", "center"), ("REQS", "right"),
                            ("COST", "right"), ("WITHOUT", "right"), ("VS ASKED", "right"), ("CHECK", "center")):
        table.add_column(column, justify=justify)
    for row in rows:
        saving = (row["without"] - row["cost"]) / row["without"] if row["comparable"] and row["without"] else None
        requests = f"{row['requests']}" + (f" ({row['background']} bg)" if row["background"] else "")
        if row["denied"] or row["failed"]:
            requests += f" {row['denied'] + row['failed']}✗"
        check = row.get("check")
        if row.get("status", "ok") != "ok":
            check = row["status"].upper()
        check_text = Text(check or "", style={"PASS": "green"}.get(check or "", "bold red" if check else "dim"))
        tier = Text("—" if row["tier_ok"] is None else ("✓" if row["tier_ok"] else f"✗ {row['expected_tier']}"),
                    style={True: "green", False: "bold red"}.get(row["tier_ok"], "dim"))
        table.add_row(row["step"], row["type"], "\n".join(row["routes"]) or "—", tier, requests, f"${row['cost']:.3f}",
                      f"${row['without']:.3f}" if row["comparable"] else "—", dashboard.vs_asked(saving), check_text)
    cost = sum(row["cost"] for row in rows)
    without = sum(row["without"] for row in rows)
    checked = [row["check"] for row in rows if row.get("check")]
    table.add_section()
    table.add_row("TOTAL", "", "", "", str(sum(row["requests"] for row in rows)), f"${cost:.3f}", f"${without:.3f}",
                  dashboard.vs_asked((without - cost) / without if without else None),
                  f"{checked.count('PASS')}/{len(checked)}" if checked else "")
    # Piped or redirected output defaults to 80 columns, too narrow for the table.
    (Console() if sys.stdout.isatty() else Console(width=130)).print(table)


if __name__ == "__main__":
    main()
