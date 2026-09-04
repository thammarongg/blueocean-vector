"""Tests for the admin CLI's telemetry commands.

The rule under test: the CLI reads over HTTP and never silently falls back to
opening the SQLite file. A transient connection failure while the container is
running would otherwise open a bind-mounted database underneath an active
writer, which is the exact failure the single-writer design prevents.

Run with:
    uv run python -m tests.telemetry_cli
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from blueocean_mcp.telemetry import client, db

from ._helpers import REPO_ROOT

ADMIN = str(REPO_ROOT / ".venv" / "bin" / "blueocean-admin")
DEAD_PORT = 8799  # nothing listens here


def _run(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [ADMIN, *args], env=env, capture_output=True, text=True, check=False
    )


def test_usage_fails_loudly_when_the_server_is_down() -> None:
    print("== usage fails loudly rather than opening the file ==")
    with tempfile.TemporaryDirectory() as d:
        db_file = Path(d) / "t.db"
        result = _run(
            ["usage"],
            {
                "BLUEOCEAN_SERVER_URL": f"http://127.0.0.1:{DEAD_PORT}",
                "BLUEOCEAN_TELEMETRY_DB": str(db_file),
            },
        )
        assert result.returncode != 0, result.stdout
        assert "could not reach" in result.stderr.lower(), result.stderr
        assert not db_file.exists(), (
            "no silent fallback: the CLI must not open the database file"
        )
    print("  OK")


def test_usage_db_flag_reads_the_file_directly() -> None:
    print("== --db opts explicitly into reading the file ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO events (ts, kind, tool, ok, total_ms, origin)"
            " VALUES (?, 'tool', 'memory_search', 1, 12.0, 'observed')",
            (int(time.time()),),
        )
        conn.commit()
        conn.close()

        result = _run(["usage", "--db", path, "--json"])
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["tiles"]["calls"] == 1, body
    print("  OK")


def test_usage_table_output_is_the_default() -> None:
    print("== default output is a table, not raw JSON ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO events (ts, kind, tool, ok, total_ms, origin)"
            " VALUES (?, 'tool', 'memory_search', 1, 12.0, 'observed')",
            (int(time.time()),),
        )
        conn.commit()
        conn.close()

        result = _run(["usage", "--db", path])
        assert result.returncode == 0, result.stderr
        assert "memory_search" in result.stdout, result.stdout
        assert not result.stdout.lstrip().startswith("{"), result.stdout
    print("  OK")


def test_server_unavailable_is_raised_not_swallowed() -> None:
    print("== the client surfaces unavailability as an exception ==")
    raised = False
    try:
        client.fetch_stats(days=7, tz_offset_minutes=0, project=None,
                           server_url=f"http://127.0.0.1:{DEAD_PORT}", token=None)
    except client.ServerUnavailable:
        raised = True
    assert raised, "fetch_stats must raise rather than return empty data"
    print("  OK")


def main() -> None:
    test_usage_fails_loudly_when_the_server_is_down()
    test_usage_db_flag_reads_the_file_directly()
    test_usage_table_output_is_the_default()
    test_server_unavailable_is_raised_not_swallowed()
    print("\nTELEMETRY CLI TEST PASSED")


if __name__ == "__main__":
    main()
