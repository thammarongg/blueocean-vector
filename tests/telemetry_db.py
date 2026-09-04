"""Tests for the telemetry SQLite layer: schema creation, additive
migrations, retention purge, and multi-process writes.

Run with:
    uv run python -m tests.telemetry_db
"""

import importlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from blueocean_mcp import config
from blueocean_mcp.telemetry import db, writer


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


def test_writer_persists_rows() -> None:
    print("== writer thread drains the queue into SQLite ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        w = writer.TelemetryWriter(path)
        w.start()
        try:
            now = int(time.time())
            w.record({"ts": now, "kind": "tool", "tool": "memory_search", "ok": 1,
                      "origin": "observed", "result_count": 3})
            w.flush()
        finally:
            w.stop()
        conn = db.connect(path)
        row = conn.execute("SELECT tool, result_count FROM events").fetchone()
        assert row == ("memory_search", 3), row
        conn.close()
    print("  OK")


def test_writer_drops_oldest_when_full_and_counts() -> None:
    """A full queue must never block a memory operation. Dropping is the
    correct behaviour; hiding that it happened is not."""
    print("== full queue drops oldest and counts the drops ==")
    with tempfile.TemporaryDirectory() as d:
        w = writer.TelemetryWriter(str(Path(d) / "t.db"), queue_size=2)
        # Not started: nothing drains, so the queue fills deterministically.
        for i in range(5):
            w.record({"ts": i, "kind": "tool", "tool": "memory_store", "ok": 1})
        assert w.dropped == 3, w.dropped
    print("  OK")


def test_writer_self_disables_on_failure() -> None:
    """A broken telemetry backend must degrade to silence, never to an
    exception on the memory path."""
    print("== a failing writer self-disables instead of raising ==")
    with tempfile.TemporaryDirectory() as d:
        w = writer.TelemetryWriter(str(Path(d) / "t.db"))
        w.start()
        try:
            w.record({"ts": 1, "kind": "tool", "tool": "memory_store",
                      "nonexistent_column": "boom"})
            w.flush()
            assert w.disabled is True, "writer should have disabled itself"
            w.record({"ts": 2, "kind": "tool", "tool": "memory_store"})  # must not raise
        finally:
            w.stop()
    print("  OK")


def test_entry_hits_upsert_and_delete() -> None:
    print("== entry_hits accumulates and deletes ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        w = writer.TelemetryWriter(path)
        w.start()
        try:
            w.record_hits("proj", ["a", "b"], ["a"])
            w.record_hits("proj", ["a"], [])
            w.flush()
            conn = db.connect(path)
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute("SELECT point_id, hits, full_hits FROM entry_hits")
            }
            assert rows["a"] == (2, 1), rows
            assert rows["b"] == (1, 0), rows
            conn.close()

            w.delete_hits("proj", ["a"])
            w.flush()
            conn = db.connect(path)
            left = [r[0] for r in conn.execute("SELECT point_id FROM entry_hits")]
            assert left == ["b"], left
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_disabled_touches_no_file() -> None:
    print("== BLUEOCEAN_TELEMETRY=0 opens no database ==")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        os.environ["BLUEOCEAN_TELEMETRY"] = "0"
        try:
            importlib.reload(config)
            importlib.reload(writer)
            assert writer.is_enabled() is False
            assert writer.get_writer() is None
            assert not path.exists()
        finally:
            os.environ.pop("BLUEOCEAN_TELEMETRY")
            importlib.reload(config)
            importlib.reload(writer)
    print("  OK")


def test_migration_v2_nulls_leaked_cli_reported_error_msg() -> None:
    """PRIVACY REGRESSION for schema v2. Version 1 stored caller-supplied
    error_msg on cli-reported audit rows verbatim, so rows already on disk
    may carry memory or query text for the rest of the 90-day retention.

    Break this test catches: a migration that (a) leaves the leaked
    cli-reported error_msg in place, (b) also destroys server-observed error
    messages, which are sanitized on the way in and safe to keep, (c) fails
    to advance user_version so every connect re-runs the sweep, or (d) is not
    idempotent on a second open.
    """
    print("== v1->v2 migration nulls only cli-reported error_msg ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        # Build a version 1 database: v1 schema plus the leaked rows, then
        # stamp it back to v1 the way an old deployment would be on disk.
        conn = db.connect(path)
        now = int(time.time())
        conn.execute(
            "INSERT INTO events (ts, kind, tool, ok, error_class, error_msg, origin)"
            " VALUES (?,?,?,?,?,?,?)",
            (now, "admin", "prune", 0, "RuntimeError",
             "leaked caller text 'SELECT secret'", "cli-reported"),
        )
        conn.execute(
            "INSERT INTO events (ts, kind, tool, ok, error_class, error_msg, origin)"
            " VALUES (?,?,?,?,?,?,?)",
            (now, "tool", "memory_search", 0, "ValueError",
             "server-observed message, sanitized on the way in", "observed"),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        conn = db.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION, (
            "migration must advance user_version so the data sweep runs once"
        )
        rows = {
            r[0]: r[1] for r in conn.execute(
                "SELECT origin, error_msg FROM events"
            )
        }
        assert rows["cli-reported"] is None, (
            f"v1 leaked caller text must be nulled, got {rows['cli-reported']!r}"
        )
        assert rows["observed"] == "server-observed message, sanitized on the way in", (
            "server-observed error messages are safe and must survive the migration"
        )
        conn.close()

        # Reconnecting an already-migrated database must be harmless.
        conn = db.connect(path)
        rows = {
            r[0]: r[1] for r in conn.execute(
                "SELECT origin, error_msg FROM events"
            )
        }
        assert rows["observed"] == "server-observed message, sanitized on the way in"
        assert rows["cli-reported"] is None
        conn.close()
    print("  OK")


def main() -> None:
    test_connect_creates_schema()
    test_connect_is_idempotent()
    test_migration_is_additive()
    test_purge_old_deletes_only_expired_events()
    test_writer_persists_rows()
    test_writer_drops_oldest_when_full_and_counts()
    test_writer_self_disables_on_failure()
    test_entry_hits_upsert_and_delete()
    test_disabled_touches_no_file()
    test_migration_v2_nulls_leaked_cli_reported_error_msg()
    print("\nTELEMETRY DB TEST PASSED")


if __name__ == "__main__":
    main()
