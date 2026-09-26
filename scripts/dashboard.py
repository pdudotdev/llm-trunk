#!/usr/bin/env python3
"""Live dashboard: what llm-trunk's routing is costing and saving, as it happens.

Run on the machine hosting the gateway, from anywhere in the repo:

    python3 scripts/dashboard.py              # starts empty: only traffic from now on
    python3 scripts/dashboard.py --since 24h  # include the last 24 hours of history
    python3 scripts/dashboard.py --ttl 60m    # prompt-cache lifetime (default 5m)
    python3 scripts/dashboard.py --window 2h  # sessions shown if active this recently (default 30m)

Read-only; needs `rich` and `pyyaml` (pip install -r requirements-dev.txt).
Scroll the live feed with the arrow keys or the mouse wheel (or j/k), a page
with space/b, jump to the newest with g and the oldest with G; q quits.

"Without llm-trunk" is an estimate: each request's own tokens priced at the
model the client asked for (pricing.yaml), so a request routed to a cheaper
tier shows what the tier saved. Requests logged before the gateway recorded
the requested model can't be compared and are counted separately.
"""
import argparse
import itertools
import os
import queue
import re
import select
import subprocess
import sys
import threading
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timezone
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

from events import ICONS, REPO, STAMP_RE, fmt_tokens, pretty_model, request_type_of, route_kind  # noqa: E402

from policy.decide import REQUEST_TYPES  # noqa: E402  (events puts the repo on the path)
from policy.models import model_key  # noqa: E402

MODEL_STYLES = {"Opus": "magenta", "Sonnet": "blue", "Haiku": "green", "Fable": "yellow"}
SESSION_STYLES = ["cyan", "yellow", "magenta", "green", "blue", "bright_cyan", "bright_yellow", "bright_magenta"]
# Request types as the dashboard names them: Claude Code's own housekeeping
# (titles, suggestions, away summaries, permission checks) reads as "internal".
TYPE_LABELS = {"background": "internal"}
FEED_HISTORY = 10_000  # rows kept for scrolling back
SESSION_ROWS = 6
HEADER_HEIGHT, MIDDLE_HEIGHT = 4, 12
TYPES_WIDTH = 54  # the spend panel's widest line; the sessions panel gets the rest


# --- Prices ---------------------------------------------------------------------


def load_prices(path: Path = REPO / "pricing.yaml") -> dict[str, dict]:
    return yaml.safe_load(path.read_text())["models"]


def load_routes(path: Path = REPO / "litellm" / "config.yaml") -> dict[str, str]:
    """Tier -> the model it really calls."""
    config = yaml.safe_load(path.read_text())
    return {entry["model_name"]: entry["litellm_params"]["model"] for entry in config["model_list"]}


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
    # when the tier runs the model the client asked for.
    return actual * on_requested / on_routed if isinstance(actual, (int, float)) else on_requested


def served_model(event: dict, routes: dict, prices: dict) -> str | None:
    """The model that answered: logged per request when it's one we can price,
    else the tier's current model from litellm/config.yaml."""
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
        self.actual = 0.0
        self.compared = 0
        self.compared_actual = self.compared_without = 0.0
        self.input_tokens = self.cached_tokens = 0
        self.type_cost: dict[str, float] = defaultdict(float)
        # request type -> [actual, without llm-trunk], over comparable requests only
        self.type_compare: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.sessions: dict[str, dict] = {}
        # session -> (skill, epoch when its sticky route expires if left idle)
        self.sticky: dict[str, tuple[str, float]] = {}
        self.feed: deque = deque(maxlen=FEED_HISTORY)
        # The feed lists newest first; scroll = how many newer rows are hidden
        # above the view. page = rows the view showed last time it was drawn.
        self.scroll = self.unseen = 0
        self.page = 1

    def add(self, when: datetime, kind: str, event: dict) -> None:
        self.first = self.first or when
        saving = None
        if kind == "deny":
            self.denies += 1
        elif kind == "spend":
            saving = self._add_spend(when, event)
        elif kind == "expired":
            self.sticky.pop(event.get("session"), None)
        self.feed.appendleft((when, kind, event, saving))
        if self.scroll:
            # Scrolled back: keep the rows in view where they are.
            self.scroll = min(self.scroll + 1, self.max_scroll())
            self.unseen += 1

    def max_scroll(self) -> int:
        return max(0, len(self.feed) - self.page)

    def press(self, key: str) -> None:
        """Scroll the feed: up/down a row, pgup/pgdn a page, home/end to either end."""
        steps = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page}
        if key in steps:
            self.scroll += steps[key]
        elif key == "home":
            self.scroll = 0
        elif key == "end":
            self.scroll = self.max_scroll()
        self.scroll = max(0, min(self.scroll, self.max_scroll()))
        if not self.scroll:
            self.unseen = 0

    def _track_sticky(self, when: datetime, event: dict) -> None:
        # Every request but a subagent's reports the session's sticky timer:
        # its time left, or none once the route has ended.
        session = event.get("session")
        if not session or event.get("request_type") == "subagent":
            return
        left, skill = event.get("sticky_left_s"), event.get("skill_id")
        if isinstance(left, (int, float)) and skill:
            self.sticky[session] = (skill, when.timestamp() + left)
        else:
            self.sticky.pop(session, None)

    def sticky_left(self, session: str) -> tuple[str, int] | None:
        """(skill, seconds left) while the session's sticky route is live."""
        if session not in self.sticky:
            return None
        skill, expires = self.sticky[session]
        left = int(expires - self.clock())
        return (skill, left) if left > 0 else None

    def _add_spend(self, when: datetime, event: dict) -> float | None:
        logged = event.get("cost")
        cost = float(logged) if isinstance(logged, (int, float)) else 0.0
        kind = request_type_of(event)
        routed_model = served_model(event, self.routes, self.prices)
        self.requests += 1
        self.actual += cost
        self.type_cost[kind] += cost
        self.input_tokens += event.get("input_tokens") or 0
        self.cached_tokens += event.get("cache_read_tokens") or 0
        self._track_sticky(when, event)
        # The cache clock and "next message" price follow the main conversation:
        # subagents, titles and permission checks share the session but run
        # another model on another context.
        main_turn = kind in ("normal", "skill") and not event.get("background")
        if main_turn and event.get("session") and isinstance(event.get("input_tokens"), (int, float)):
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
        self.type_compare[kind][0] += cost
        self.type_compare[kind][1] += baseline
        return (baseline - cost) / baseline if baseline else None

    @property
    def saved(self) -> float:
        return self.compared_without - self.compared_actual

    def type_saving(self, kind: str) -> float | None:
        """Share saved on this request type vs the models asked for; negative = costs more."""
        actual, without = self.type_compare.get(kind, (0.0, 0.0))
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


def _model_text(model: str | None) -> Text:
    name = pretty_model(model) if model else "?"
    return Text(name, style=MODEL_STYLES.get(name.split(" ")[0], ""))


def type_text(event: dict) -> str:
    """The request type, with the skill's name when the request invoked one."""
    kind = request_type_of(event)
    label = TYPE_LABELS.get(kind, kind)
    skill = event.get("unregistered_skill") or event.get("skill_id")
    return f"{label} ({skill})" if kind == "skill" and skill else label


def vs_asked(saving: float | None) -> Text:
    """A request's or request type's cost vs the model Claude Code asked for."""
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


def types_panel(dash: Dashboard) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    for kind in REQUEST_TYPES:
        cost = dash.type_cost.get(kind)
        if cost is None:
            continue
        share = cost / dash.actual if dash.actual else 0
        bar = "█" * round(share * 8)
        table.add_row(TYPE_LABELS.get(kind, kind), Text(bar.ljust(8), style="cyan"), f"{share:.0%}", _money(cost), vs_asked(dash.type_saving(kind)))
    table.add_row("", "", "", "", "")
    hit = dash.cached_tokens / dash.input_tokens if dash.input_tokens else 0
    table.add_row("input from cache", Text(("█" * round(hit * 8)).ljust(8), style="green"), f"{hit:.0%}", "", "")
    table.add_row("denied", "", str(dash.denies), "", "")
    return Panel(table, title="spend by request type · vs model asked for", title_align="left")


def _session_text(session: str | None) -> Text:
    if not session:
        return Text("—", style="dim")
    return Text(session, style=SESSION_STYLES[zlib.crc32(session.encode()) % len(SESSION_STYLES)])


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def sessions_panel(dash: Dashboard) -> Panel:
    table = Table(box=None, padding=(0, 1), show_edge=False, header_style="dim")
    for name in ("SESSION", "MODEL", "CACHE", "NEXT MESSAGE"):
        table.add_column(name, no_wrap=True, overflow="ellipsis")
    # The one column that gives way on a narrow terminal (its text still won't wrap).
    table.add_column("STICKY", no_wrap=False)
    now = dash.clock()
    active = [(session, state) for session, state in dash.sessions.items() if now - state["last"] <= dash.window]
    recent = sorted(active, key=lambda item: -item[1]["last"])[:SESSION_ROWS]
    for session, state in recent:
        warm, left, if_warm, if_cold = dash.cache_state(state)
        status = Text(f"● {_clock(left)}", style="green") if warm else Text("○ cold", style="bold red")
        if if_warm is None or if_cold is None:
            next_message = Text("")
        elif warm:
            next_message = Text(f"{_money(if_warm)} · cold {_money(if_cold)}", style="dim")
        else:
            next_message = Text(f"next re-cache costs {_money(if_cold)}", style="red")
        sticky = dash.sticky_left(session)
        sticky_text = Text(f"{ICONS['sticky']} {_clock(sticky[1])} {sticky[0]}" if sticky else "—",
                           style="yellow" if sticky else "dim", no_wrap=True, overflow="ellipsis")
        table.add_row(_session_text(session), _model_text(state["model"]), status, next_message, sticky_text)
    if not recent:
        table.add_row(Text(f"no activity in the last {_duration(dash.window)}", style="dim"))
    title = f"sessions active in the last {_duration(dash.window)} · cache {_duration(dash.ttl)}"
    return Panel(table, title=title, title_align="left")


# (header, right-aligned)
FEED_COLUMNS = (("TIME", False), ("SESSION", False), ("ROUTE", False), ("REQ TYPE", False), ("TIER", False),
                ("MODEL", False), ("EFFORT", False), ("INPUT", True), ("COST", True), ("VS ASKED", True))
GAP = "  "


def feed_row(dash: Dashboard, when: datetime, kind: str, event: dict, saving: float | None) -> tuple[list, Text | None]:
    """(the row's leading cells, the detail that runs on to the end of the line)."""
    lead = [Text(f"{when:%H:%M:%S}", style="dim"), _session_text(event.get("session"))]
    if kind == "spend":
        route = route_kind(event)
        label = f"{ICONS[route]} {route}" + (" (i)" if event.get("background") else "")
        cached = event.get("cache_read_tokens") or 0
        tokens = event.get("input_tokens")
        size = fmt_tokens(tokens) + (" (c)" if tokens and cached * 2 >= tokens else "")
        cost = event.get("cost")
        effort = event.get("effort")
        return [
            *lead, Text(label, style="bold" if route == "invoked" else ""),
            type_text(event), event.get("tier") or Text("—", style="dim"),
            _model_text(served_model(event, dash.routes, dash.prices)), effort or Text("—", style="dim"),
            size, _money(cost) if isinstance(cost, (int, float)) else "—", vs_asked(saving),
        ], None
    if kind == "deny":
        reason = str(event.get("reason", "")).removeprefix("llm-trunk: ")
        return [*lead, Text(f"{ICONS['denied']} denied", style="red"), type_text(event)], Text(reason, style="red")
    if kind == "failed":
        error = " ".join(str(event.get("error") or "").split())
        detail = f"upstream {event.get('status') or 'error'}" + (f": {error}" if error else "")
        cells = [*lead, Text(f"{ICONS['failed']} failed", style="red"), type_text(event), event.get("tier") or ""]
        return cells, Text(detail, style="red")
    detail = f"{event.get('skill_id')} ({event.get('why')}) → back to untagged"
    return [*lead, Text(f"{ICONS['expired']} expired", style="yellow"), "", event.get("tier") or ""], Text(detail, style="yellow")


def feed_lines(rows: list[tuple[list, Text | None]]) -> list[Text]:
    """Lay the rows out in columns sized to what's shown. A row's detail starts
    after its last cell and runs on; the panel cuts off whatever doesn't fit."""
    rows = [([cell if isinstance(cell, Text) else Text(cell) for cell in cells], detail) for cells, detail in rows]
    widths = [len(name) for name, _ in FEED_COLUMNS]
    for cells, _ in rows:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], cell.cell_len)
    header = [(Text(name, style="dim"), right) for name, right in FEED_COLUMNS]
    lines = []
    for cells, detail in [([cell for cell, _ in header], None), *rows]:
        line = Text(no_wrap=True, overflow="ellipsis")
        for i, cell in enumerate(cells):
            pad = " " * (widths[i] - cell.cell_len)
            line.append_text(Text(pad) + cell if FEED_COLUMNS[i][1] else cell + Text(pad))
            if i < len(cells) - 1 or detail:
                line.append(GAP)
        if detail:
            line.append_text(detail)
        line.rstrip()
        lines.append(line)
    return lines


def feed_panel(dash: Dashboard, rows: int) -> Panel:
    dash.page = max(1, rows)
    dash.scroll = min(dash.scroll, dash.max_scroll())
    shown = list(itertools.islice(dash.feed, dash.scroll, dash.scroll + dash.page))
    lines = feed_lines([feed_row(dash, *item) for item in shown])
    total = len(dash.feed)
    if dash.scroll:
        title = f"live · paused · rows {dash.scroll + 1}–{dash.scroll + len(shown)} of {total}"
        if dash.unseen:
            title += f" · {dash.unseen} new above"
        subtitle = Text("g: back to live", style="bold yellow")
    else:
        title = f"live · {total} rows" if total > len(shown) else "live"
        subtitle = Text("↑↓/wheel: scroll · space/b: page · g/G: newest/oldest · q: quit", style="dim")
    return Panel(Group(*lines), title=title, subtitle=subtitle, title_align="left", subtitle_align="right")


def render(dash: Dashboard, height: int) -> Layout:
    # The feed gets whatever height is left: its panel border and header row
    # take 3 lines, the rest are request rows.
    feed_rows = height - HEADER_HEIGHT - MIDDLE_HEIGHT - 3
    layout = Layout()
    layout.split_column(
        Layout(header(dash), size=HEADER_HEIGHT), Layout(name="middle", size=MIDDLE_HEIGHT), Layout(feed_panel(dash, feed_rows))
    )
    layout["middle"].split_row(Layout(types_panel(dash), size=TYPES_WIDTH), Layout(sessions_panel(dash)))
    return layout


# --- Log stream ---------------------------------------------------------------------


def stream(since: str, events: queue.Queue, stop: threading.Event) -> None:
    """Follow the gateway log, reconnecting when it restarts without repeating a line."""
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


# Key presses -> dashboard actions. Terminals send the mouse wheel in the
# full-screen view as arrow keys; the letter keys cover a laptop keyboard
# whose Page Up/Home/End the terminal keeps for itself.
KEYS = {
    "\x1b[A": "up", "\x1bOA": "up", "k": "up",
    "\x1b[B": "down", "\x1bOB": "down", "j": "down",
    "\x1b[5~": "pgup", "b": "pgup",
    "\x1b[6~": "pgdn", " ": "pgdn",
    "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home", "g": "home",
    "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "G": "end",
    "q": "quit",
}


def parse_keys(data: str) -> list[str]:
    """Raw terminal input -> action names; anything else is skipped."""
    keys, i = [], 0
    while i < len(data):
        match = next((seq for seq in sorted(KEYS, key=len, reverse=True) if data.startswith(seq, i)), None)
        if match:
            keys.append(KEYS[match])
            i += len(match)
        else:
            i += 1
    return keys


def read_keys(fd: int, keys: queue.Queue, stop: threading.Event) -> None:
    while not stop.is_set():
        if select.select([fd], [], [], 0.2)[0]:
            for key in parse_keys(os.read(fd, 1024).decode(errors="ignore")):
                keys.put(key)


def duration(text: str) -> int:
    """'30m' / '2h' -> seconds; anything else is an error, not a guess."""
    match = re.fullmatch(r"(\d+)([mh])", text.strip())
    if not match or int(match.group(1)) == 0:
        raise argparse.ArgumentTypeError(f"{text!r}: use minutes or hours, e.g. 30m or 2h")
    return int(match.group(1)) * (60 if match.group(2) == "m" else 3600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live cost dashboard for llm-trunk.")
    parser.add_argument("--since", help="history to load first, e.g. 1h (default: none, start fresh)")
    parser.add_argument("--ttl", default="5m", choices=["5m", "60m"], help="prompt-cache lifetime (default: 5m)")
    parser.add_argument("--window", default="30m", type=duration, help="show sessions active this recently, e.g. 30m or 2h (default: 30m)")
    args = parser.parse_args()

    dash = Dashboard(load_prices(), load_routes(), 300 if args.ttl == "5m" else 3600, window_seconds=args.window)
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    # Without --since, start fresh: earlier sessions would only confuse a new run.
    since = args.since or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    threading.Thread(target=stream, args=(since, events, stop), daemon=True).start()
    keys: queue.Queue = queue.Queue()
    console = Console()
    saved_tty = None
    if sys.stdin.isatty():
        import termios
        import tty

        # Read keys one at a time, unechoed; Ctrl+C still stops the dashboard.
        fd = sys.stdin.fileno()
        saved_tty = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        threading.Thread(target=read_keys, args=(fd, keys, stop), daemon=True).start()
    try:
        with Live(render(dash, console.size.height), screen=True, auto_refresh=False, console=console) as live:
            # Ask the terminal to send the mouse wheel as arrow keys (most do by default).
            console.file.write("\x1b[?1007h")
            while True:
                pressed = []
                try:
                    pressed.append(keys.get(timeout=0.5))
                    while not keys.empty():
                        pressed.append(keys.get())
                except queue.Empty:
                    pass
                if "quit" in pressed:
                    break
                while not events.empty():
                    dash.add(*events.get())
                for key in pressed:
                    dash.press(key)
                live.update(render(dash, console.size.height), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        console.file.write("\x1b[?1007l")
        if saved_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_tty)


if __name__ == "__main__":
    main()
