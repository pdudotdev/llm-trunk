from dataclasses import dataclass

from .cliffs import crosses_vendor_cliff


@dataclass
class Decision:
    action: str  # "allow" or "deny"
    alias: str | None
    effort: str | None
    reason: str


def _cliff_reason(row: dict, estimated_input_tokens: int) -> str | None:
    vendor = row["vendor"]
    if crosses_vendor_cliff(vendor, estimated_input_tokens):
        return f"vendor price cliff crossed for {vendor} at {estimated_input_tokens} tokens"
    return None


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

        cliff_reason = _cliff_reason(row, estimated_input_tokens)
        if cliff_reason is not None:
            return Decision("deny", None, None, cliff_reason)

        if estimated_input_tokens >= row["max_input"]:
            return Decision(
                "deny",
                None,
                None,
                f"estimated input {estimated_input_tokens} >= untagged max_input {row['max_input']}",
            )
        return Decision("allow", row["alias"], row["effort"], "untagged")

    row = catalog["skills"].get(effective_skill_id)
    if row is None:
        return Decision("deny", None, None, f"unknown skill_id: {effective_skill_id}")

    if skill_hash is not None and skill_hash != row["sha256"]:
        return Decision("deny", None, None, f"stale skill hash for {effective_skill_id}")

    cliff_reason = _cliff_reason(row, estimated_input_tokens)
    if cliff_reason is not None:
        return Decision("deny", None, None, cliff_reason)

    if estimated_input_tokens >= row["max_input"]:
        return Decision(
            "deny",
            None,
            None,
            f"estimated input {estimated_input_tokens} >= max_input {row['max_input']} for {effective_skill_id}",
        )

    return Decision("allow", row["alias"], row["effort"], f"tagged:{effective_skill_id}")
