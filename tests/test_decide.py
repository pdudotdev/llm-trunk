"""decide(): the routing policy as a pure function."""
from conftest import make_catalog

from policy.decide import INPUT_CAP, PRICE_CLIFF, STALE_HASH, UNKNOWN_SKILL, decide


def test_no_skill_goes_untagged():
    decision = decide(make_catalog(), None, None, None, 1000)
    assert (decision.action, decision.alias, decision.effort, decision.reason) == ("allow", "untagged", "low", "untagged")
    assert decision.max_output == 4000


def test_verified_skill_gets_its_lane():
    catalog = make_catalog()
    row = catalog["skills"]["plan"]
    decision = decide(catalog, "plan", row["sha256"], None, 1000)
    assert (decision.action, decision.alias, decision.effort, decision.reason) == ("allow", "plan-lane", "high", "tagged:plan")
    assert decision.max_output == 8000


def test_sticky_skill_is_used_without_a_hash():
    decision = decide(make_catalog(), None, None, "verify", 1000)
    assert (decision.action, decision.alias) == ("allow", "verify-lane")


def test_invoked_skill_wins_over_sticky():
    catalog = make_catalog()
    decision = decide(catalog, "plan", catalog["skills"]["plan"]["sha256"], "verify", 1000)
    assert decision.alias == "plan-lane"


def test_wrong_hash_is_denied_as_stale():
    decision = decide(make_catalog(), "plan", "0" * 64, None, 1000)
    assert (decision.action, decision.code) == ("deny", STALE_HASH)
    assert "re-run scripts/hash_skill.py" in decision.reason


def test_unknown_skill_is_denied():
    decision = decide(make_catalog(), "not-a-skill", "0" * 64, None, 1000)
    assert (decision.action, decision.code) == ("deny", UNKNOWN_SKILL)


def test_input_cap_boundary():
    catalog = make_catalog()
    assert decide(catalog, None, None, None, 63999).action == "allow"
    at_cap = decide(catalog, None, None, None, 64000)
    assert (at_cap.action, at_cap.code) == ("deny", INPUT_CAP)
    assert "untagged lane cap (64000)" in at_cap.reason


def test_input_cap_is_per_lane():
    catalog = make_catalog()
    plan_hash = catalog["skills"]["plan"]["sha256"]
    assert decide(catalog, "plan", plan_hash, None, 179999).action == "allow"
    assert decide(catalog, "plan", plan_hash, None, 180000).code == INPUT_CAP


def _vendor_catalog(vendor: str) -> dict:
    catalog = make_catalog()
    catalog["untagged"].update(vendor=vendor, max_input=1_000_000)
    return catalog


def test_price_cliffs():
    # The fixtures from LLM-TRUNK.md: 210k crosses Grok/Gemini, 280k crosses OpenAI.
    for vendor in ("grok", "gemini"):
        assert decide(_vendor_catalog(vendor), None, None, None, 199_999).action == "allow"
        assert decide(_vendor_catalog(vendor), None, None, None, 210_000).code == PRICE_CLIFF
    assert decide(_vendor_catalog("openai"), None, None, None, 210_000).action == "allow"
    assert decide(_vendor_catalog("openai"), None, None, None, 280_000).code == PRICE_CLIFF


def test_anthropic_has_no_price_cliff():
    assert decide(_vendor_catalog("anthropic"), None, None, None, 900_000).action == "allow"
