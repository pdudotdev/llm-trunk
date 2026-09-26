from dataclasses import dataclass

from .cliffs import crosses_vendor_cliff

UNKNOWN_SKILL = "unknown_skill"
STALE_HASH = "stale_hash"
INPUT_CAP = "input_cap"
PRICE_CLIFF = "price_cliff"

# What a request is. Where it goes is its tier.
NORMAL = "normal"
SKILL = "skill"
SUBAGENT = "subagent"
COMPACTION = "compaction"
BACKGROUND = "background"
REQUEST_TYPES = (NORMAL, SKILL, SUBAGENT, COMPACTION, BACKGROUND)


@dataclass
class Decision:
    action: str  # "allow" or "deny"
    reason: str
    code: str | None = None
    tier: str | None = None
    effort: str | None = None
    max_output: int | None = None


def _deny(code: str, reason: str) -> Decision:
    return Decision("deny", reason, code=code)


def tiers(catalog: dict) -> list[str]:
    """Tier names, cheapest first. Explicit in catalog.yaml, never inferred
    from key order: reordering the file must not invert the routing."""
    return list(catalog["order"])


def lowest(catalog: dict) -> str:
    return tiers(catalog)[0]


def one_down(catalog: dict, tier: str) -> str:
    """The next cheaper tier; the lowest tier stays where it is."""
    order = tiers(catalog)
    return order[max(order.index(tier) - 1, 0)]


def decide(
    catalog: dict,
    request_type: str,
    estimated_input_tokens: int,
    *,
    session_tier: str | None = None,
    skill_id: str | None = None,
    skill_hash: str | None = None,
    registered: bool = False,
    agent_tier: str | None = None,
) -> Decision:
    """Pick the tier for one request.

    session_tier: the conversation's current tier (its live sticky route), or
      None when it has none -- then it's on the lowest tier.
    skill_id/skill_hash/registered: for a skill run; only a registered skill
      whose hash matches the catalog gets its catalog tier.
    agent_tier: for a subagent request, the tier its subagent already runs on,
      so all of one subagent's requests share a tier (and a cache).
    """
    current = session_tier or lowest(catalog)
    if request_type == SKILL and registered:
        row = catalog["skills"].get(skill_id)
        if row is None:
            return _deny(UNKNOWN_SKILL, f"llm-trunk: skill {skill_id!r} is not in catalog.yaml")
        if skill_hash is not None and skill_hash != row["sha256"]:
            return _deny(
                STALE_HASH,
                f"llm-trunk: skill {skill_id!r} changed since it was hashed "
                f"(got {skill_hash[:12]}, catalog has {row['sha256'][:12]}) — "
                "re-run scripts/hash_skill.py and update catalog.yaml",
            )
        tier, reason = row["tier"], f"skill:{skill_id}"
    elif request_type == SKILL:
        tier, reason = lowest(catalog), "unregistered skill"
    elif request_type == SUBAGENT:
        tier = agent_tier or one_down(catalog, current)
        reason = "subagent"
    elif request_type in (COMPACTION, BACKGROUND):
        # Both carry the whole conversation: on the current tier's model it's
        # a cache read, anywhere else a full re-cache.
        tier, reason = current, request_type
    else:
        tier, reason = current, "sticky" if session_tier else "untagged"

    row = catalog["tiers"][tier]
    # Reasons are shown to the user by Claude Code, so each says what to do.
    vendor = row.get("vendor")
    if crosses_vendor_cliff(vendor, estimated_input_tokens):
        return _deny(
            PRICE_CLIFF,
            f"llm-trunk: ~{estimated_input_tokens} input tokens crosses the {vendor} "
            "price cliff — run /compact or start a new session",
        )
    # Compaction is how an oversized conversation shrinks, so it's never capped.
    if request_type != COMPACTION and estimated_input_tokens >= row["max_input"]:
        return _deny(
            INPUT_CAP,
            f"llm-trunk: ~{estimated_input_tokens} input tokens exceeds the {tier} tier cap "
            f"({row['max_input']}) — run /compact or start a new session",
        )
    return Decision("allow", reason, tier=tier, effort=row["effort"], max_output=row["max_output"])
