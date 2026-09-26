"""scripts/dashboard.py: savings math, cache clocks and rendering."""
import sys
from datetime import datetime, timezone

import pytest
from conftest import REPO
from rich.console import Console

sys.path.insert(0, str(REPO / "scripts"))

import dashboard  # noqa: E402

PRICES = dashboard.load_prices()
ROUTES = {"plan-lane": "anthropic/claude-opus-5-5", "verify-lane": "anthropic/claude-haiku-4-5", "untagged": "anthropic/claude-haiku-4-5"}
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


def test_no_saving_when_the_lane_runs_the_requested_model():
    event = {"input_tokens": 1000, "output_tokens": 10, "cost": 0.01, "requested_model": "claude-opus-5-5-20260101"}
    assert dashboard.without_trunk(event, "anthropic/claude-opus-5-5", PRICES) == pytest.approx(0.01)


def test_old_events_without_a_requested_model_are_not_compared():
    event = {"input_tokens": 1000, "output_tokens": 10, "cost": 0.01}
    assert dashboard.without_trunk(event, "anthropic/claude-haiku-4-5", PRICES) is None


def _spend(**fields):
    base = {"skill_id": None, "skill_hash": None, "alias": "untagged", "effort": "low", "session": "s1",
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
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="plan-lane", cost=0.05))
    dash.add(T0, "spend", _spend(requested_model=None, cost=0.01))  # logged before requested_model existed
    dash.add(T0, "deny", {"code": "input_cap", "reason": "too big", "session": "s1"})
    assert dash.requests == 3 and dash.compared == 2 and dash.denies == 1
    assert dash.actual == pytest.approx(0.0668)
    # The untagged request ran on Haiku instead of Opus 5.5. Mostly cached input,
    # and Opus 5.5 cache reads are only 2x Haiku's ($0.20 vs $0.10), so the
    # ratio is well under the 4x of uncached tokens. The plan lane already runs
    # Opus 5.5, so it saves nothing.
    ratio = dashboard.token_cost(_spend(), PRICES["claude-opus-5-5"]) / dashboard.token_cost(_spend(), PRICES["claude-haiku-4-5"])
    assert 2 < ratio < 3
    assert dash.compared_without == pytest.approx(0.0068 * ratio + 0.05)
    assert dash.saved == pytest.approx(0.0068 * (ratio - 1))
    assert dash.lane_cost["plan"] == pytest.approx(0.05)


def test_cache_clock_warm_then_cold():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend(alias="plan-lane", input_tokens=100_000))
    warm, left, if_warm, if_cold = dash.cache_state(dash.sessions["s1"])
    assert warm and left == 300
    assert if_warm == pytest.approx(100_000 * 0.20 / 1e6)  # Opus 5.5 cache read
    assert if_cold == pytest.approx(100_000 * 5.00 / 1e6)  # Opus 5.5 5-minute cache write
    clock.now += 301
    assert dash.cache_state(dash.sessions["s1"])[0] is False


def _screen(dash) -> str:
    console = Console(record=True, width=120, height=40, color_system=None)
    console.print(dashboard.render(dash))
    return console.export_text()


def test_render_shows_savings_lanes_sessions_and_feed():
    clock = Clock()
    dash = _dashboard(clock)
    dash.add(T0, "spend", _spend())
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="plan-lane", session="s2", cost=0.05))
    dash.add(T0, "deny", {"code": "input_cap", "reason": "llm-trunk: ~89409 input tokens exceeds the cap", "session": "s3"})
    clock.now += 120
    screen = _screen(dash)
    saved_share = dash.saved / dash.compared_without
    assert "SAVED $0.0" in screen and f"{saved_share:.0%} cheaper than the models Claude Code asked for" in screen
    assert "plan" in screen and "untagged" in screen
    assert "warm 3:00" in screen
    assert "invoked" in screen and "denied" in screen and "Opus 5.5 · low" in screen


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


def test_lane_that_costs_more_than_asked_is_flagged():
    # What the live run found: Claude Code asked for Opus 5.5, the plan lane ran Opus 5.
    routes = {**ROUTES, "plan-lane": "anthropic/claude-opus-5"}
    dash = dashboard.Dashboard(PRICES, routes, 300, Clock())
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="plan-lane", cost=0.05, cache_read_tokens=0))
    assert dash.saved < 0
    assert dash.lane_saving("plan") == pytest.approx(-0.25)  # Opus 5 is 25% pricier on uncached tokens
    screen = _screen(dash)
    assert "COSTING $" in screen and "MORE" in screen
    assert "⚠ +25% cost" in screen  # in the lane panel and the feed


def test_logged_model_wins_over_current_lane_config():
    # History stays true after a lane is re-pointed: this request really ran on Opus 5.
    dash = _dashboard()  # ROUTES now point plan-lane at Opus 5.5
    dash.add(T0, "spend", _spend(skill_id="plan", skill_hash="h", alias="plan-lane", cost=0.05,
                                 cache_read_tokens=0, model="claude-opus-5-20260101"))
    assert dash.lane_saving("plan") == pytest.approx(-0.25)
    assert dash.sessions["s1"]["model"] == "claude-opus-5-20260101"


def test_unpriceable_logged_model_falls_back_to_lane_config():
    dash = _dashboard()
    dash.add(T0, "spend", _spend(model="qa-test-plan-creation"))  # e.g. an alias instead of a model id
    assert dash.sessions["s1"]["model"] == ROUTES["untagged"]
    assert dash.compared == 1
