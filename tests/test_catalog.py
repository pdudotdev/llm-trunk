"""The real catalog.yaml and litellm/config.yaml.

catalog.yaml is re-read on every request, so a mistake in it breaks every
request immediately. These checks catch that before it's merged.
"""
import os
import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO

from policy.cliffs import _VENDOR_CLIFFS
from policy.hash import sha256_hex, strip_frontmatter

EFFORTS = {"low", "medium", "high", "xhigh"}  # never "max" (LLM-TRUNK.md)
CATALOG = yaml.safe_load((REPO / "catalog.yaml").read_text())
CONFIG = yaml.safe_load((REPO / "litellm" / "config.yaml").read_text())
MODEL_NAMES = {entry["model_name"] for entry in CONFIG["model_list"]}
LANES = {"untagged": CATALOG["untagged"], **CATALOG["skills"]}


@pytest.mark.parametrize("name", sorted(LANES))
def test_lane_is_complete(name):
    row = LANES[name]
    assert row["alias"] in MODEL_NAMES, f"{name}: alias {row['alias']!r} missing from litellm/config.yaml"
    assert row["effort"] in EFFORTS
    assert row.get("vendor", "anthropic") in {"anthropic", *_VENDOR_CLIFFS}
    for cap in ("max_input", "max_output"):
        assert isinstance(row[cap], int) and row[cap] > 0


@pytest.mark.parametrize("name", sorted(CATALOG["skills"]))
def test_skill_has_a_valid_hash(name):
    row = CATALOG["skills"][name]
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
    assert isinstance(row["bytes"], int) and row["bytes"] > 0


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
