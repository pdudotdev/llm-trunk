#!/usr/bin/env python3
"""Check the gateway's routing decisions against llm-trunk's rules.

    python3 scripts/check_rules.py                   # everything in the gateway log
    python3 scripts/check_rules.py --since 1h
    python3 scripts/check_rules.py --results scenarios/results/<run>.json

Read-only. Exits 1 if any request broke a rule, so it can gate a scenario run
or a benchmark. Only events logged with request types are checked; older
per-skill-lane lines are counted as skipped.

The rules (see ROADMAP.md, "Design: tier-based routing"):
  normal turn            -> the session's tier (its sticky route, else the lowest)
  registered skill       -> its catalog tier
  unregistered skill     -> the lowest tier
  subagent               -> one tier below the session, never below the lowest,
                            and the same tier for all of one subagent's requests
  compaction             -> the session's tier
  suggestion / recap     -> the session's tier
  session title          -> the lowest tier
  permission check       -> the tier running the model Claude Code asked for
                            (the most capable tier if none does); effort as sent
and the model that answered must be the one configured for the tier.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report import parse  # noqa: E402
from watch import REPO, model_key  # noqa: E402

from policy.decide import lowest, one_down  # noqa: E402


def load_config() -> tuple[dict, dict[str, str]]:
    catalog = yaml.safe_load((REPO / "catalog.yaml").read_text())
    config = yaml.safe_load((REPO / "litellm" / "config.yaml").read_text())
    tier_models = {entry["model_name"]: entry["litellm_params"]["model"] for entry in config["model_list"]}
    return catalog, tier_models


CATALOG_CHANGED = "skill no longer in the catalog"


def expected_tier(event: dict, catalog: dict, agents: dict, tier_models: dict[str, str]) -> tuple[str | None, str]:
    """(tier this request should have gone to, the rule that says so)."""
    kind = event.get("request_type")
    session_tier = event.get("session_tier") or lowest(catalog)
    background = event.get("background")
    if background == "permission_check":
        # Never above the highest tier the key could reach with a skill.
        order = catalog["order"]
        ceiling = event.get("ceiling") if event.get("ceiling") in order else order[-1]
        wanted = model_key(event.get("requested_model"))
        for tier in reversed(order[: order.index(ceiling) + 1]):
            if model_key(tier_models.get(tier)) == wanted:
                return tier, "permission check -> tier running the model asked for"
        return ceiling, "permission check -> most capable tier the key can reach"
    if background == "title":
        return lowest(catalog), "session title -> lowest tier"
    if kind == "skill" and event.get("unregistered_skill"):
        return lowest(catalog), "unregistered skill -> lowest tier"
    if kind == "skill":
        row = catalog["skills"].get(event.get("skill_id"))
        if row is None:
            # Its hash was verified when it ran, so it was registered then:
            # the catalog has changed since, and this can't be judged now.
            return None, CATALOG_CHANGED
        return row["tier"], f"registered skill {event.get('skill_id')} -> catalog tier"
    if kind == "subagent":
        agent = (event.get("session"), event.get("agent"))
        if agent in agents:
            return agents[agent], "subagent keeps its tier"
        return one_down(catalog, session_tier), "subagent -> one tier below the session"
    if kind in ("compaction", "background", "normal"):
        return session_tier, f"{kind} -> session tier"
    return None, "unknown request type"


def check(events: list[tuple[datetime, str, dict]], catalog: dict, tier_models: dict[str, str]) -> dict:
    violations, checked, skipped, changed = [], 0, 0, 0
    agents: dict[tuple, str] = {}
    for when, kind, event in events:
        if kind != "spend":
            continue
        if not event.get("request_type"):
            skipped += 1
            continue
        expected, rule = expected_tier(event, catalog, agents, tier_models)
        if rule == CATALOG_CHANGED:
            changed += 1
            continue
        checked += 1
        actual = event.get("tier")
        problems = []
        if actual != expected:
            problems.append(f"expected tier {expected}, got {actual}")
        served, configured = model_key(event.get("model")), model_key(tier_models.get(actual or ""))
        if served and configured and served != configured:
            problems.append(f"tier {actual} should run {configured}, but {served} answered")
        if event.get("request_type") == "subagent" and actual:
            agents.setdefault((event.get("session"), event.get("agent")), actual)
        for problem in problems:
            violations.append({"when": when.isoformat(), "session": event.get("session"), "rule": rule, "problem": problem})
    return {"checked": checked, "skipped": skipped, "catalog_changed": changed, "violations": violations}


def results_events(path: Path) -> list[tuple[datetime, str, dict]]:
    """Events saved by scenarios/run.py."""
    data = json.loads(path.read_text())
    return [
        (datetime.fromtimestamp(event["arrived"], timezone.utc), event["kind"], event)
        for event in data["events"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Check routing decisions against llm-trunk's rules.")
    parser.add_argument("--since", help="only this far back, e.g. 1h (default: whole log)")
    parser.add_argument("--results", help="a scenarios/results/*.json file instead of the gateway log")
    args = parser.parse_args()
    if args.results:
        events = results_events(Path(args.results))
    else:
        command = ["docker", "compose", "logs", "-t", "--no-log-prefix", "litellm"]
        if args.since:
            command[4:4] = ["--since", args.since]
        output = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=True).stdout
        events = parse(output.splitlines())
    catalog, tier_models = load_config()
    result = check(events, catalog, tier_models)
    for violation in result["violations"]:
        print(f"✗ {violation['when']}  session {violation['session']}  {violation['rule']}: {violation['problem']}")
    status = "no rule violations" if not result["violations"] else f"{len(result['violations'])} rule violation(s)"
    changed = f", {result['catalog_changed']} for skills since removed from the catalog" if result["catalog_changed"] else ""
    print(f"{result['checked']} requests checked, {result['skipped']} older lines skipped{changed} — {status}")
    sys.exit(1 if result["violations"] else 0)


if __name__ == "__main__":
    main()
