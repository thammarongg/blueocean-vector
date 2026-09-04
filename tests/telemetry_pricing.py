"""Tests for price resolution.

Run with:
    uv run python -m tests.telemetry_pricing
"""

import json
import os
import tempfile
from pathlib import Path

from blueocean_mcp.telemetry import pricing


def test_builtin_prices_match_the_spec() -> None:
    print("== built-in table matches the figures verified on 2026-09-02 ==")
    assert pricing.BUILTIN_PRICES[("openai", "text-embedding-3-small")] == 0.02
    assert pricing.BUILTIN_PRICES[("openai", "text-embedding-3-large")] == 0.13
    assert pricing.BUILTIN_PRICES[("openai", "text-embedding-ada-002")] == 0.10
    assert pricing.BUILTIN_PRICES[("bedrock", "amazon.titan-embed-text-v2:0")] == 0.02
    print("  OK")


def test_resolution_order() -> None:
    print("== env beats pricing file beats built-in table ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "pricing.json")
        pricing.write_file(path, {"openai/text-embedding-3-small": 0.05})

        price, source = pricing.resolve("openai", "text-embedding-3-small", path)
        assert (price, source) == (0.05, "openrouter"), (price, source)

        os.environ["BLUEOCEAN_PRICE_OPENAI_TEXT_EMBEDDING_3_SMALL"] = "0.99"
        try:
            price, source = pricing.resolve("openai", "text-embedding-3-small", path)
            assert (price, source) == (0.99, "env"), (price, source)
        finally:
            os.environ.pop("BLUEOCEAN_PRICE_OPENAI_TEXT_EMBEDDING_3_SMALL")

        price, source = pricing.resolve("openai", "text-embedding-3-large", path)
        assert (price, source) == (0.13, "builtin"), (price, source)
    print("  OK")


def test_unknown_model_is_null_not_zero() -> None:
    """NULL means "we do not know". fastembed's 0.00 means "genuinely free".
    Folding one into the other produces a confidently wrong cost total."""
    print("== an unknown model yields NULL, not 0 ==")
    price, source = pricing.resolve("openai", "text-embedding-9-imaginary", None)
    assert price is None, price
    assert source is None, source
    assert pricing.cost_usd(1000, None) is None
    print("  OK")


def test_fastembed_is_free_not_priced_from_the_feed() -> None:
    """intfloat/multilingual-e5-large is on OpenRouter, but running it locally
    through fastembed costs nothing. The feed must not price a local model."""
    print("== fastembed is always 0.00 from the built-in table ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "pricing.json")
        pricing.write_file(path, {"intfloat/multilingual-e5-large": 0.01})
        price, source = pricing.resolve("fastembed", "intfloat/multilingual-e5-large", path)
        assert (price, source) == (0.0, "builtin"), (price, source)
    print("  OK")


def test_cost_math() -> None:
    print("== cost is tokens / 1M * price ==")
    assert pricing.cost_usd(1_000_000, 0.02) == 0.02
    assert pricing.cost_usd(500_000, 0.02) == 0.01
    assert pricing.cost_usd(None, 0.02) is None
    print("  OK")


def test_write_file_is_atomic_and_readable() -> None:
    print("== pricing file writes atomically ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "sub" / "pricing.json")
        pricing.write_file(path, {"openai/text-embedding-3-small": 0.02})
        body = json.loads(Path(path).read_text())
        assert body["prices"]["openai/text-embedding-3-small"] == 0.02, body
        assert "fetched_at" in body, body
        assert not list(Path(path).parent.glob("*.tmp*")), "temp file left behind"
        assert pricing.load_file(path) == {"openai/text-embedding-3-small": 0.02}
    print("  OK")


def test_parse_openrouter_converts_per_token_to_per_million() -> None:
    """The feed quotes USD per token as a string. Our table is USD per 1M."""
    print("== OpenRouter per-token prices convert to per-1M ==")
    payload = {"data": [
        {"id": "openai/text-embedding-3-small", "pricing": {"prompt": "0.00000002"}},
        {"id": "openai/text-embedding-3-large", "pricing": {"prompt": "0.00000013"}},
        {"id": "openai/text-embedding-ada-002", "pricing": {"prompt": "0.0000001"}},
        {"id": "liquid/lfm-2.5-embedding-350m:free", "pricing": {"prompt": "0"}},
        {"id": "broken/model", "pricing": {}},
    ]}
    prices = pricing.parse_openrouter(payload)
    assert abs(prices["openai/text-embedding-3-small"] - 0.02) < 1e-9, prices
    assert abs(prices["openai/text-embedding-3-large"] - 0.13) < 1e-9, prices
    assert abs(prices["openai/text-embedding-ada-002"] - 0.10) < 1e-9, prices
    assert "broken/model" not in prices, "a model with no price is skipped, not zeroed"
    print("  OK")


def test_free_models_are_flagged_not_just_zero() -> None:
    """`:free` OpenRouter models state that requests and embeddings may be
    retained for training. A displayed "$0" without that context is a trap for
    a project whose whole premise is that memory content stays local."""
    print("== :free models are marked as data-retaining ==")
    payload = {"data": [
        {"id": "liquid/lfm-2.5-embedding-350m:free", "pricing": {"prompt": "0"}},
    ]}
    prices = pricing.parse_openrouter(payload)
    assert pricing.retains_data("liquid/lfm-2.5-embedding-350m:free") is True
    assert pricing.retains_data("openai/text-embedding-3-small") is False
    assert prices["liquid/lfm-2.5-embedding-350m:free"] == 0.0
    print("  OK")


def main() -> None:
    test_builtin_prices_match_the_spec()
    test_resolution_order()
    test_unknown_model_is_null_not_zero()
    test_fastembed_is_free_not_priced_from_the_feed()
    test_cost_math()
    test_write_file_is_atomic_and_readable()
    test_parse_openrouter_converts_per_token_to_per_million()
    test_free_models_are_flagged_not_just_zero()
    print("\nTELEMETRY PRICING TEST PASSED")


if __name__ == "__main__":
    main()
