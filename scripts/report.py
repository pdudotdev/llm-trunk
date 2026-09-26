#!/usr/bin/env python3
"""Cost report for llm-trunk, built from the gateway's log.

Run on the machine hosting the gateway, from anywhere in the repo:

    python3 scripts/report.py              # everything still in the log
    python3 scripts/report.py --since 24h

Read-only: it reads the same `llm-trunk` log lines as scripts/watch.py and
totals them per request type, tier, day and session. Costs are LiteLLM's estimates
at the configured prices, not an invoice. Docker keeps the log only while the
container exists, so a `docker compose up -d` that recreates it starts over.
"""
import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watch import EVENT_RE, REPO, STAMP_RE, request_type_of  # noqa: E402

from policy.decide import REQUEST_TYPES  # noqa: E402  (watch puts the repo on the path)


def parse(lines) -> list[tuple[datetime, str, dict]]:
    """Timestamped log lines -> (local time, kind, event) for llm-trunk lines."""
    events = []
    for line in lines:
        stamp = STAMP_RE.match(line.rstrip("\n"))
        if not stamp:
            continue
        match = EVENT_RE.search(stamp.group("message"))
        if not match:
            continue
        try:
            event = json.loads(match.group("json"))
        except ValueError:
            continue
        when = datetime.strptime(stamp.group("second"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        events.append((when.astimezone(), match.group("kind"), event))
    return events


def _number(value) -> float:
    return value if isinstance(value, (int, float)) else 0


def summarize(events) -> dict:
    types = defaultdict(lambda: {"requests": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0})
    tiers = defaultdict(lambda: {"requests": 0, "cost": 0.0})
    days = defaultdict(lambda: {"requests": 0, "cost": 0.0})
    sessions = defaultdict(float)
    background = Counter()
    denies, failures = Counter(), Counter()
    estimate_errors = []
    first = last = None
    for when, kind, event in events:
        first, last = first or when, when
        if kind == "deny":
            denies[event.get("code") or "unknown"] += 1
        elif kind == "failed":
            failures[str(event.get("status") or "error")] += 1
        if kind != "spend":
            continue
        row = types[request_type_of(event)]
        cost = _number(event.get("cost"))
        # Events from before tiers carry their per-skill lane as the alias.
        tier = tiers[event.get("tier") or f"(old lane) {event.get('alias') or 'untagged'}"]
        tier["requests"] += 1
        tier["cost"] += cost
        row["requests"] += 1
        row["input"] += _number(event.get("input_tokens"))
        row["cached"] += _number(event.get("cache_read_tokens"))
        row["output"] += _number(event.get("output_tokens"))
        row["cost"] += cost
        day = days[when.date().isoformat()]
        day["requests"] += 1
        day["cost"] += cost
        sessions[event.get("session") or "?"] += cost
        if event.get("background"):
            background[event["background"]] += 1
        estimated, billed = event.get("estimated_input_tokens"), event.get("input_tokens")
        if isinstance(estimated, (int, float)) and isinstance(billed, (int, float)) and billed > 0:
            estimate_errors.append((estimated - billed) / billed)
    return {
        "first": first,
        "last": last,
        "types": dict(types),
        "tiers": dict(tiers),
        "days": dict(days),
        "sessions": dict(sessions),
        "background": dict(background),
        "denies": dict(denies),
        "failures": dict(failures),
        "estimate_errors": estimate_errors,
    }


def _tokens(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1000:.0f}k" if value >= 10_000 else f"{value / 1000:.1f}k"


def _share(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def render(summary: dict) -> str:
    if summary["first"] is None:
        return "No llm-trunk events in the log for this period."
    total_cost = sum(row["cost"] for row in summary["types"].values())
    out = [
        f"llm-trunk · cost report   {summary['first']:%Y-%m-%d %H:%M} → {summary['last']:%Y-%m-%d %H:%M}",
        "Costs are LiteLLM estimates at the configured prices, not an invoice.",
        "",
        f"{'REQUEST TYPE':<16}{'REQS':>7}{'INPUT':>8}{'CACHED':>8}{'OUTPUT':>8}{'COST':>10}{'SHARE':>7}",
    ]
    for name in (*REQUEST_TYPES, *sorted(set(summary["types"]) - set(REQUEST_TYPES))):
        row = summary["types"].get(name)
        if row is None:
            continue
        out.append(
            f"{name:<16}{row['requests']:>7}{_tokens(row['input']):>8}"
            f"{_share(row['cached'], row['input']):>8}{_tokens(row['output']):>8}"
            f"{'$' + format(row['cost'], '.3f'):>10}{_share(row['cost'], total_cost):>7}"
        )
    requests = sum(row["requests"] for row in summary["types"].values())
    out.append(f"{'TOTAL':<16}{requests:>7}{'':>8}{'':>8}{'':>8}{'$' + format(total_cost, '.3f'):>10}")
    out += ["", "By tier:"]
    for name, row in sorted(summary["tiers"].items(), key=lambda item: -item[1]["cost"]):
        out.append(f"  {name:<30} {row['requests']:>5} requests  ${row['cost']:.3f}  ({_share(row['cost'], total_cost)})")

    out += ["", "By day:"]
    for day, totals in sorted(summary["days"].items()):
        out.append(f"  {day}  {totals['requests']:>5} requests  ${totals['cost']:.3f}")

    if summary["background"]:
        kinds = ", ".join(f"{kind} ×{count}" for kind, count in sorted(summary["background"].items()))
        out += ["", f"Background calls by kind: {kinds}"]
    if summary["denies"]:
        out.append("Denied before reaching Anthropic: " + ", ".join(f"{code} ×{n}" for code, n in sorted(summary["denies"].items())))
    if summary["failures"]:
        out.append("Failed upstream: " + ", ".join(f"{status} ×{n}" for status, n in sorted(summary["failures"].items())))
    errors = summary["estimate_errors"]
    if errors:
        out.append(
            f"Input-size estimate vs billed: median {statistics.median(errors):+.0%}, "
            f"range {min(errors):+.0%} to {max(errors):+.0%} (n={len(errors)})"
        )
    top = sorted(summary["sessions"].items(), key=lambda item: -item[1])[:5]
    if top:
        out += ["", "Top sessions by cost: " + ", ".join(f"{session} ${cost:.3f}" for session, cost in top)]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cost report for llm-trunk, from the gateway's log.")
    parser.add_argument("--since", help="only this far back, e.g. 24h or 7d (default: whole log)")
    args = parser.parse_args()
    command = ["docker", "compose", "logs", "-t", "--no-log-prefix", "litellm"]
    if args.since:
        command[4:4] = ["--since", args.since]
    try:
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        sys.exit("error: docker not found — run this on the machine hosting the gateway")
    except subprocess.CalledProcessError as error:
        sys.exit(f"error: {error.stderr.strip() or error}")
    print(render(summarize(parse(result.stdout.splitlines()))))


if __name__ == "__main__":
    main()
