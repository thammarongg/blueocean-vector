"""Every setting `.env.example` offers either reaches the container or says why not.

docker-compose.yml lists, one by one, the variables it passes to
`blueocean-mcp`. A variable missing from that list does nothing under
compose, however it is set in .env, and nothing reports it. That is how
`BLUEOCEAN_MAX_TOKENS`, `BLUEOCEAN_TOP_K` and the HNSW settings were
silently ignored. This test makes every name in `.env.example` either
passed or excluded here, with the reason.

The numeric settings are parsed with int()/float() at import, so an empty
value would stop the server starting. Compose must use `${VAR:-default}`,
where an empty value also falls back to the default, and that default must
be the code's own.

Run with:
    uv run python -m tests.compose_env
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Named in .env.example, deliberately not passed to the container.
EXCLUDED = {
    "BLUEOCEAN_QDRANT_URL": "compose pins it with --qdrant-url to its own qdrant service",
    "BLUEOCEAN_QDRANT_API_KEY": "compose's qdrant service runs without an API key",
    "BLUEOCEAN_EMBED_MODEL": "the image downloads its model at build time",
    "BLUEOCEAN_EMBED_CACHE_DIR": "the image keeps fastembed's default cache, set at build time",
    # The image installs neither the `openai` nor the `bedrock` extra, so
    # these providers cannot run in the container whatever is passed.
    "BLUEOCEAN_EMBEDDING": "the image only ships fastembed",
    "OPENAI_API_KEY": "the image has no openai SDK",
    "OPENAI_BASE_URL": "the image has no openai SDK",
    "AWS_REGION": "the image has no boto3",
    "BLUEOCEAN_BEDROCK_MODEL_ID": "the image has no boto3",
}

# Passed with a default: compose variable -> the blueocean_mcp.config name it sets.
DEFAULTED = {
    "BLUEOCEAN_MAX_TOKENS": "DEFAULT_MAX_TOKENS",
    "BLUEOCEAN_TOP_K": "DEFAULT_TOP_K",
    "BLUEOCEAN_HNSW_M": "DEFAULT_HNSW_M",
    "BLUEOCEAN_HNSW_EF_CONSTRUCT": "DEFAULT_HNSW_EF_CONSTRUCT",
    "BLUEOCEAN_HEALTH_EMBED_TTL": "DEFAULT_HEALTH_EMBED_TTL",
    "BLUEOCEAN_SESSION_IDLE_SECONDS": "SESSION_IDLE_SECONDS",
}


def env_example() -> dict[str, str]:
    """Every NAME=value in .env.example, commented-out examples included."""
    text = (REPO_ROOT / ".env.example").read_text()
    pattern = re.compile(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*)$", re.MULTILINE)
    return {m[1]: m[2].strip() for m in pattern.finditer(text)}


def compose_environment() -> dict[str, str]:
    # The raw file: `docker compose config` would resolve .env and print the
    # real auth token.
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    entries = compose["services"]["blueocean-mcp"].get("environment") or []
    return dict(str(e).split("=", 1) for e in entries)


def config_values(env_overrides: dict[str, str]) -> dict[str, float]:
    """The DEFAULTED config names as a fresh import sees them."""
    env = {k: v for k, v in os.environ.items() if k not in DEFAULTED}
    env.update(env_overrides)
    names = json.dumps(sorted(set(DEFAULTED.values())))
    code = f"import json, blueocean_mcp.config as c; print(json.dumps({{n: getattr(c, n) for n in {names}}}))"
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, f"importing config failed:\n{out.stderr}"
    return json.loads(out.stdout)


def main() -> None:
    example = env_example()
    passed = compose_environment()

    print("== every .env.example setting reaches the container or is excluded with a reason ==")
    missing = sorted(n for n in example if n not in passed and n not in EXCLUDED)
    assert not missing, (
        f"named in .env.example but never passed to blueocean-mcp, so setting it in .env "
        f"does nothing under compose: {missing}. Pass it in docker-compose.yml, or add it "
        f"to EXCLUDED with the reason."
    )
    both = sorted(n for n in EXCLUDED if n in passed)
    assert not both, f"listed as excluded but passed anyway: {both}"
    stale = sorted(n for n in EXCLUDED if n not in example)
    assert not stale, f"EXCLUDED names that .env.example no longer has: {stale}"
    print(f"  {len(example)} names: {len(example) - len(EXCLUDED)} passed, {len(EXCLUDED)} excluded")

    print("== numeric settings fall back to the code's own default, even when empty ==")
    defaults = config_values({})
    compose_defaults = {}
    for var, attr in DEFAULTED.items():
        value = passed.get(var)
        assert value is not None, f"{var} is not passed to blueocean-mcp"
        m = re.fullmatch(r"\$\{" + var + r":-([^}]*)\}", value)
        assert m, (
            f"{var}={value}: expected ${{{var}:-<default>}}; without ':-' an empty value "
            f"in .env reaches int()/float() and the server does not start"
        )
        compose_defaults[var] = m[1]
        assert float(m[1]) == defaults[attr], (
            f"{var} defaults to {m[1]} in compose but {defaults[attr]} in config.{attr}"
        )
        if example.get(var):
            assert float(example[var]) == defaults[attr], (
                f".env.example shows {var}={example[var]}, the default is {defaults[attr]}"
            )
        print(f"  {var} -> {m[1]} (config.{attr} = {defaults[attr]})")

    print("== each compose default, passed in, parses to the same value ==")
    assert config_values(compose_defaults) == defaults, "a compose default parses differently"
    print("  identical")

    print("\nCOMPOSE ENV TEST PASSED")


if __name__ == "__main__":
    main()
