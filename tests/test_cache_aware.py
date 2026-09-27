"""Cache-aware switching: a move down waits until it pays for re-caching."""
import asyncio
import json
import types

import pytest
import yaml
from conftest import (
    HTTPException,
    REPO,
    assistant,
    cb,
    compaction_request,
    key,
    request,
    routing,
    skill_turn,
    subagent_request,
    unregistered_turn,
    user,
)

from policy.cache import hold_or_move, load_prices, prompt_cost, token_cost

PRICES = load_prices(REPO / "pricing.yaml")
OPUS, SONNET, HAIKU = PRICES["claude-opus-5-5"], PRICES["claude-sonnet-5"], PRICES["claude-haiku-4-5"]

# A Claude Code-sized conversation: ~20k estimated tokens, under every cap.
BIG = user("context " * 7_500)


def turn(*later, session="session-1", **extra):
    return request(BIG, assistant(), *later, session=session, **extra)


def reply(gateway, data, *, input_tokens=20_000, read=19_000, write=1_000, output=500, cost=None, one_hour=0):
    """Log the reply to a routed request, as LiteLLM would after Anthropic answers."""
    usage = types.SimpleNamespace(
        prompt_tokens=input_tokens, completion_tokens=output, cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
        cache_creation={"ephemeral_5m_input_tokens": write - one_hour, "ephemeral_1h_input_tokens": one_hour},
    )
    kwargs = {"litellm_params": {"metadata": data["litellm_metadata"]}, "response_cost": cost}
    asyncio.run(gateway.callback.async_log_success_event(kwargs, types.SimpleNamespace(usage=usage, model="m"), None, None))


# --- The economics (policy/cache.py) ----------------------------------------------


def test_prompt_cost_reads_the_cached_part_and_writes_the_rest():
    assert prompt_cost(OPUS, 100_000, 90_000) == pytest.approx((90_000 * 0.20 + 10_000 * 5.00) / 1e6)
    assert prompt_cost(OPUS, 100_000, 0) == pytest.approx(100_000 * 5.00 / 1e6)
    assert prompt_cost(OPUS, 100_000, 200_000) == pytest.approx(100_000 * 0.20 / 1e6)  # never more than all


def test_hold_until_holding_has_cost_as_much_as_moving():
    assert hold_or_move(penalty=0.12, held=0.0) is True
    assert hold_or_move(penalty=0.12, held=0.12) is False
    assert hold_or_move(penalty=-0.01, held=0.0) is False  # moving is already cheaper


@pytest.mark.parametrize(
    ("case", "stay_price", "stay_tokens", "stay_cached", "move_price", "move_tokens", "move_cached", "penalty"),
    [
        # dev-day /change-review: Opus -> Sonnet, nothing cached on Sonnet. Logged: $0.143 moved.
        ("hold", OPUS, 55_858, 55_300, SONNET, 55_858, 0, 0.126),
        # dev-day /personal-notes: -> Haiku, which still had tools+system (36.8k of 43.5k).
        ("move", OPUS, 56_500, 55_700, HAIKU, 43_466, 36_829, -0.003),
        # dev-day /commit-message after /compact: only tools+system cached on either side.
        ("move", OPUS, 55_000, 48_000, HAIKU, 41_752, 36_829, -0.035),
    ],
)
def test_penalty_matches_what_the_logs_show(case, stay_price, stay_tokens, stay_cached, move_price, move_tokens, move_cached, penalty):
    got = prompt_cost(move_price, move_tokens, move_cached) - prompt_cost(stay_price, stay_tokens, stay_cached)
    assert got == pytest.approx(penalty, abs=0.004)
    assert hold_or_move(got, 0.0) is (case == "hold")


def test_opus_and_sonnet_read_the_cache_at_the_same_price():
    # Why complex -> moderate on a warm conversation almost never pays back.
    assert OPUS["cache_read"] == SONNET["cache_read"]


def test_token_cost_is_shared_with_the_dashboard():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import dashboard

    assert dashboard.token_cost is token_cost


# --- Holds in the gateway --------------------------------------------------------


def test_cheaper_skill_on_a_warm_conversation_is_held(gateway):
    gateway.send(turn(skill_turn("plan")))
    data = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))
    assert data["model"] == "complex" and data["reasoning_effort"] == "high"  # runs as the held tier
    fields = routing(data)
    assert (fields["held_for"], fields["warm_tier"], fields["tier"]) == ("moderate", "complex", "complex")
    assert fields["recache_cost"] > fields["held_so_far"] == 0
    # The rules' tier still moved: the session is now on the review skill.
    (route,) = gateway.callback._sticky.values()
    assert route[:2] == ("moderate", "review")
    follow_up = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review"), assistant(), user("and?")))
    assert follow_up["model"] == "complex" and routing(follow_up)["held_for"] == "moderate"


def test_cold_conversation_moves_down_at_once(gateway, clock):
    gateway.send(turn(skill_turn("plan")))
    clock.advance(cb.CACHE_TTL_SECONDS)
    data = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))
    assert data["model"] == "moderate" and routing(data).get("held_for") is None
    assert routing(data).get("recache_cost") is None  # nothing was weighed: it was free


def test_suggestions_keep_the_cache_warm(gateway, clock):
    gateway.send(turn(skill_turn("plan")))
    clock.advance(200)
    gateway.send(turn(skill_turn("plan"), assistant(), user("[SUGGESTION MODE: x]")))
    clock.advance(200)  # 400 s since the turn, 200 s since the suggestion read the cache
    assert gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))["model"] == "complex"


def test_moves_once_holding_has_cost_as_much_as_moving(gateway, capsys):
    gateway.send(turn(skill_turn("plan")))
    held = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))
    capsys.readouterr()
    reply(gateway, held, output=20_000)  # a long answer: $0.40 on Opus, $0.20 on Sonnet
    (line,) = [line for line in capsys.readouterr().out.splitlines() if line.startswith("llm-trunk spend")]
    extra = json.loads(line.split(": ", 1)[1])["hold_extra"]
    assert extra == pytest.approx(token_cost({"input_tokens": 20_000, "cache_read_tokens": 19_000, "cache_write_tokens": 1_000,
                                              "output_tokens": 20_000}, OPUS)
                                  - token_cost({"input_tokens": 20_000, "cache_read_tokens": 19_000, "cache_write_tokens": 1_000,
                                                "output_tokens": 20_000}, SONNET), abs=1e-5)
    moved = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review"), assistant(), user("next")))
    fields = routing(moved)
    assert moved["model"] == "moderate" and fields["held_for"] is None
    assert fields["recache_cost"] <= fields["held_so_far"] == pytest.approx(extra, abs=1e-4)
    # The hold is over: its total starts again from zero.
    assert gateway.callback._conversations[next(iter(gateway.callback._conversations))]["held"] == 0.0


def test_sticky_route_ending_while_warm_is_held(gateway, clock):
    gateway.send(turn(skill_turn("plan")))
    for _ in range(7):
        clock.advance(250)
        gateway.send(turn(skill_turn("plan"), assistant(), user("more")))
    clock.advance(cb.STICKY_MAX_AGE_SECONDS - 1750 + 1)  # past max age, 51 s after the last turn
    data = gateway.send(turn(skill_turn("plan"), assistant(), user("more")))
    assert data["model"] == "complex" and routing(data)["held_for"] == "light"


def test_unregistered_skill_on_a_warm_conversation_is_held(gateway):
    gateway.send(turn(skill_turn("plan")))
    data = gateway.send(turn(skill_turn("plan"), assistant(), unregistered_turn()))
    assert data["model"] == "complex" and routing(data)["held_for"] == "light"
    assert routing(data)["unregistered_skill"] == "personal-notes" and not gateway.callback._sticky


BIG_TOOLS = [{"name": "Read", "description": "d" * 60_000, "input_schema": {"type": "object"}}]


def test_move_is_free_when_the_cheaper_model_already_has_the_tool_list(gateway):
    # Another session in the same directory keeps Claude Code's tools+system
    # cached on Haiku; this conversation's own part is small.
    gateway.send(request(user("hi"), session="other", tools=False) | {"tools": BIG_TOOLS})
    gateway.send(request(skill_turn("plan"), tools=False) | {"tools": BIG_TOOLS})
    data = gateway.send(request(skill_turn("plan"), assistant(), skill_turn("verify"), tools=False) | {"tools": BIG_TOOLS})
    assert data["model"] == "light" and routing(data)["recache_cost"] < 0


def test_same_move_is_held_when_the_cheaper_model_has_nothing_cached(gateway):
    gateway.send(request(skill_turn("plan"), tools=False) | {"tools": BIG_TOOLS})
    data = gateway.send(request(skill_turn("plan"), assistant(), skill_turn("verify"), tools=False) | {"tools": BIG_TOOLS})
    assert data["model"] == "complex" and routing(data)["held_for"] == "light"


def test_after_compaction_only_the_tool_list_is_cached(gateway):
    gateway.send(turn(skill_turn("plan")))
    gateway.send(compaction_request(BIG, assistant(), skill_turn("plan")))  # held tier == rules' tier: no hold
    summary = user("<summary>" + "s" * 30_000 + "</summary>")  # the conversation starts over
    data = gateway.send(request(summary, assistant(), skill_turn("verify")))
    assert data["model"] == "light" and routing(data)["recache_cost"] < 0


def test_compaction_during_a_hold_runs_on_the_warm_tier(gateway):
    gateway.send(turn(skill_turn("plan")))
    gateway.send(turn(skill_turn("plan"), assistant(), unregistered_turn()))  # held for light
    data = gateway.send(compaction_request(BIG, assistant(), skill_turn("plan"), assistant(), unregistered_turn()))
    assert data["model"] == "complex" and routing(data)["request_type"] == "compaction"


def test_never_holds_above_what_the_key_can_reach(gateway):
    gateway.send(turn(skill_turn("plan")))
    revoked = key(allowed_skills=["review"])  # the key loses the complex skill
    data = gateway.send(turn(skill_turn("plan"), assistant(), user("next")), revoked)
    assert data["model"] == "light"


def test_holds_never_let_through_what_the_rules_deny(gateway):
    history = user("x" * 200_000)  # ~66k tokens: fine on complex, over light's cap
    gateway.send(request(history, assistant(), skill_turn("plan")))
    with pytest.raises(HTTPException) as denied:
        gateway.send(request(history, assistant(), skill_turn("plan"), assistant(), unregistered_turn()))
    assert "light tier cap" in denied.value.detail


def test_off_switch_in_the_catalog(gateway, catalog, monkeypatch, tmp_path):
    path = tmp_path / "off.yaml"
    path.write_text(yaml.safe_dump({**catalog, "cache_aware": False}))
    monkeypatch.setattr(cb, "CATALOG_PATH", str(path))
    gateway.send(turn(skill_turn("plan")))
    assert gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))["model"] == "moderate"


def test_subagent_follows_the_rules_tier_not_the_held_one(gateway):
    gateway.send(turn(skill_turn("plan")))
    gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))  # held on complex for moderate
    assert gateway.send(subagent_request())["model"] == "light"  # one below moderate


def test_title_is_never_held(gateway):
    gateway.send(turn(skill_turn("plan")))
    data = gateway.send(request(user("<session>" + "t" * 30_000 + "</session>"), tools=False))
    assert data["model"] == "light" and routing(data).get("held_for") is None


def test_one_hour_cache_writes_extend_the_warm_window(gateway, clock):
    first = gateway.send(turn(skill_turn("plan")))
    reply(gateway, first, one_hour=1_000)
    clock.advance(20 * 60)  # long past 5 minutes, well inside the hour
    gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("plan")))  # re-invoke: still complex
    data = gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("plan"), assistant(), skill_turn("review")))
    assert data["model"] == "complex" and routing(data)["held_for"] == "moderate"


def test_small_prompts_are_never_held(gateway):
    # Below the minimum cacheable size there may be nothing cached to keep.
    gateway.send(request(skill_turn("plan")))
    assert gateway.send(request(skill_turn("plan"), assistant(), skill_turn("review")))["model"] == "moderate"


def test_missing_price_means_no_hold(gateway, monkeypatch, tmp_path):
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump({"models": {}}))
    monkeypatch.setattr(cb, "PRICING_PATH", str(path))
    gateway.send(turn(skill_turn("plan")))
    assert gateway.send(turn(skill_turn("plan"), assistant(), skill_turn("review")))["model"] == "moderate"


def test_conversation_memory_is_bounded(gateway, monkeypatch):
    monkeypatch.setattr(cb, "MAX_STICKY_SESSIONS", 2)
    monkeypatch.setattr(cb, "MAX_PENDING", 2)
    for session in ("a", "b", "c"):
        gateway.send(turn(user("hi"), session=session))
    assert len(gateway.callback._conversations) == 2 and len(gateway.callback._pending) == 2


def test_every_request_logs_when_it_started(gateway, clock):
    fields = routing(gateway.send(request(user("hi"))))
    assert fields["started_at"] == pytest.approx(clock.time(), abs=0.01)
