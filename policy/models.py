import re


def model_key(model: str | None) -> str | None:
    """Any spelling of a Claude model id -> its family, e.g.
    anthropic/claude-opus-5-5[1m], claude-haiku-4-5-20251001 and
    us.anthropic.claude-haiku-4-5-20251001-v1:0 -> claude-opus-5-5 / claude-haiku-4-5.

    Shared by the gateway and the scripts, so both read model ids the same way.
    """
    if not model:
        return None
    name = model.lower().split("/")[-1].replace("[1m]", "")
    name = re.sub(r"^(?:[a-z-]+\.)?anthropic\.", "", name)  # Bedrock: us.anthropic.claude-...
    name = re.sub(r"-v\d+(?::\d+)?$", "", name)  # Bedrock: ...-v1:0
    return re.sub(r"-\d{8}$", "", name)  # dated snapshot: ...-20251001
