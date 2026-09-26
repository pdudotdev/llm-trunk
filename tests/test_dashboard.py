"""scripts/dashboard.py: savings math, cache clocks and rendering."""
import argparse
import sys
from datetime import datetime, timezone

import pytest
from conftest import REPO
from rich.cells import cell_len
from rich.console import Console

sys.path.insert(0, str(REPO / "scripts"))

import dashboard  # noqa: E402

PRICES = dashboard.load_prices()
ROUTES = {"complex": "anthropic/claude-opus-5-5", "moderate": "anthropic/claude-sonnet-5", "light": "anthropic/claude-haiku-4-5"}
T0 = datetime(2026, 9, 26, 11, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("spelling", "key"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic/claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-opus-4-6[1m]", "claude-opus-4-6"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("global.anthropic.claude-sonnet-5", "claude-sonnet-5"),
        (None, None),
    ],
)
def test_model_key(spelling, key):
    assert dashboard.model_key(spelling) == key


def test_every_routed_model_has_a_price():
    for alias, model in dashboard.load_routes().items():
        assert dashboard.model_key(model) in PRICES, f"{alias}: add {model} to pricing.yaml"


def test_token_cost_splits_cache_reads_and_writes():
    event = {"input_tokens": 100_000, "cache_read_tokens": 60_000, "cache_write_tokens": 30_000, "output_tokens": 1_000}
    # Sonnet 5: 10k fresh x $2 + 60k read x $0.20 + 30k write x $2.50 + 1k out x $10, per million
    assert dashboard.token_cost(event, PRICES["claude-sonnet-5"]) == pytest.approx(0.117)


def test_without_trunk_prices_the_requested_model():
    event = {"input_tokens": 40_000, "cache_read_tokens": 0, "cache_write_tokens": 40_000, "output_tokens": 500,
             "cost": 0.0525, "requested_model": "claude-opus-5-5"}
    baseline = dashboard.without_trunk(event, "anthropic/claude-haiku-4-5", PRICES)
    assert baseline == pytest.approx(0.0525 * 4)  # Opus 5.5 is 4x Haiku 4.5 for every token type here


def test_no_saving_when_the_tier_runs_the_requested_model():
    event = {"input_tokens": 1000, "output_tokens": 10, "cost": 0.01, "requested_model": "claude-opus-5-5-20260101"}
    assert dashboard.without_trunk(event, "anthropic/claude-opus-5-5", PRICES) == pytest.approx(0.01)


def test_old_events_without_a_requested_model_are_not_compared():
    event = {"input_tokens": 1000, "output_tokens": 10, "cost": 0.01}
    assert dashboard.without_trunk(event, "anthropic/claude-haiku-4-5", PRICES) is None


def _spend(**fields):
    base = {"skill_id": None, "skill_hash": None, "alias": "light", "effort": "low", "session": "s1",
            "input_tokens": 40_000, "cache_read_tokens": 38_000, "cache_write_tokens": 0, "output_tokens": 300,
            "cost": 0.0068, "requested_model": "claude-opus-5-5", "background": None}
    return {**base, **fields}


class Clock:
    now = T0.timestamp()

    def __call__(self):
        return self.now


def _dashboard(clock=None):
    return dashboard.Dashboard(PRICES, ROUTES, 300, clock or Clock())


def test_dashboard_totals_and_savings():
    dash = _dashboard()
    dash.add(T0, "spend", _spend())
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="complex", cost=0.05))
    dash.add(T0, "spend", _spend(requested_model=None, cost=0.01))  # logged before requested_model existed
    dash.add(T0, "deny", {"code": "input_cap", "reason": "too big", "session": "s1"})
    assert dash.requests == 3 and dash.compared == 2 and dash.denies == 1
    assert dash.actual == pytest.approx(0.0668)
    # The untagged request ran on Haiku instead of Opus 5.5. Mostly cached input,
    # and Opus 5.5 cache reads are only 2x Haiku's ($0.20 vs $0.10), so the
    # ratio is well under the 4x of uncached tokens. The complex tier already runs
    # Opus 5.5, so it saves nothing.
    ratio = dashboard.token_cost(_spend(), PRICES["claude-opus-5-5"]) / dashboard.token_cost(_spend(), PRICES["claude-haiku-4-5"])
    assert 2 < ratio < 3
    assert dash.compared_without == pytest.approx(0.0068 * ratio + 0.05)
    assert dash.saved == pytest.approx(0.0068 * (ratio - 1))
    assert dash.type_cost["skill"] == pytest.approx(0.05)


def test_cache_clock_warm_then_cold():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend(alias="complex", input_tokens=100_000))
    warm, left, if_warm, if_cold = dash.cache_state(dash.sessions["s1"])
    assert warm and left == 300
    assert if_warm == pytest.approx(100_000 * 0.20 / 1e6)  # Opus 5.5 cache read
    assert if_cold == pytest.approx(100_000 * 5.00 / 1e6)  # Opus 5.5 5-minute cache write
    clock.now += 301
    assert dash.cache_state(dash.sessions["s1"])[0] is False


def _screen(dash, width=120) -> str:
    console = Console(record=True, width=width, height=40, color_system=None)
    console.print(dashboard.render(dash, 40))
    return console.export_text()


def test_render_shows_savings_types_sessions_and_feed():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend())
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="complex", session="s2", cost=0.05))
    dash.add(T0, "deny", {"code": "input_cap", "reason": "llm-trunk: ~89409 input tokens exceeds the cap", "session": "s3"})
    clock.now += 120
    screen = _screen(dash)
    saved_share = dash.saved / dash.compared_without
    assert "SAVED $0.0" in screen and f"{saved_share:.0%} cheaper than the models Claude Code asked for" in screen
    assert "plan" in screen and "untagged" in screen
    assert "● 3:00" in screen
    assert "invoked" in screen and "denied" in screen and "Opus 5.5" in screen and "low" in screen


def test_render_before_any_comparable_traffic():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(requested_model=None))
    assert "no requests with a recorded client model yet" in _screen(dash)


@pytest.mark.parametrize(
    ("saving", "text"),
    [(None, "—"), (0.001, "—"), (0.75, "saved 75%"), (-0.9, "⚠ +90% cost")],
)
def test_vs_asked_labels(saving, text):
    assert dashboard.vs_asked(saving).plain == text


def test_type_that_costs_more_than_asked_is_flagged():
    # What the live run found: Claude Code asked for Opus 5.5, the tier ran Opus 5.
    routes = {**ROUTES, "complex": "anthropic/claude-opus-5"}
    dash = dashboard.Dashboard(PRICES, routes, 300, Clock())
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="complex", cost=0.05, cache_read_tokens=0))
    assert dash.saved < 0
    assert dash.type_saving("skill") == pytest.approx(-0.25)  # Opus 5 is 25% pricier on uncached tokens
    screen = _screen(dash)
    assert "COSTING $" in screen and "MORE" in screen
    assert "⚠ +25% cost" in screen  # in the request-type panel and the feed


def test_logged_model_wins_over_current_tier_config():
    # History stays true after a tier is re-pointed: this request really ran on Opus 5.
    dash = _dashboard()  # ROUTES now point complex at Opus 5.5
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="complex", cost=0.05,
                                 cache_read_tokens=0, model="claude-opus-5-20260101"))
    assert dash.type_saving("skill") == pytest.approx(-0.25)
    assert dash.sessions["s1"]["model"] == "claude-opus-5-20260101"


def test_unpriceable_logged_model_falls_back_to_tier_config():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(model="complex"))  # e.g. a tier name instead of a model id
    assert dash.sessions["s1"]["model"] == ROUTES["light"]
    assert dash.compared == 1


def test_sessions_idle_longer_than_the_window_are_hidden():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend(session="oldsess1"))
    clock.now += 20 * 60
    dash.add(datetime.fromtimestamp(clock.now, timezone.utc), "spend", _spend(session="newsess1"))
    clock.now += 11 * 60  # oldsess1 idle 31 min, newsess1 idle 11 min (cold but still shown)
    screen = _screen(dash)
    assert "newsess1" in screen and "cold" in screen
    assert "oldsess1" not in screen.split("sessions active")[1].split("live")[0]
    assert "sessions active in the last 30 min · cache 5 min" in screen


def test_warm_session_line_is_not_truncated():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(alias="complex", input_tokens=53_000))
    screen = _screen(dash)
    assert "NEXT MESSAGE" in screen and "$0.011 now · $0.265 when cold" in _screen(dash, width=160)


def test_spend_pane_groups_by_request_type():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(request_type="normal"))
    dash.add(T0, "spend", _spend(request_type="subagent", cost=0.02))
    dash.add(T0, "spend", _spend(request_type="compaction", cost=0.01))
    dash.add(T0, "spend", _spend(background="prompt_suggestion", cost=0.003))  # old line: no request_type
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", cost=0.04))  # old line: an invocation
    assert set(dash.type_cost) == {"normal", "subagent", "compaction", "background", "skill"}
    screen = _screen(dash)
    assert "spend by request type · vs model asked for" in screen
    for kind in ("normal", "skill", "subagent", "compaction", "internal"):
        assert kind in screen


def test_feed_shows_tier_and_skill():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(request_type="skill", skill_id="design-review", skill_hash="h", tier="complex", alias="complex"))
    dash.add(T0, "spend", _spend(request_type="subagent", tier="moderate", alias="light"))
    screen = _screen(dash)
    assert "skill (design-review)" in screen and "🤖 subagent" in screen


def test_type_names_the_skill_only_for_skill_requests():
    sticky = _spend(request_type="normal", skill_id="change-review", tier="moderate")
    title = _spend(request_type="background", background="title", skill_id="change-review")
    assert dashboard.type_text(_spend(request_type="skill", skill_id="change-review")) == "skill (change-review)"
    assert dashboard.type_text(_spend(request_type="skill", unregistered_skill="notes")) == "skill (notes)"
    assert dashboard.type_text(sticky) == "normal"
    assert dashboard.type_text(title) == "internal"


def test_cold_session_names_the_re_cache_cost():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend(alias="complex", input_tokens=53_000))
    clock.now += 301
    assert "next re-cache costs $0.265" in _screen(dash, width=160)


def test_session_clock_follows_the_main_conversation_only():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(request_type="normal", alias="complex", input_tokens=120_000))
    dash.add(T0, "spend", _spend(request_type="subagent", alias="light", input_tokens=15_000))
    dash.add(T0, "spend", _spend(request_type="background", background="title", input_tokens=900))
    dash.add(T0, "spend", _spend(request_type="background", background="permission_check", input_tokens=2_000))
    state = dash.sessions["s1"]
    assert state["context"] == 120_000 and state["model"] == ROUTES["complex"]


@pytest.mark.parametrize(("text", "seconds"), [("30m", 1800), ("2h", 7200), ("5m", 300)])
def test_window_durations(text, seconds):
    assert dashboard.duration(text) == seconds


@pytest.mark.parametrize("text", ["2d", "90", "0m", "h", "1.5h"])
def test_window_rejects_what_it_would_misread(text):
    with pytest.raises(argparse.ArgumentTypeError, match="use minutes or hours"):
        dashboard.duration(text)


def test_feed_names_each_rows_session():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(session="a1b2c3d4"))
    dash.add(T0, "deny", {"reason": "llm-trunk: too big", "session": "ffee0011", "request_type": "normal"})
    feed = _screen(dash).split("─ live")[1]
    assert "SESSION" in feed and "a1b2c3d4" in feed and "ffee0011" in feed


def test_deny_reason_runs_to_the_end_of_the_line():
    dash = _dashboard()
    reason = ("llm-trunk: skill 'change-review' changed since it was hashed (got 3f2a9c41d0e7, catalog has "
              "8b1e77c2a9d4) — re-run scripts/hash_skill.py and update catalog.yaml")
    dash.add(T0, "spend", _spend())
    dash.add(T0, "deny", {"code": "stale_hash", "reason": reason, "session": "s1", "request_type": "skill"})
    assert reason.removeprefix("llm-trunk: ") in _screen(dash, width=260)
    narrow = _screen(dash, width=160)  # cut off at the edge, without squeezing the other rows
    assert "changed since it was hashed" in narrow and "update catalog.yaml" not in narrow
    assert "40k (c)  $0.007" in narrow


def test_every_row_fills_every_column():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(request_type="skill", skill_id="change-review", skill_hash="h", tier="moderate"))
    dash.add(T0, "deny", {"code": "input_cap", "reason": "llm-trunk: too big", "session": "s1", "request_type": "normal"})
    dash.add(T0, "expired", {"skill_id": "change-review", "tier": "moderate", "why": "idle", "session": "s1",
                             "ended_at": T0.timestamp() - 3600})
    feed = _screen(dash, width=200).split("─ live")[1]
    header, expired, denied, _ = [line for line in feed.splitlines() if "NOTE" in line or "s1" in line]
    def column(line, text):  # on screen: the emoji take two cells
        return cell_len(line[: line.index(text)])

    note = column(header, "NOTE")
    ended = f"{datetime.fromtimestamp(T0.timestamp() - 3600, T0.tzinfo):%H:%M}"
    assert column(expired, f"change-review sticky route ended at {ended} (idle)") == note
    assert column(denied, "too big") == note
    assert expired.split()[5:12] == ["—", "moderate", "—", "—", "—", "—", "—"]  # REQ TYPE .. VS ASKED
    assert denied.split()[5:13] == ["normal", "—", "—", "—", "—", "—", "—", "too"]


def test_note_column_only_when_a_row_has_one():
    dash = _dashboard()
    dash.add(T0, "spend", _spend())
    assert "NOTE" not in _screen(dash)


def _busy(clock, rows):
    dash = _dashboard(clock)
    for i in range(rows):
        dash.add(T0, "spend", _spend(session=f"r{i:04d}"))
    return dash


def test_feed_fills_the_screen_and_keeps_history():
    dash = _busy(Clock(), 200)
    screen = _screen(dash)  # 40 lines: 21 request rows
    assert "r0199" in screen and "r0179" in screen and "r0178" not in screen
    assert "live · 200 rows" in screen
    assert len(dash.feed) == 200


def test_scrolling_back_through_the_feed():
    dash = _busy(Clock(), 200)
    _screen(dash)
    dash.press("pgdn")
    screen = _screen(dash)
    assert "r0178" in screen and "r0179" not in screen
    assert "paused · rows 22–42 of 200" in screen and "g: back to live" in screen
    dash.press("end")
    assert "r0000" in _screen(dash)
    dash.press("down")  # already at the oldest row
    assert dash.scroll == 200 - dash.page
    dash.press("home")
    assert dash.scroll == 0 and "r0199" in _screen(dash)
    dash.press("up")  # already at the newest row
    assert dash.scroll == 0


def test_new_rows_do_not_move_a_scrolled_view():
    clock = Clock()
    dash = _busy(clock, 100)
    _screen(dash)
    dash.press("down")
    dash.press("down")
    dash.add(T0, "spend", _spend(session="new00001"))
    screen = _screen(dash)
    assert "r0097" in screen.split("\n")[18]  # still the first row shown
    assert "new00001" not in screen and "1 new above" in screen
    dash.press("home")
    screen = _screen(dash)
    assert "new00001" in screen and "new above" not in screen


def test_scrolling_is_harmless_on_a_short_feed():
    dash = _busy(Clock(), 3)
    _screen(dash)
    for key in ("down", "pgdn", "end"):
        dash.press(key)
        assert dash.scroll == 0


@pytest.mark.parametrize(
    ("data", "keys"),
    [
        ("\x1b[A\x1b[A\x1b[B", ["up", "up", "down"]),  # arrows, or the mouse wheel
        ("\x1bOA\x1bOB", ["up", "down"]),  # arrows in application mode
        ("\x1b[5~\x1b[6~ b", ["pgup", "pgdn", "pgdn", "pgup"]),
        ("gG\x1b[H\x1b[F", ["home", "end", "home", "end"]),
        ("jkxq", ["down", "up", "quit"]),
        ("\x1b[Z", []),  # anything else is ignored
    ],
)
def test_keys(data, keys):
    assert dashboard.parse_keys(data) == keys


def test_sessions_pane_shows_the_sticky_timer():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend(request_type="skill", skill_id="design-review", skill_hash="h", tier="complex",
                                 alias="complex", sticky_left_s=600))
    clock.now += 70
    assert "📌 8:50 design-review" in _screen(dash, width=160)
    assert "📌 8:50 design-review" in _screen(dash)  # a narrow terminal shortens the price text first
    # A background call reports the timer without refreshing it; a subagent reports none.
    dash.add(T0, "spend", _spend(request_type="background", background="title", skill_id="design-review", sticky_left_s=600))
    dash.add(T0, "spend", _spend(request_type="subagent", tier="moderate"))
    assert dash.sticky_left("s1") == ("design-review", 530)
    clock.now += 600
    assert dash.sticky_left("s1") is None


def test_sticky_timer_ends_with_the_route():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(request_type="skill", skill_id="plan", skill_hash="h", tier="complex", sticky_left_s=600))
    dash.add(T0, "expired", {"skill_id": "plan", "tier": "complex", "why": "idle", "session": "s1"})
    assert dash.sticky_left("s1") is None
    dash.add(T0, "spend", _spend(request_type="skill", skill_id="plan", skill_hash="h", tier="complex", sticky_left_s=600))
    dash.add(T0, "spend", _spend(request_type="skill", unregistered_skill="notes", sticky_left_s=None))
    assert dash.sticky_left("s1") is None
