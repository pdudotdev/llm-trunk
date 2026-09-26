"""decide(): the tier policy as a pure function."""
import pytest
from conftest import make_catalog

from policy.decide import (
    BACKGROUND,
    COMPACTION,
    INPUT_CAP,
    NORMAL,
    PRICE_CLIFF,
    SKILL,
    STALE_HASH,
    SUBAGENT,
    UNKNOWN_SKILL,
    decide,
    lowest,
    one_down,
)


def _skill(catalog, name, **extra):
    return decide(catalog, SKILL, 1000, skill_id=name, skill_hash=catalog["skills"][name]["sha256"], registered=True, **extra)


def test_tier_order_comes_from_the_catalog():
    catalog = make_catalog()
    assert lowest(catalog) == "light"
    assert one_down(catalog, "complex") == "moderate"
    assert one_down(catalog, "moderate") == "light"
    assert one_down(catalog, "light") == "light"  # the floor


def test_normal_turn_without_a_route_goes_to_the_lowest_tier():
    decision = decide(make_catalog(), NORMAL, 1000)
    assert (decision.action, decision.tier, decision.effort, decision.reason) == ("allow", "light", "low", "untagged")
    assert decision.max_output == 4000


def test_normal_turn_stays_on_the_sticky_tier():
    decision = decide(make_catalog(), NORMAL, 1000, session_tier="complex")
    assert (decision.tier, decision.effort, decision.reason) == ("complex", "high", "sticky")


@pytest.mark.parametrize(("name", "tier"), [("plan", "complex"), ("review", "moderate"), ("verify", "light")])
def test_registered_skill_gets_its_catalog_tier(name, tier):
    decision = _skill(make_catalog(), name)
    assert (decision.action, decision.tier, decision.reason) == ("allow", tier, f"skill:{name}")


def test_registered_skill_overrides_the_sticky_tier_both_ways():
    catalog = make_catalog()
    assert _skill(catalog, "verify", session_tier="complex").tier == "light"
    assert _skill(catalog, "plan", session_tier="light").tier == "complex"


def test_wrong_hash_is_denied_as_stale():
    decision = decide(make_catalog(), SKILL, 1000, skill_id="plan", skill_hash="0" * 64, registered=True)
    assert (decision.action, decision.code) == ("deny", STALE_HASH)
    assert "re-run scripts/hash_skill.py" in decision.reason


def test_unknown_registered_claim_is_denied():
    decision = decide(make_catalog(), SKILL, 1000, skill_id="nope", skill_hash="0" * 64, registered=True)
    assert decision.code == UNKNOWN_SKILL


def test_unregistered_skill_goes_to_the_lowest_tier_even_mid_session():
    decision = decide(make_catalog(), SKILL, 1000, session_tier="complex", registered=False)
    assert (decision.tier, decision.reason) == ("light", "unregistered skill")


@pytest.mark.parametrize(("parent", "expected"), [("complex", "moderate"), ("moderate", "light"), ("light", "light"), (None, "light")])
def test_subagent_is_one_tier_below_its_parent(parent, expected):
    assert decide(make_catalog(), SUBAGENT, 1000, session_tier=parent).tier == expected


def test_subagent_keeps_the_tier_it_started_on():
    # The parent moved to complex since, but the subagent's cache is on moderate.
    decision = decide(make_catalog(), SUBAGENT, 1000, session_tier="complex", agent_tier="light")
    assert decision.tier == "light"


@pytest.mark.parametrize("request_type", [COMPACTION, BACKGROUND])
@pytest.mark.parametrize("current", ["complex", "moderate", None])
def test_compaction_and_background_stay_on_the_current_tier(request_type, current):
    assert decide(make_catalog(), request_type, 1000, session_tier=current).tier == (current or "light")


def test_input_cap_boundary_is_per_tier():
    catalog = make_catalog()
    assert decide(catalog, NORMAL, 63999).action == "allow"
    at_cap = decide(catalog, NORMAL, 64000)
    assert (at_cap.action, at_cap.code) == ("deny", INPUT_CAP)
    assert "light tier cap (64000)" in at_cap.reason and "run /compact" in at_cap.reason
    assert decide(catalog, NORMAL, 179999, session_tier="complex").action == "allow"
    assert decide(catalog, NORMAL, 180000, session_tier="complex").code == INPUT_CAP


def test_compaction_is_never_capped():
    # /compact is how an oversized conversation recovers from a cap deny.
    assert decide(make_catalog(), COMPACTION, 150_000).action == "allow"
    assert decide(make_catalog(), BACKGROUND, 150_000).code == INPUT_CAP


def _vendor_catalog(vendor: str) -> dict:
    catalog = make_catalog()
    catalog["tiers"]["light"].update(vendor=vendor, max_input=1_000_000)
    return catalog


def test_price_cliffs():
    # The fixtures from LLM-TRUNK.md: 210k crosses Grok/Gemini, 280k crosses OpenAI.
    for vendor in ("grok", "gemini"):
        assert decide(_vendor_catalog(vendor), NORMAL, 199_999).action == "allow"
        assert decide(_vendor_catalog(vendor), NORMAL, 210_000).code == PRICE_CLIFF
    assert decide(_vendor_catalog("openai"), NORMAL, 210_000).action == "allow"
    assert decide(_vendor_catalog("openai"), NORMAL, 280_000).code == PRICE_CLIFF
    assert decide(_vendor_catalog("anthropic"), NORMAL, 900_000).action == "allow"
