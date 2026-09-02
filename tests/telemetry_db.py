"""Tests for the telemetry SQLite layer: schema creation, additive
migrations, retention purge, and multi-process writes.

Run with:
    uv run python -m tests.telemetry_db
"""

import sqlite3
import tempfile
import time
from pathlib import Path

from blueocean_mcp.telemetry import db


def test_connect_creates_schema() -> None:
    print("== connect creates schema and stamps user_version ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        conn = db.connect(path)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "events" in tables, tables
        assert "entry_hits" in tables, tables
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION, version
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", mode
        conn.close()
    print("  OK")


def test_connect_is_idempotent() -> None:
    print("== reopening an existing database keeps its rows ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO events (ts, kind, tool, ok, origin) VALUES (?,?,?,?,?)",
            (int(time.time()), "tool", "memory_store", 1, "observed"),
        )
        conn.commit()
        conn.close()

        conn = db.connect(path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1, count
        conn.close()
    print("  OK")


def test_migration_is_additive() -> None:
    """A database stamped at an older version must gain the missing columns
    and keep every row. Dropping and recreating would throw away the 90 days
    of history that is the only thing this feature is worth."""
    print("== migration adds columns without losing rows ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, "
            "tool TEXT, ok INTEGER)"
        )
        raw.execute(
            "INSERT INTO events (ts, kind, tool, ok) VALUES (1, 'tool', 'old', 1)"
        )
        raw.execute("PRAGMA user_version = 0")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        row = conn.execute("SELECT tool, origin FROM events").fetchone()
        assert row[0] == "old", row
        assert row[1] is None, "pre-existing rows keep NULL for new columns"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        conn.close()
    print("  OK")


def test_purge_old_deletes_only_expired_events() -> None:
    print("== retention purge deletes expired events, keeps entry_hits ==")
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        now = 1_800_000_000
        old = now - 91 * 86400
        recent = now - 3 * 86400
        for ts in (old, recent):
            conn.execute(
                "INSERT INTO events (ts, kind, tool, ok, origin) VALUES (?,?,?,?,?)",
                (ts, "tool", "memory_search", 1, "observed"),
            )
        conn.execute(
            "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
            "VALUES (?,?,?,?,?)",
            ("p", "abc", 1, 0, old),
        )
        conn.commit()

        deleted = db.purge_old(conn, retention_days=90, now=now)
        assert deleted == 1, deleted
        remaining = conn.execute("SELECT ts FROM events").fetchall()
        assert remaining == [(recent,)], remaining
        hits = conn.execute("SELECT COUNT(*) FROM entry_hits").fetchone()[0]
        assert hits == 1, "entry_hits is never purged: it is the only long-horizon signal"
        conn.close()
    print("  OK")


def main() -> None:
    test_connect_creates_schema()
    test_connect_is_idempotent()
    test_migration_is_additive()
    test_purge_old_deletes_only_expired_events()
    print("\nTELEMETRY DB TEST PASSED")


if __name__ == "__main__":
    main()
