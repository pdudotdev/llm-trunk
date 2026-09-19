from dataclasses import dataclass

from .cliffs import crosses_vendor_cliff

UNKNOWN_SKILL = "unknown_skill"
STALE_HASH = "stale_hash"
INPUT_CAP = "input_cap"
PRICE_CLIFF = "price_cliff"


@dataclass
class Decision:
    action: str  # "allow" or "deny"
    reason: str
    code: str | None = None
    alias: str | None = None
    effort: str | None = None
    max_output: int | None = None


def _deny(code: str, reason: str) -> Decision:
    return Decision("deny", reason, code=code)


def decide(
    catalog: dict,
    skill_id: str | None,
    skill_hash: str | None,
    sticky_skill_id: str | None,
    estimated_input_tokens: int,
) -> Decision:
    effective_skill_id = skill_id or sticky_skill_id

    if effective_skill_id is None:
        row = catalog["untagged"]
        label = "untagged"
    else:
        row = catalog["skills"].get(effective_skill_id)
        if row is None:
            return _deny(UNKNOWN_SKILL, f"unknown skill_id: {effective_skill_id}")
        if skill_hash is not None and skill_hash != row["sha256"]:
            return _deny(
                STALE_HASH,
                f"stale skill hash for {effective_skill_id}: "
                f"got {skill_hash}, catalog has {row['sha256']}",
            )
        label = f"tagged:{effective_skill_id}"

    vendor = row.get("vendor")
    if crosses_vendor_cliff(vendor, estimated_input_tokens):
        return _deny(
            PRICE_CLIFF,
            f"vendor price cliff crossed for {vendor} at {estimated_input_tokens} tokens",
        )

    if estimated_input_tokens >= row["max_input"]:
        return _deny(
            INPUT_CAP,
            f"estimated input {estimated_input_tokens} >= max_input {row['max_input']} for {label}",
        )

    return Decision(
        "allow",
        label,
        alias=row["alias"],
        effort=row["effort"],
        max_output=row["max_output"],
    )
