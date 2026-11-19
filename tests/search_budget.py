"""The default search budget is 4000 tokens, and the container can change it.

`allocate()` gives only ~40% of the budget to full content. At 2000 tokens a
test query got full content for 2 of its top 5 hits; at 4000 it got all 5.
So the default is 4000.

`BLUEOCEAN_MAX_TOKENS` is read once at import, so each check runs in a fresh
interpreter with an explicit environment. docker-compose.yml must also pass
the variable to `blueocean-mcp`: without that, setting it in .env does
nothing in the container.

Run with:
    uv run python -m tests.search_budget
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_DEFAULT = 4000
EXPECTED_COMPOSE_ENTRY = "BLUEOCEAN_MAX_TOKENS=${BLUEOCEAN_MAX_TOKENS:-4000}"


def default_max_tokens(env_value: str | None) -> int:
    """DEFAULT_MAX_TOKENS as a fresh import sees it, with the variable set or removed."""
    env = {k: v for k, v in os.environ.items() if k != "BLUEOCEAN_MAX_TOKENS"}
    if env_value is not None:
        env["BLUEOCEAN_MAX_TOKENS"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", "from blueocean_mcp.config import DEFAULT_MAX_TOKENS as d; print(d)"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"importing config failed:\n{out.stderr}"
    return int(out.stdout.strip())


def main() -> None:
    print("== DEFAULT_MAX_TOKENS is 4000 when BLUEOCEAN_MAX_TOKENS is unset ==")
    got = default_max_tokens(None)
    assert got == EXPECTED_DEFAULT, f"default budget is {got}, expected {EXPECTED_DEFAULT}"
    print(f"  unset -> {got}")

    print("== BLUEOCEAN_MAX_TOKENS overrides the default ==")
    got = default_max_tokens("1234")
    assert got == 1234, f"BLUEOCEAN_MAX_TOKENS=1234 gave {got}"
    print(f"  BLUEOCEAN_MAX_TOKENS=1234 -> {got}")

    print("== docker-compose.yml passes BLUEOCEAN_MAX_TOKENS to blueocean-mcp ==")
    # The raw file on purpose: `docker compose config` would resolve .env and
    # print the real auth token.
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    environment = compose["services"]["blueocean-mcp"].get("environment") or []
    entries = [e for e in environment if str(e).startswith("BLUEOCEAN_MAX_TOKENS=")]
    assert entries, (
        "blueocean-mcp's environment does not pass BLUEOCEAN_MAX_TOKENS, so a "
        "value in .env never reaches the container"
    )
    assert entries == [EXPECTED_COMPOSE_ENTRY], (
        f"expected exactly {EXPECTED_COMPOSE_ENTRY!r}, got {entries!r}"
    )
    print(f"  {entries[0]}")

    print("\nSEARCH BUDGET TEST PASSED")


if __name__ == "__main__":
    main()
