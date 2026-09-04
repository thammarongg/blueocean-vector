"""SQLite schema and connection handling for telemetry.

Schema migrations are additive only: a version mismatch never drops columns
or rows, because the 90 days of history is the entire point of the feature
and a `docker compose pull` must not silently erase it. The one exception is
a data migration that removes values that should never have been stored
(v2 nulls caller-supplied error_msg on cli-reported rows); that is a privacy
fix, not schema churn.
"""

import sqlite3
import time
from pathlib import Path

from ..config import DEFAULT_TELEMETRY_DB

SCHEMA_VERSION = 2

# Version-gated data migrations, keyed by the version they upgrade FROM.
# Each statement runs once per database lifetime: after the walk below,
# user_version equals SCHEMA_VERSION, so a reopened database never repeats
# it. A fresh database (user_version 0) is treated as upgrading from v1, so
# its statements do run - harmlessly, against a table created empty one
# screen above - rather than special-casing 0.
#
# v1 -> v2: cli-reported audit rows stored the caller's error_msg verbatim
# (routes.py now discards it), so rows already on disk may carry memory or
# query text for the rest of the 90-day retention. The sweep nulls exactly
# those values. Server-observed error messages are written by instrument.py
# only after sanitization and are deliberately kept.
_DATA_MIGRATIONS: dict[int, list[str]] = {
    1: [
        (
            "UPDATE events SET error_msg = NULL"
            " WHERE origin = 'cli-reported' AND error_msg IS NOT NULL"
        ),
    ],
}

# (column name, SQL type). Migrations walk this list and ALTER TABLE ADD any
# column an older database is missing, so adding a dimension later is a
# one-line change here rather than a schema rewrite.
_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("ts", "INTEGER NOT NULL"),
    ("kind", "TEXT"),
    ("tool", "TEXT"),
    ("project", "TEXT"),
    ("area", "TEXT"),
    ("module", "TEXT"),
    ("agent_name", "TEXT"),
    ("agent_version", "TEXT"),
    ("session_id", "TEXT"),
    ("agent_session_label", "TEXT"),
    ("ok", "INTEGER"),
    ("error_class", "TEXT"),
    ("error_msg", "TEXT"),
    ("total_ms", "REAL"),
    ("embed_ms", "REAL"),
    ("result_count", "INTEGER"),
    ("top_score", "REAL"),
    ("tokens_returned", "INTEGER"),
    ("embed_tokens", "INTEGER"),
    ("tokens_exact", "INTEGER"),
    ("est_cost_usd", "REAL"),
    ("unit_price_per_1m", "REAL"),
    ("price_source", "TEXT"),
    ("deleted_count", "INTEGER"),
    ("origin", "TEXT"),
]

_INDEXES = [
    ("idx_events_ts", "events (ts)"),
    ("idx_events_tool_ts", "events (tool, ts)"),
    ("idx_events_project_ts", "events (project, ts)"),
    ("idx_events_agent_ts", "events (agent_name, ts)"),
]


def db_path() -> str:
    return DEFAULT_TELEMETRY_DB


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the telemetry database, with the PRAGMAs and
    schema this project needs.

    WAL plus busy_timeout is for the several stdio development processes that
    may share one file on a single host. It is deliberately not relied on
    across a Docker bind mount, where SQLite's locking is not trustworthy;
    see the spec's single-writer rule.
    """
    target = path or db_path()
    Path(target).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(Path(target).expanduser()), timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    from_version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)"
    )
    existing = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    for name, decl in _EVENT_COLUMNS:
        if name in existing:
            continue
        # NOT NULL is dropped when adding to a populated table: SQLite forbids
        # adding a NOT NULL column without a default, and pre-existing rows
        # legitimately have no value for a dimension that did not exist yet.
        conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl.replace(' NOT NULL', '')}")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS entry_hits ("
        " project TEXT NOT NULL,"
        " point_id TEXT NOT NULL,"
        " hits INTEGER NOT NULL DEFAULT 0,"
        " full_hits INTEGER NOT NULL DEFAULT 0,"
        " last_seen_at INTEGER,"
        " PRIMARY KEY (project, point_id))"
    )
    for name, target in _INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    # A fresh database (0) is clamped to 1: it walks the same sweep chain
    # as a real v1 database, and the v1 UPDATE simply matches nothing
    # against the empty table created just above.
    for version in range(max(from_version, 1), SCHEMA_VERSION):
        for statement in _DATA_MIGRATIONS.get(version, []):
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def purge_old(
    conn: sqlite3.Connection, retention_days: int, now: int | None = None
) -> int:
    """Delete events older than the retention window and return the count.

    `entry_hits` is never purged. It holds one row per memory entry rather
    than one per event, so it does not grow with traffic, and "last retrieved
    eight months ago" is exactly the signal that makes an entry safe to prune.
    """
    cutoff = (now if now is not None else int(time.time())) - retention_days * 86400
    cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount
