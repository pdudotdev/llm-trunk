GROK_GEMINI_CLIFF_INPUT_TOKENS = 200_000
OPENAI_ASTRA_CLIFF_INPUT_TOKENS = 272_000

_VENDOR_CLIFFS = {
    "grok": GROK_GEMINI_CLIFF_INPUT_TOKENS,
    "gemini": GROK_GEMINI_CLIFF_INPUT_TOKENS,
    "openai": OPENAI_ASTRA_CLIFF_INPUT_TOKENS,
}


def crosses_vendor_cliff(vendor: str, estimated_input_tokens: int) -> bool:
    cliff = _VENDOR_CLIFFS.get(vendor)
    if cliff is None:
        return False
    return estimated_input_tokens >= cliff
