"""The real catalog.yaml, litellm/config.yaml and pricing.yaml.

catalog.yaml is re-read on every request, so a mistake in it breaks every
request immediately. These checks catch that before it's merged.
"""
import os
import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO

from policy.hash import sha256_hex, strip_frontmatter

EFFORTS = {"low", "medium", "high", "xhigh"}  # never "max": a tier sets a ceiling, not an unbounded budget
CATALOG = yaml.safe_load((REPO / "catalog.yaml").read_text())
CONFIG = yaml.safe_load((REPO / "litellm" / "config.yaml").read_text())
PRICES = yaml.safe_load((REPO / "pricing.yaml").read_text())["models"]
MODELS = {entry["model_name"]: entry["litellm_params"]["model"].split("/")[-1] for entry in CONFIG["model_list"]}


def test_order_lists_every_tier_once():
    assert sorted(CATALOG["order"]) == sorted(CATALOG["tiers"])
    assert len(set(CATALOG["order"])) == len(CATALOG["order"])


def test_order_really_is_cheapest_first():
    # A reordered list would silently send unregistered skills to the priciest model.
    prices = [PRICES[MODELS[tier]]["input"] for tier in CATALOG["order"]]
    assert prices == sorted(prices), f"catalog order {CATALOG['order']} isn't cheapest first: {prices}"


@pytest.mark.parametrize("tier", sorted(CATALOG["tiers"]))
def test_tier_is_complete(tier):
    row = CATALOG["tiers"][tier]
    assert tier in MODELS, f"tier {tier!r} has no model_name in litellm/config.yaml"
    assert MODELS[tier] in PRICES, f"{MODELS[tier]} has no price in pricing.yaml"
    assert row["effort"] in EFFORTS
    for cap in ("max_input", "max_output"):
        assert isinstance(row[cap], int) and row[cap] > 0


@pytest.mark.parametrize("name", sorted(CATALOG["skills"]))
def test_skill_has_a_known_tier_and_valid_hash(name):
    row = CATALOG["skills"][name]
    assert row["tier"] in CATALOG["tiers"]
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
    assert isinstance(row["bytes"], int) and row["bytes"] > 0


def test_every_tier_has_a_skill_to_exercise_it():
    assert {row["tier"] for row in CATALOG["skills"].values()} == set(CATALOG["tiers"])


def test_callback_hook_is_registered():
    assert CONFIG["litellm_settings"]["callbacks"] == ["policy.litellm_callback.proxy_handler_instance"]


SKILLS_DIR = Path(os.environ.get("LLM_TRUNK_SKILLS_DIR", REPO.parent / "company-client" / ".claude" / "skills"))


@pytest.mark.skipif(not SKILLS_DIR.is_dir(), reason=f"skill files not found at {SKILLS_DIR}")
@pytest.mark.parametrize("name", sorted(CATALOG["skills"]))
def test_catalog_hash_matches_skill_file(name):
    body = strip_frontmatter((SKILLS_DIR / name / "SKILL.md").read_bytes())
    row = CATALOG["skills"][name]
    assert (sha256_hex(body), len(body)) == (row["sha256"], row["bytes"]), (
        f"{name}: SKILL.md changed -- re-run scripts/hash_skill.py and update catalog.yaml"
    )
