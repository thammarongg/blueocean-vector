"""Where a price comes from, and what it costs.

Resolution order is env, then the pricing file, then the built-in table, then
nothing. The resolved price and its source are snapshotted onto every event
row, so history stays truthful after the table is updated and a suspicious
number can be traced to an override, a bad feed, or a stale table.
"""

import json
import os
import re
import time
from pathlib import Path

from ..config import DEFAULT_PRICING_FILE

# USD per 1M input tokens. Verified from primary sources on 2026-09-02:
# OpenAI figures from developers.openai.com/api/docs/pricing; the Bedrock
# figure from the AWS Price List API (GetProducts, ServiceCode=AmazonBedrock,
# titanModel=TitanEmbeddingsV2-Text-input, regionCode=us-east-1), which
# returns $0.00002 per 1K tokens on demand.
BUILTIN_PRICES: dict[tuple[str, str], float] = {
    ("openai", "text-embedding-3-small"): 0.02,
    ("openai", "text-embedding-3-large"): 0.13,
    ("openai", "text-embedding-ada-002"): 0.10,
    ("bedrock", "amazon.titan-embed-text-v2:0"): 0.02,
}

# Local providers cost nothing, whatever a hosted feed says about the same
# model name. multilingual-e5-large is on OpenRouter at $0.01 per 1M, but
# running it through fastembed is free, and pricing it from the feed would
# invent a bill that does not exist.
_LOCAL_PROVIDERS = frozenset({"fastembed"})

_ENV_SAFE = re.compile(r"[^A-Z0-9]+")


def _env_key(provider: str, model: str) -> str:
    return "BLUEOCEAN_PRICE_" + _ENV_SAFE.sub("_", f"{provider}_{model}".upper()).strip("_")


def load_file(path: str | None = None) -> dict[str, float]:
    target = Path(path or DEFAULT_PRICING_FILE).expanduser()
    try:
        body = json.loads(target.read_text())
    except (OSError, ValueError):
        return {}
    prices = body.get("prices", {})
    return {k: float(v) for k, v in prices.items() if isinstance(v, (int, float))}


def write_file(path: str | None, prices: dict[str, float]) -> None:
    """Write the pricing file atomically: temp file in the same directory,
    then rename, so a concurrent reader never sees half a file."""
    target = Path(path or DEFAULT_PRICING_FILE).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {"fetched_at": int(time.time()), "source": "openrouter", "prices": prices}
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(target)


def resolve(
    provider: str, model: str, pricing_file: str | None = None
) -> tuple[float | None, str | None]:
    """Return (USD per 1M input tokens, source) or (None, None) when unknown."""
    override = os.getenv(_env_key(provider, model))
    if override is not None:
        try:
            return float(override), "env"
        except ValueError:
            pass  # a malformed override falls through rather than crashing a tool call

    if provider in _LOCAL_PROVIDERS:
        return 0.0, "builtin"

    if provider != "bedrock":
        # OpenRouter ids are "<vendor>/<model>", which matches OpenAI's model
        # names directly. Bedrock is not carried by the feed at all.
        from_file = load_file(pricing_file).get(f"{provider}/{model}")
        if from_file is not None:
            return from_file, "openrouter"

    builtin = BUILTIN_PRICES.get((provider, model))
    if builtin is not None:
        return builtin, "builtin"
    return None, None


def cost_usd(tokens: int | None, price_per_1m: float | None) -> float | None:
    if tokens is None or price_per_1m is None:
        return None
    return tokens / 1_000_000 * price_per_1m
