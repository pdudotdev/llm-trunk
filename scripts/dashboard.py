#!/usr/bin/env python3
"""Live dashboard: what llm-trunk's routing is costing and saving, as it happens.

Run on the machine hosting the gateway, from anywhere in the repo:

    python3 scripts/dashboard.py              # last hour of history, then live
    python3 scripts/dashboard.py --since 24h
    python3 scripts/dashboard.py --ttl 60m    # prompt-cache lifetime (default 5m)
    python3 scripts/dashboard.py --window 2h  # sessions shown if active this recently (default 30m)

Read-only, like scripts/watch.py, which stays the dependency-free view; this
one needs the `rich` package. "Without llm-trunk" is an estimate: each
request's own tokens priced at the model the client asked for (pricing.yaml),
so a request routed to a cheaper lane shows what the lane saved. Requests
logged before the gateway recorded the requested model can't be compared and
are counted separately.
"""
import argparse
import queue
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report import parse  # noqa: E402
from watch import ICONS, REPO, STAMP_RE, fmt_tokens, pretty_model, route_kind  # noqa: E402

MODEL_STYLES = {"Opus": "magenta", "Sonnet": "blue", "Haiku": "green", "Fable": "yellow"}
FEED_ROWS = 14
SESSION_ROWS = 6


# --- Prices ---------------------------------------------------------------------


def load_prices(path: Path = REPO / "pricing.yaml") -> dict[str, dict]:
    return yaml.safe_load(path.read_text())["models"]


def load_routes(path: Path = REPO / "litellm" / "config.yaml") -> dict[str, str]:
    """Lane alias -> the model it really calls."""
    config = yaml.safe_load(path.read_text())
    return {entry["model_name"]: entry["litellm_params"]["model"] for entry in config["model_list"]}


def model_key(model: str | None) -> str | None:
    """Any spelling of a Claude model id -> the key used in pricing.yaml."""
    if not model:
        return None
    name = model.lower().split("/")[-1].replace("[1m]", "")
    name = re.sub(r"^(?:[a-z-]+\.)?anthropic\.", "", name)  # Bedrock: us.anthropic.claude-...
    name = re.sub(r"-v\d+(?::\d+)?$", "", name)  # Bedrock: ...-v1:0
    return re.sub(r"-\d{8}$", "", name)  # dated snapshot: ...-20251001


def token_cost(event: dict, price: dict) -> float | None:
    """A request's tokens priced at one model's list prices, in USD."""
    total, output = event.get("input_tokens"), event.get("output_tokens")
    if not isinstance(total, (int, float)) or not isinstance(output, (int, float)):
        return None
    read = event.get("cache_read_tokens") or 0
    write = event.get("cache_write_tokens") or 0
    fresh = max(0, total - read - write)  # LiteLLM's input count includes both
    return (
        fresh * price["input"] + read * price["cache_read"] + write * price["cache_write"] + output * price["output"]
    ) / 1_000_000


def without_trunk(event: dict, routed_model: str | None, prices: dict) -> float | None:
    """What this request would have cost on the model the client asked for."""
    requested = prices.get(model_key(event.get("requested_model")))
    routed = prices.get(model_key(routed_model))
    if requested is None or routed is None:
        return None
    on_requested, on_routed = token_cost(event, requested), token_cost(event, routed)
    if on_requested is None or not on_routed:
        return None
    actual = event.get("cost")
    # Scale LiteLLM's actual cost by the price ratio, so both sides agree
    # when the lane runs the model the client asked for.
    return actual * on_requested / on_routed if isinstance(actual, (int, float)) else on_requested


def served_model(event: dict, routes: dict, prices: dict) -> str | None:
    """The model that answered: logged per request when it's one we can price,
    else the lane's current model from litellm/config.yaml."""
    logged = event.get("model")
    if logged and model_key(logged) in prices:
        return logged
    return routes.get(event.get("alias") or "")


# --- State -----------------------------------------------------------------------


class Dashboard:
    def __init__(self, prices: dict, routes: dict, ttl_seconds: int, clock=time.time, window_seconds: int = 1800) -> None:
        self.prices, self.routes, self.ttl, self.clock = prices, routes, ttl_seconds, clock
        # The gateway can't see a session close, only its requests: a session
        # counts as open while it has been active within this window.
        self.window = window_seconds
        self.first: datetime | None = None
        self.requests = self.denies = 0
        self.actual = self.background_cost = 0.0
        self.compared = 0
        self.compared_actual = self.compared_without = 0.0
        self.input_tokens = self.cached_tokens = 0
        self.lane_cost: dict[str, float] = defaultdict(float)
        # lane -> [actual, without llm-trunk], over comparable requests only
        self.lane_compare: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.sessions: dict[str, dict] = {}
        self.feed: deque = deque(maxlen=FEED_ROWS)

    def add(self, when: datetime, kind: str, event: dict) -> None:
        self.first = self.first or when
        saving = None
        if kind == "deny":
            self.denies += 1
        elif kind == "spend":
            saving = self._add_spend(when, event)
        self.feed.appendleft((when, kind, event, saving))

    def _add_spend(self, when: datetime, event: dict) -> float | None:
        logged = event.get("cost")
        cost = float(logged) if isinstance(logged, (int, float)) else 0.0
        lane = event.get("skill_id") or "untagged"
        routed_model = served_model(event, self.routes, self.prices)
        self.requests += 1
        self.actual += cost
        self.lane_cost[lane] += cost
        self.input_tokens += event.get("input_tokens") or 0
        self.cached_tokens += event.get("cache_read_tokens") or 0
        if event.get("background"):
            self.background_cost += cost
        if event.get("session") and isinstance(event.get("input_tokens"), (int, float)):
            self.sessions[event["session"]] = {
                "last": when.timestamp(),
                "model": routed_model,
                "context": event["input_tokens"],
            }
        baseline = without_trunk(event, routed_model, self.prices)
        if baseline is None:
            return None
        self.compared += 1
        self.compared_actual += cost
        self.compared_without += baseline
        self.lane_compare[lane][0] += cost
        self.lane_compare[lane][1] += baseline
        return (baseline - cost) / baseline if baseline else None

    @property
    def saved(self) -> float:
        return self.compared_without - self.compared_actual

    def lane_saving(self, lane: str) -> float | None:
        """Share saved on this lane vs the models asked for; negative = costs more."""
        actual, without = self.lane_compare.get(lane, (0.0, 0.0))
        return (without - actual) / without if without else None

    def cache_state(self, session: dict) -> tuple[bool, int, float | None, float | None]:
        """(warm, seconds left, next message if warm, next message if cold)."""
        left = int(session["last"] + self.ttl - self.clock())
        price = self.prices.get(model_key(session["model"]))
        if price is None:
            return left > 0, max(left, 0), None, None
        context = session["context"]
        return left > 0, max(left, 0), context * price["cache_read"] / 1e6, context * price["cache_write"] / 1e6


# --- Rendering ---------------------------------------------------------------------


def _duration(seconds: int) -> str:
    return f"{seconds // 3600}h" if seconds >= 3600 and seconds % 3600 == 0 else f"{seconds // 60} min"


def _money(value: float) -> str:
    return f"${value:,.3f}" if value < 100 else f"${value:,.0f}"


def _model_text(model: str | None, effort: str | None = None) -> Text:
    name = pretty_model(model) if model else "?"
    return Text(f"{name} · {effort}" if effort else name, style=MODEL_STYLES.get(name.split(" ")[0], ""))


def vs_asked(saving: float | None) -> Text:
    """A request's or lane's cost vs the model Claude Code asked for."""
    if saving is None or abs(saving) < 0.005:
        return Text("—", style="dim")
    if saving > 0:
        return Text(f"saved {saving:.0%}", style="green")
    return Text(f"⚠ +{-saving:.0%} cost", style="bold red")


def header(dash: Dashboard) -> Panel:
    title = Text("🔀 llm-trunk · live", style="bold")
    if dash.compared and dash.compared_without:
        share = abs(dash.saved) / dash.compared_without
        if dash.saved >= 0:
            headline = Text.assemble(
                ("SAVED ", "bold"), (_money(dash.saved), "bold green"),
                (f"  ·  {share:.0%} cheaper than the models Claude Code asked for", "green"),
            )
        else:
            headline = Text.assemble(
                ("COSTING ", "bold"), (_money(-dash.saved), "bold red"),
                (f" MORE  ·  +{share:.0%} vs the models Claude Code asked for", "red"),
            )
        detail = Text(
            f"actual {_money(dash.compared_actual)}  ·  without llm-trunk ≈ {_money(dash.compared_without)}"
            f"  ·  {dash.compared} of {dash.requests} requests comparable",
            style="dim",
        )
    else:
        headline = Text(f"SPENT {_money(dash.actual)}", style="bold")
        detail = Text("no requests with a recorded client model yet — savings appear as new traffic arrives", style="dim")
    since = f"since {dash.first:%H:%M}" if dash.first else "waiting for traffic"
    return Panel(Group(headline, detail), title=title, subtitle=Text(since, style="dim"), title_align="left")


def lanes_panel(dash: Dashboard) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    for lane, cost in sorted(dash.lane_cost.items(), key=lambda item: -item[1])[:6]:
        share = cost / dash.actual if dash.actual else 0
        bar = "█" * round(share * 8)
        table.add_row(lane[:21], Text(bar.ljust(8), style="cyan"), f"{share:.0%}", _money(cost), vs_asked(dash.lane_saving(lane)))
    table.add_row("", "", "", "", "")
    hit = dash.cached_tokens / dash.input_tokens if dash.input_tokens else 0
    table.add_row("input from cache", Text(("█" * round(hit * 8)).ljust(8), style="green"), f"{hit:.0%}", "", "")
    background = dash.background_cost / dash.actual if dash.actual else 0
    table.add_row("background calls", Text(("█" * round(background * 8)).ljust(8), style="yellow"), f"{background:.0%}", _money(dash.background_cost), "")
    table.add_row("denied", "", str(dash.denies), "", "")
    return Panel(table, title="spend by lane · vs model asked for", title_align="left")


def sessions_panel(dash: Dashboard) -> Panel:
    table = Table.grid(padding=(0, 1))
    for _ in range(4):
        table.add_column(no_wrap=True, overflow="ellipsis")
    now = dash.clock()
    active = [(session, state) for session, state in dash.sessions.items() if now - state["last"] <= dash.window]
    recent = sorted(active, key=lambda item: -item[1]["last"])[:SESSION_ROWS]
    for session, state in recent:
        warm, left, if_warm, if_cold = dash.cache_state(state)
        status = Text(f"● {left // 60}:{left % 60:02d}", style="green") if warm else Text("○ cold", style="bold red")
        if if_warm is None or if_cold is None:
            next_message = Text("")
        elif warm:
            next_message = Text(f"next {_money(if_warm)} · cold {_money(if_cold)}", style="dim")
        else:
            next_message = Text(f"next re-caches {_money(if_cold)}", style="red")
        table.add_row(Text(session, style="bold"), _model_text(state["model"]), status, next_message)
    if not recent:
        table.add_row(Text(f"no activity in the last {_duration(dash.window)}", style="dim"))
    title = f"sessions active in the last {_duration(dash.window)} · cache {_duration(dash.ttl)}"
    return Panel(table, title=title, title_align="left")


def feed_panel(dash: Dashboard) -> Panel:
    table = Table(box=None, padding=(0, 1), show_edge=False, header_style="dim")
    for name, justify in (("TIME", "left"), ("ROUTE", "left"), ("LANE", "left"), ("MODEL · EFFORT", "left"),
                          ("INPUT", "right"), ("COST", "right"), ("VS ASKED", "right")):
        table.add_column(name, justify=justify, no_wrap=True)
    for when, kind, event, saving in dash.feed:
        clock = f"{when:%H:%M:%S}"
        if kind == "spend":
            route = route_kind(event)
            label = f"{ICONS[route]} {route}" + (" (i)" if event.get("background") else "")
            cached = event.get("cache_read_tokens") or 0
            tokens = event.get("input_tokens")
            size = fmt_tokens(tokens) + (" (c)" if tokens and cached * 2 >= tokens else "")
            cost = event.get("cost")
            table.add_row(
                Text(clock, style="dim"), Text(label, style="bold" if route == "invoked" else ""),
                event.get("skill_id") or "untagged",
                _model_text(served_model(event, dash.routes, dash.prices), event.get("effort")),
                size, _money(cost) if isinstance(cost, (int, float)) else "—", vs_asked(saving),
            )
        elif kind == "deny":
            reason = str(event.get("reason", "")).removeprefix("llm-trunk: ")
            table.add_row(Text(clock, style="dim"), Text(f"{ICONS['denied']} denied", style="red"), Text(reason[:70], style="red"))
        elif kind == "failed":
            table.add_row(Text(clock, style="dim"), Text(f"{ICONS['failed']} failed", style="red"),
                          event.get("skill_id") or "untagged", Text(f"upstream {event.get('status') or 'error'}", style="red"))
        else:
            table.add_row(Text(clock, style="dim"), Text(f"{ICONS['expired']} expired", style="yellow"),
                          Text(f"{event.get('skill_id')} ({event.get('why')}) → back to untagged", style="yellow"))
    return Panel(table, title="live", title_align="left")


def render(dash: Dashboard) -> Layout:
    layout = Layout()
    layout.split_column(Layout(header(dash), size=4), Layout(name="middle", size=12), Layout(feed_panel(dash)))
    layout["middle"].split_row(Layout(lanes_panel(dash)), Layout(sessions_panel(dash)))
    return layout


# --- Log stream ---------------------------------------------------------------------


def stream(since: str, events: queue.Queue, stop: threading.Event) -> None:
    """Follow the gateway log, reconnecting when it restarts; like watch.py."""
    resume_after = None
    while not stop.is_set():
        command = ["docker", "compose", "logs", "-f", "-t", "--no-log-prefix", "--since", since, "litellm"]
        process = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                match = STAMP_RE.match(line.rstrip("\n"))
                if not match or (resume_after and match.group("stamp") <= resume_after):
                    continue
                resume_after = match.group("stamp")
                for item in parse([line]):
                    events.put(item)
                if stop.is_set():
                    break
        finally:
            process.terminate()
            process.wait()
        since = resume_after or since
        stop.wait(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live cost dashboard for llm-trunk.")
    parser.add_argument("--since", default="1h", help="history to load first (default: 1h)")
    parser.add_argument("--ttl", default="5m", choices=["5m", "60m"], help="prompt-cache lifetime (default: 5m)")
    parser.add_argument("--window", default="30m", help="show sessions active this recently, e.g. 30m or 2h (default: 30m)")
    args = parser.parse_args()

    window = int(args.window[:-1]) * (3600 if args.window.endswith("h") else 60)
    dash = Dashboard(load_prices(), load_routes(), 300 if args.ttl == "5m" else 3600, window_seconds=window)
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    threading.Thread(target=stream, args=(args.since, events, stop), daemon=True).start()
    try:
        with Live(render(dash), screen=True, auto_refresh=False, console=Console()) as live:
            while True:
                while not events.empty():
                    dash.add(*events.get())
                live.update(render(dash), refresh=True)
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
