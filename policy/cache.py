"""Prompt-cache economics, shared by the gateway and the scripts.

Anthropic caches each model's view of a conversation separately, so moving a
conversation to another tier's model makes that model load all of it again at
the cache-write price, where staying would have read it back at the far lower
cache-read price. Moving up is a quality requirement and always happens; moving
down only saves money, so it waits until it pays for that reload:

    stay    = this prompt on the warm tier, the conversation read from cache
    move    = this prompt on the cheaper tier, only what that model has cached
    penalty = move - stay
    held    = what staying has cost so far, vs the cheaper tier (actual usage)

    move when the warm tier's cache has expired, or once held >= penalty

It is the rent-or-buy rule: keep renting until the rent paid equals the price
of buying. It never costs more than twice the best choice in hindsight, and a
hold that ends at a pause (the cache expires, moving is free) costs nothing.
"""
from pathlib import Path

import yaml


def load_prices(path: str | Path) -> dict[str, dict]:
    """Model family -> list prices in USD per million tokens (pricing.yaml)."""
    return yaml.safe_load(Path(path).read_text())["models"]


def token_cost(event: dict, price: dict) -> float | None:
    """A request's logged tokens priced at one model's list prices, in USD."""
    total, output = event.get("input_tokens"), event.get("output_tokens")
    if not isinstance(total, (int, float)) or not isinstance(output, (int, float)):
        return None
    read = event.get("cache_read_tokens") or 0
    write = event.get("cache_write_tokens") or 0
    fresh = max(0, total - read - write)  # LiteLLM's input count includes both
    return (
        fresh * price["input"] + read * price["cache_read"] + write * price["cache_write"] + output * price["output"]
    ) / 1_000_000


def prompt_cost(price: dict, tokens: float, cached: float) -> float:
    """The input side of one request: `cached` tokens read back from the cache,
    the rest written to it (Claude Code marks its prompts for caching)."""
    cached = min(max(cached, 0), tokens)
    return (cached * price["cache_read"] + (tokens - cached) * price["cache_write"]) / 1_000_000


def hold_or_move(penalty: float, held: float) -> bool:
    """True to stay on the warm tier: moving still costs more than holding has."""
    return penalty > held
