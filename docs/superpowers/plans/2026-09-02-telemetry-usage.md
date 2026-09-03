# Telemetry and Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make memory usage observable by recording every MCP tool call to a local SQLite event log and exposing it through an MCP tool, an admin CLI, and a single-file dashboard.

**Architecture:** One wrapper around all registered MCP tools writes rows onto a bounded in-memory queue drained by a background thread into SQLite. Every reader is an aggregation over that one table. The server process is the only process that opens the database file; the CLI reads over HTTP.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `contextvars`, `threading`; Starlette routes on the existing `mcp` streamable-http app; vanilla JS and inline SVG for the dashboard. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-09-02-telemetry-usage-design.md`

## Global Constraints

- **Privacy, hard rule.** Telemetry never stores query text, memory content, summaries, entry metadata, or bearer tokens. `error_msg` is truncated to 200 characters.
- **Failure isolation.** A telemetry failure never breaks a memory operation: log once, self-disable, continue.
- **`BLUEOCEAN_TELEMETRY=0` disables everything.** With it set, no database file is opened and `/dashboard`, `/api/stats`, `/api/audit`, `/api/prices` return 503.
- **The process that owns the directory is the only one that writes it.** The server writes the database and the pricing file. The CLI reads over HTTP and never falls back to the file silently; `--db` is an explicit opt-in.
- **No new runtime dependency.** stdlib only for the telemetry path. `urllib.request` for the CLI's HTTP calls, matching what `tests/` already use.
- **Test convention.** Tests under `tests/` are standalone scripts, not pytest-discovered. Each file ends with a `main()` calling every test function and `if __name__ == "__main__": main()`, and is run as `uv run python -m tests.<name>`. Assertions use plain `assert` with a message; progress is printed.
- **`memory_usage` is never instrumented.** Reading the meter is not using the memory.
- **Timestamps are UTC epoch seconds** (`int(time.time())`), matching the rest of the codebase. Day bucketing applies the caller's offset in SQL.
- **Style.** No em dashes in identifiers or AWS-facing strings; match the surrounding code's comment density, which explains *why* rather than *what*.

---

## File Structure

**New package `src/blueocean_mcp/telemetry/`** (a package, not one module: the codebase keeps flat modules for single responsibilities like `auth.py`, and uses a package where there are several, as in `embeddings/`)

| File | Responsibility |
| --- | --- |
| `telemetry/__init__.py` | Public surface: `is_enabled`, `get_writer`, `shutdown` |
| `telemetry/db.py` | Connection, PRAGMAs, schema, `user_version` migrations, retention purge |
| `telemetry/writer.py` | `Event` row shape, bounded queue, writer thread, `record`, `record_hits`, `delete_hits`, drop counting, self-disable |
| `telemetry/usage.py` | `ContextVar` accumulator for embedding tokens and time |
| `telemetry/pricing.py` | Built-in price table, resolution chain, pricing file read/write, OpenRouter fetch |
| `telemetry/queries.py` | Aggregation SQL: the `/api/stats` payload and the `memory_usage` summary |
| `telemetry/routes.py` | Starlette handlers for `/api/stats`, `/api/audit`, `/api/prices`, `/dashboard` |
| `telemetry/dashboard.html` | The single-file page, loaded from disk and served verbatim |

**Modified**

| File | Change |
| --- | --- |
| `src/blueocean_mcp/config.py` | New env knobs |
| `src/blueocean_mcp/tools.py` | `instrument()` applied over the registration list; new `memory_usage` tool |
| `src/blueocean_mcp/embeddings/{base,openai,bedrock,fastembed}.py` | Report token usage into the accumulator |
| `src/blueocean_mcp/__main__.py` | Insert the telemetry routes, start and stop the writer |
| `src/blueocean_mcp/admin.py` | `usage` subcommand; audit reporting from `prune` and `restore` |
| `docker-compose.yml`, `Dockerfile`, `.gitignore`, `README.md` | Bind mount, `/data`, ignore `data/`, docs |

`vector_store.py` is deliberately **not** modified. It stays free of telemetry; `entry_hits` bookkeeping lives in the instrumentation layer and in `admin.py`, so the storage layer keeps one responsibility.

**Tests**

| File | Covers |
| --- | --- |
| `tests/telemetry_db.py` | schema, migration, retention, concurrent writers, disabled mode |
| `tests/telemetry_instrument.py` | ctx injection, tool coverage, `clientInfo`, failure isolation, Bedrock accumulation |
| `tests/telemetry_pricing.py` | price resolution order, OpenRouter parsing, atomic file write |
| `tests/telemetry_privacy.py` | the sentinel round trip |
| `tests/telemetry_http.py` | route auth, disabled 503, day bucketing, audit origin |
| `tests/telemetry_cli.py` | no silent fallback, `--db`, `--refresh-prices` |

---

## Task 1: Database layer

**Files:**
- Create: `src/blueocean_mcp/telemetry/__init__.py`
- Create: `src/blueocean_mcp/telemetry/db.py`
- Modify: `src/blueocean_mcp/config.py`
- Test: `tests/telemetry_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `db.connect(path) -> sqlite3.Connection`, `db.db_path() -> str`, `db.purge_old(conn, retention_days, now=None) -> int`, `db.SCHEMA_VERSION: int`, and in `config`: `TELEMETRY_ENABLED: bool`, `DEFAULT_TELEMETRY_DB: str`, `DEFAULT_PRICING_FILE: str`, `TELEMETRY_RETENTION_DAYS: int`, `TELEMETRY_QUEUE_SIZE: int`, `DEFAULT_SERVER_URL: str`.

- [ ] **Step 1: Add the config knobs**

Append to `src/blueocean_mcp/config.py`:

```python
from pathlib import Path

# Telemetry. Off means off: with BLUEOCEAN_TELEMETRY=0 no database file is
# ever opened, and the HTTP surface answers 503 rather than 404 -- a 404
# makes a deliberate configuration look like a broken deployment.
TELEMETRY_ENABLED = os.getenv("BLUEOCEAN_TELEMETRY", "1") != "0"

# Under docker-compose this is overridden to /data/telemetry.db, which is a
# bind mount. The default below is for running outside a container.
DEFAULT_TELEMETRY_DB = os.getenv(
    "BLUEOCEAN_TELEMETRY_DB", str(Path.home() / ".blueocean" / "telemetry.db")
)
DEFAULT_PRICING_FILE = os.getenv(
    "BLUEOCEAN_PRICING_FILE", str(Path.home() / ".blueocean" / "pricing.json")
)
TELEMETRY_RETENTION_DAYS = int(os.getenv("BLUEOCEAN_TELEMETRY_RETENTION_DAYS", "90"))
TELEMETRY_QUEUE_SIZE = int(os.getenv("BLUEOCEAN_TELEMETRY_QUEUE", "10000"))

# Where blueocean-admin looks for the server when it needs telemetry it is
# not allowed to read off disk (see the spec's single-writer rule).
DEFAULT_SERVER_URL = os.getenv("BLUEOCEAN_SERVER_URL", "http://localhost:8765")
```

- [ ] **Step 2: Write the failing test**

Create `tests/telemetry_db.py`:

```python
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
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run python -m tests.telemetry_db`
Expected: FAIL with `ModuleNotFoundError: No module named 'blueocean_mcp.telemetry'`

- [ ] **Step 4: Write the implementation**

Create `src/blueocean_mcp/telemetry/__init__.py`:

```python
"""Local, on-by-default usage telemetry for the blueocean memory server."""

from .db import SCHEMA_VERSION, connect, db_path, purge_old

__all__ = ["SCHEMA_VERSION", "connect", "db_path", "purge_old"]
```

Create `src/blueocean_mcp/telemetry/db.py`:

```python
"""SQLite schema and connection handling for telemetry.

Migrations are additive only. A version mismatch never drops data: the 90
days of history is the entire point of the feature, and a `docker compose
pull` must not silently erase it.
"""

import sqlite3
import time
from pathlib import Path

from ..config import DEFAULT_TELEMETRY_DB

SCHEMA_VERSION = 1

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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m tests.telemetry_db`
Expected: PASS, ending with `TELEMETRY DB TEST PASSED`

- [ ] **Step 6: Commit**

```bash
git add src/blueocean_mcp/telemetry/__init__.py src/blueocean_mcp/telemetry/db.py \
        src/blueocean_mcp/config.py tests/telemetry_db.py
git commit -m "Add telemetry SQLite schema with additive migrations"
```

---

## Task 2: Bounded queue and writer thread

**Files:**
- Create: `src/blueocean_mcp/telemetry/writer.py`
- Modify: `src/blueocean_mcp/telemetry/__init__.py`
- Test: `tests/telemetry_db.py` (append)

**Interfaces:**
- Consumes: `db.connect`, `db.purge_old`, `config.TELEMETRY_ENABLED`, `config.TELEMETRY_QUEUE_SIZE`, `config.TELEMETRY_RETENTION_DAYS`.
- Produces: `writer.TelemetryWriter(path, queue_size)` with `.start()`, `.stop(timeout=5.0)`, `.record(row: dict)`, `.record_hits(project, summary_ids, full_ids)`, `.delete_hits(project, point_ids)`, `.flush(timeout=5.0)`, `.dropped: int`, `.disabled: bool`; module functions `get_writer() -> TelemetryWriter | None`, `is_enabled() -> bool`, `shutdown() -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telemetry_db.py` (and add the calls to `main()`):

```python
def test_writer_persists_rows() -> None:
    print("== writer thread drains the queue into SQLite ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        w = writer.TelemetryWriter(path)
        w.start()
        try:
            # A realistic timestamp, not ts=1: the writer purges rows outside
            # the retention window at startup, so an epoch-1970 row would be
            # correctly deleted microseconds after it was written.
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
            rows = dict(
                (r[0], (r[1], r[2]))
                for r in conn.execute("SELECT point_id, hits, full_hits FROM entry_hits")
            )
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
```

Add to the imports at the top of the file:

```python
import importlib
import os

from blueocean_mcp import config
from blueocean_mcp.telemetry import writer
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_db`
Expected: FAIL with `ImportError: cannot import name 'writer'`

- [ ] **Step 3: Write the implementation**

Create `src/blueocean_mcp/telemetry/writer.py`:

```python
"""Bounded queue plus a background thread that drains it into SQLite.

Two rules shape this file. First, recording must never block or raise on the
memory path: a full queue drops its oldest row and counts it, and any backend
failure disables telemetry rather than propagating. Second, one connection is
owned by one thread, which is why the writer thread opens its own and nothing
else touches it.
"""

import logging
import queue
import sqlite3
import threading
import time
from typing import Any

from ..config import (
    TELEMETRY_ENABLED,
    TELEMETRY_QUEUE_SIZE,
    TELEMETRY_RETENTION_DAYS,
)
from . import db

logger = logging.getLogger(__name__)

_PURGE_INTERVAL_SECONDS = 3600


class TelemetryWriter:
    def __init__(self, path: str | None = None, queue_size: int | None = None) -> None:
        self._path = path
        self._queue: queue.Queue = queue.Queue(
            maxsize=queue_size or TELEMETRY_QUEUE_SIZE
        )
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self.dropped = 0
        self.disabled = False

    # -- producer side (called from request threads) ----------------------

    def _put(self, item: tuple[str, Any]) -> None:
        if self.disabled:
            return
        while True:
            try:
                self._queue.put_nowait(item)
                self._idle.clear()
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:  # pragma: no cover - racing consumer
                    return

    def record(self, row: dict) -> None:
        self._put(("event", row))

    def record_hits(
        self, project: str, summary_ids: list[str], full_ids: list[str]
    ) -> None:
        if not summary_ids and not full_ids:
            return
        self._put(("hits", (project, list(summary_ids), list(full_ids))))

    def delete_hits(self, project: str, point_ids: list[str]) -> None:
        if not point_ids:
            return
        self._put(("delete_hits", (project, list(point_ids))))

    # -- consumer side ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="blueocean-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put(("stop", None))
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue has been drained. Tests only; the request
        path never waits on telemetry."""
        self._idle.wait(timeout=timeout)

    def _run(self) -> None:
        try:
            conn = db.connect(self._path)
        except Exception:
            logger.warning("telemetry disabled: could not open database", exc_info=True)
            self.disabled = True
            return
        # "Purge at process start" means exactly that: once, here. Seeding
        # last_purge from the monotonic clock also stops the first event from
        # triggering a purge, which is what happens if it starts at 0.0.
        last_purge = time.monotonic()
        try:
            db.purge_old(conn, TELEMETRY_RETENTION_DAYS)
        except Exception:  # pragma: no cover - purge is best effort
            logger.warning("telemetry retention purge failed", exc_info=True)
        try:
            while True:
                try:
                    kind, payload = self._queue.get(timeout=1.0)
                except queue.Empty:
                    self._idle.set()
                    if self._stopping.is_set():
                        return
                    kind, payload = None, None
                if kind == "stop":
                    return
                if kind is not None:
                    try:
                        self._apply(conn, kind, payload)
                    except Exception:
                        # One log line, then silence. A telemetry backend that
                        # keeps raising must not turn into a log flood on every
                        # memory operation.
                        logger.warning("telemetry disabled after write failure", exc_info=True)
                        self.disabled = True
                        self._idle.set()
                        return
                    if self._queue.empty():
                        self._idle.set()
                now = time.monotonic()
                if now - last_purge > _PURGE_INTERVAL_SECONDS:
                    last_purge = now
                    try:
                        db.purge_old(conn, TELEMETRY_RETENTION_DAYS)
                    except Exception:  # pragma: no cover - purge is best effort
                        logger.warning("telemetry retention purge failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    def _apply(self, conn: sqlite3.Connection, kind: str, payload: Any) -> None:
        if kind == "event":
            cols = list(payload.keys())
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO events ({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
        elif kind == "hits":
            project, summary_ids, full_ids = payload
            now = int(time.time())
            full = set(full_ids)
            rows = [
                (project, pid, 1 if pid in full else 0, now)
                for pid in dict.fromkeys(list(summary_ids) + list(full_ids))
            ]
            # One batched statement per search, not one per result: a search
            # can return up to BLUEOCEAN_MAX_TOP_K (200) entries.
            conn.executemany(
                "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
                "VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(project, point_id) DO UPDATE SET "
                " hits = hits + 1,"
                " full_hits = full_hits + excluded.full_hits,"
                " last_seen_at = excluded.last_seen_at",
                rows,
            )
        elif kind == "delete_hits":
            project, point_ids = payload
            conn.executemany(
                "DELETE FROM entry_hits WHERE project = ? AND point_id = ?",
                [(project, pid) for pid in point_ids],
            )
        conn.commit()


_writer: TelemetryWriter | None = None
_lock = threading.Lock()


def is_enabled() -> bool:
    return TELEMETRY_ENABLED


def get_writer() -> TelemetryWriter | None:
    """The process-wide writer, or None when telemetry is disabled.

    Returning None rather than a no-op object keeps the disabled path honest:
    callers see there is nothing to write to, and no file is ever opened.
    """
    global _writer
    if not TELEMETRY_ENABLED:
        return None
    with _lock:
        if _writer is None:
            _writer = TelemetryWriter()
            _writer.start()
        return _writer


def shutdown() -> None:
    global _writer
    with _lock:
        if _writer is not None:
            _writer.stop()
            _writer = None
```

Update `src/blueocean_mcp/telemetry/__init__.py`:

```python
"""Local, on-by-default usage telemetry for the blueocean memory server."""

from .db import SCHEMA_VERSION, connect, db_path, purge_old
from .writer import TelemetryWriter, get_writer, is_enabled, shutdown

__all__ = [
    "SCHEMA_VERSION",
    "TelemetryWriter",
    "connect",
    "db_path",
    "get_writer",
    "is_enabled",
    "purge_old",
    "shutdown",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.telemetry_db`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/blueocean_mcp/telemetry/writer.py src/blueocean_mcp/telemetry/__init__.py \
        tests/telemetry_db.py
git commit -m "Add telemetry writer with bounded queue and self-disabling failure"
```

---

## Task 3: The instrument wrapper

This is the task with the hazard. The test comes first and is not optional.

**Files:**
- Create: `src/blueocean_mcp/telemetry/instrument.py`
- Modify: `src/blueocean_mcp/tools.py`
- Test: `tests/telemetry_instrument.py`

**Interfaces:**
- Consumes: `writer.get_writer`.
- Produces: `instrument.instrument(fn, tool_name, writer_factory=get_writer) -> Callable` and `instrument.INSTRUMENT_DENYLIST: frozenset[str]`. The wrapped function keeps the original's name, docstring, `__signature__` and `__annotations__`, with one extra keyword-only parameter `ctx: Context`.

- [ ] **Step 1: Write the failing test, including the schema assertion**

Create `tests/telemetry_instrument.py`:

```python
"""Tests for the tool instrumentation wrapper.

The critical one is test_ctx_is_injected_and_hidden. The mcp library finds a
Context parameter through typing.get_type_hints (i.e. __annotations__) but
builds the agent-visible JSON schema from the signature. A wrapper that sets
only one of the two fails silently: either ctx is never injected, or ctx
leaks into the schema agents see.

Run with:
    uv run python -m tests.telemetry_instrument
"""

import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.context_injection import find_context_parameter

from blueocean_mcp.telemetry import writer
from blueocean_mcp.telemetry.instrument import INSTRUMENT_DENYLIST, instrument


def _writer_in(d: str) -> writer.TelemetryWriter:
    w = writer.TelemetryWriter(str(Path(d) / "t.db"))
    w.start()
    return w


def test_ctx_is_injected_and_hidden() -> None:
    print("== ctx is injected but absent from the published schema ==")

    def memory_demo(project: str, count: int = 1) -> dict:
        """Demo tool."""
        return {"project": project, "count": count}

    wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: None)

    assert find_context_parameter(wrapped) == "ctx", (
        "the library reads __annotations__ via get_type_hints; the wrapper must set it"
    )

    mcp = MCPServer(name="t")
    mcp.add_tool(wrapped, name="memory_demo", description="demo")
    tool = mcp._tool_manager.get_tool("memory_demo")
    props = tool.parameters.get("properties", {})
    assert "ctx" in find_context_parameter(wrapped)
    assert "ctx" not in props, f"ctx leaked into the agent-visible schema: {props}"
    assert set(props) == {"project", "count"}, props
    print("  OK")


def test_call_is_recorded_with_timing() -> None:
    print("== a successful call is recorded ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_demo(project: str) -> dict:
                """Demo."""
                return {"ok": True}

            wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: w)
            result = wrapped(project="p", ctx=None)
            assert result == {"ok": True}
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            row = conn.execute(
                "SELECT tool, project, ok, origin, total_ms FROM events"
            ).fetchone()
            assert row[0] == "memory_demo", row
            assert row[1] == "p", row
            assert row[2] == 1, row
            assert row[3] == "observed", row
            assert row[4] is not None and row[4] >= 0, row
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_failure_is_recorded_and_reraised() -> None:
    print("== a failing call records the error and still raises ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_demo(project: str) -> dict:
                """Demo."""
                raise ValueError("x" * 500)

            wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: w)
            raised = False
            try:
                wrapped(project="p", ctx=None)
            except ValueError:
                raised = True
            assert raised, "the wrapper must never swallow the tool's exception"
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            ok, cls, msg = conn.execute(
                "SELECT ok, error_class, error_msg FROM events"
            ).fetchone()
            assert ok == 0, ok
            assert cls == "ValueError", cls
            assert len(msg) == 200, f"error_msg must be truncated to 200 chars, got {len(msg)}"
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_telemetry_failure_does_not_break_the_tool() -> None:
    print("== a broken writer does not break the memory operation ==")

    class Broken:
        def record(self, row):
            raise RuntimeError("backend on fire")

        def record_hits(self, *a):
            raise RuntimeError("backend on fire")

    def memory_demo(project: str) -> dict:
        """Demo."""
        return {"ok": True}

    wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: Broken())
    assert wrapped(project="p", ctx=None) == {"ok": True}
    print("  OK")


def test_client_info_is_recorded() -> None:
    """The whole point of the audit trail is knowing which agent called.
    Built from the library's real types on purpose: if the field is renamed
    again, this test fails instead of identity silently going NULL."""
    print("== clientInfo lands in agent_name and agent_version ==")
    from typing import ClassVar

    from mcp.types import ClientCapabilities, Implementation, InitializeRequestParams

    class FakeSession:
        client_params = InitializeRequestParams(
            protocolVersion="2025-11-25",
            capabilities=ClientCapabilities(),
            clientInfo=Implementation(name="probe-agent", version="9.9.9"),
        )

    class FakeCtx:
        session = FakeSession()
        headers: ClassVar[dict[str, str]] = {"mcp-session-id": "sess-123"}

    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_demo(project: str) -> dict:
                """Demo."""
                return {"ok": True}

            wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: w)
            wrapped(project="p", ctx=FakeCtx())
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            row = conn.execute(
                "SELECT agent_name, agent_version, session_id FROM events"
            ).fetchone()
            assert row == ("probe-agent", "9.9.9", "sess-123"), row
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_missing_client_info_is_not_fatal() -> None:
    print("== a client that sends no identity still records the call ==")

    class FakeCtx:
        session = type("S", (), {"client_params": None})()
        headers = None

    def memory_demo(project: str) -> dict:
        """Demo."""
        return {"ok": True}

    wrapped = instrument(memory_demo, "memory_demo", writer_factory=lambda: None)
    assert wrapped(project="p", ctx=FakeCtx()) == {"ok": True}
    print("  OK")


def test_memory_usage_is_on_the_denylist() -> None:
    print("== memory_usage is deliberately not instrumented ==")
    assert "memory_usage" in INSTRUMENT_DENYLIST
    print("  OK")


def test_all_tools_instrumented_except_the_denylist() -> None:
    """Guards the registration loop: a tool added later must not silently
    vanish from the stats."""
    print("== every registered tool is instrumented except the denylist ==")
    from blueocean_mcp.server import build_server

    mcp = build_server()
    names = set(mcp._tool_manager._tools)
    instrumented = {
        n for n in names if find_context_parameter(mcp._tool_manager.get_tool(n).fn) == "ctx"
    }
    expected = names - INSTRUMENT_DENYLIST
    assert instrumented == expected, (
        f"instrumented={sorted(instrumented)} expected={sorted(expected)}"
    )
    assert len(expected) == 8, f"expected 8 instrumented tools, got {len(expected)}"
    print(f"  OK: {len(expected)} instrumented, {len(names) - len(expected)} excluded")


def main() -> None:
    test_ctx_is_injected_and_hidden()
    test_call_is_recorded_with_timing()
    test_failure_is_recorded_and_reraised()
    test_telemetry_failure_does_not_break_the_tool()
    test_client_info_is_recorded()
    test_missing_client_info_is_not_fatal()
    test_memory_usage_is_on_the_denylist()
    test_all_tools_instrumented_except_the_denylist()
    print("\nTELEMETRY INSTRUMENT TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_instrument`
Expected: FAIL with `ModuleNotFoundError: No module named 'blueocean_mcp.telemetry.instrument'`

- [ ] **Step 3: Write the implementation**

Create `src/blueocean_mcp/telemetry/instrument.py`:

```python
"""One wrapper, applied to every registered tool in one loop.

Two things here are load-bearing and easy to "simplify" into a silent bug.

1. The wrapper sets BOTH __signature__ and __annotations__. The mcp library
   locates the Context parameter with typing.get_type_hints (annotations) but
   builds the agent-visible JSON schema from the signature. Set only one and
   either ctx is never injected, or ctx appears in the schema agents see.
   Neither raises.
2. Nothing in here may raise. A telemetry problem must not become a memory
   failure, so every telemetry call sits inside a bare except.
"""

import functools
import inspect
import logging
import pathlib
import time
from typing import Any, Callable

from mcp.server.mcpserver import Context

from .writer import get_writer

logger = logging.getLogger(__name__)

# Reading the meter is not using the memory. If memory_usage were recorded, an
# open dashboard would inflate the numbers it displays and memory_usage would
# become the top tool within days.
INSTRUMENT_DENYLIST = frozenset({"memory_usage"})

_ERROR_MSG_MAX = 200


_REDACTED = "<redacted: contained caller text>"
_REDACTED_FOREIGN = "<redacted: third-party exception>"

# Our own package directory. An exception whose deepest traceback frame is
# outside it came from a library we call, and those libraries quote the
# payload they choked on - which is the user's memory.
_PACKAGE_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

# Arguments already stored in their own columns, and not content. Excluding
# them keeps an ordinary message like "collection blueocean_myproject not
# found" readable instead of redacting it for naming the project.
_NON_SENSITIVE_KEYS = frozenset({"project", "area", "module"})

# Below this length a caller argument matches ordinary words in an
# infrastructure message and would redact everything.
_MIN_SENSITIVE_LEN = 8


def _caller_strings(kwargs: dict) -> list[str]:
    out: list[str] = []
    for key, value in kwargs.items():
        if key in _NON_SENSITIVE_KEYS:
            continue
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, dict):
            out.extend(str(v) for v in value.values())
    return out


def _raised_by_us(exc: BaseException) -> bool:
    """True when the deepest traceback frame is inside this package."""
    tb, filename = exc.__traceback__, None
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    return bool(filename) and filename.startswith(_PACKAGE_ROOT)


def _safe_error_message(exc: Exception, kwargs: dict) -> str | None:
    """Never let an exception message carry memory content into telemetry.

    Auditing every dependency's message formatting forever is not a plan, so
    a message survives only if we raised it ourselves and it does not quote
    the caller. The cost is accepted: a Qdrant ConnectionError arrives as a
    class name without its text.
    """
    message = str(exc)
    if not message:
        return None
    if not _raised_by_us(exc):
        return _REDACTED_FOREIGN
    for value in _caller_strings(kwargs):
        if len(value) >= _MIN_SENSITIVE_LEN and value in message:
            return _REDACTED
    return message[:_ERROR_MSG_MAX]


def _agent_identity(ctx: Any) -> dict:
    """Best-effort identity from the MCP handshake. Every field is
    client-supplied: fine as a grouping key, never an identity assertion."""
    out: dict = {}
    if ctx is None:
        return out
    try:
        params = ctx.session.client_params
        # The field is `client_info` on this version of the mcp library.
        # Reading `clientInfo` instead raises AttributeError, which the except
        # below swallows, and identity silently stays NULL forever - which is
        # exactly what happened the first time this was written. The fallback
        # keeps both spellings working if the library renames it again.
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        if info is not None:
            out["agent_name"] = getattr(info, "name", None)
            out["agent_version"] = getattr(info, "version", None)
    except Exception:  # noqa: BLE001 - identity is optional, never fatal
        logger.debug("agent identity unavailable", exc_info=True)
    try:
        headers = ctx.headers or {}
        out["session_id"] = headers.get("mcp-session-id")
    except Exception:  # noqa: BLE001
        logger.debug("session id unavailable", exc_info=True)
    return out


def instrument(
    fn: Callable, tool_name: str, writer_factory: Callable[[], Any] = get_writer
) -> Callable:
    original_sig = inspect.signature(fn)
    ctx_param = inspect.Parameter(
        "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context
    )

    @functools.wraps(fn)
    def wrapper(*args, ctx: Context | None = None, **kwargs):
        started = time.perf_counter()
        row: dict = {
            "ts": int(time.time()),
            "kind": "tool",
            "tool": tool_name,
            "origin": "observed",
        }
        for field in ("project", "area", "module"):
            if field in kwargs:
                row[field] = kwargs[field]
        row.update(_agent_identity(ctx))

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            row["ok"] = 0
            row["error_class"] = type(exc).__name__
            row["error_msg"] = _safe_error_message(exc, kwargs)
            row["total_ms"] = (time.perf_counter() - started) * 1000
            _safe_record(writer_factory, row)
            raise
        row["ok"] = 1
        row["total_ms"] = (time.perf_counter() - started) * 1000
        _safe_record(writer_factory, row)
        return result

    # Both, deliberately. See the module docstring.
    wrapper.__signature__ = original_sig.replace(
        parameters=[*original_sig.parameters.values(), ctx_param]
    )
    wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "ctx": Context}
    return wrapper


def _safe_record(writer_factory: Callable[[], Any], row: dict) -> None:
    try:
        w = writer_factory()
        if w is not None:
            w.record(row)
    except Exception:  # noqa: BLE001 - telemetry never breaks a memory op
        logger.debug("telemetry record failed", exc_info=True)
```

Modify `src/blueocean_mcp/tools.py`. Replace the eight `mcp.add_tool(...)` calls at the bottom with a registration list and one loop:

```python
    from .telemetry.instrument import INSTRUMENT_DENYLIST, instrument

    registrations = [
        (memory_store, "memory_store",
         "Persist a memory entry for a project (content + condensed summary + importance + area/module)."),
        (memory_search, "memory_search",
         "Semantic search a project's memory with token-budgeted return (summary + full layers)."),
        (memory_get, "memory_get",
         "Fetch the full content of a single memory entry by ID."),
        (memory_delete, "memory_delete",
         "Delete a single memory entry by ID."),
        (memory_list_projects, "memory_list_projects",
         "List all projects that have a memory collection."),
        (memory_manifest, "memory_manifest",
         "Show the areas/modules present in a project's memory."),
        (memory_summarize_session, "memory_summarize_session",
         "Store a condensed summary of a work session for later retrieval."),
        (memory_stats, "memory_stats",
         "Admin: collection stats (count, importance/area distribution, size)."),
    ]

    # Instrumenting here, in one loop over the registration list, rather than
    # decorating each function: a tool added later cannot silently vanish from
    # the stats by someone forgetting a decorator.
    for fn, name, description in registrations:
        tool = fn if name in INSTRUMENT_DENYLIST else instrument(fn, name)
        mcp.add_tool(tool, name=name, description=description)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.telemetry_instrument`
Expected: PASS, with the last line reporting 8 instrumented

- [ ] **Step 5: Run the existing suite to prove nothing regressed**

Run: `uv run python -m tests.mcp_full_coverage`
Expected: PASS. This is the check that ctx did not leak into any tool schema in a way that breaks real calls.

- [ ] **Step 6: Commit**

```bash
git add src/blueocean_mcp/telemetry/instrument.py src/blueocean_mcp/tools.py \
        tests/telemetry_instrument.py
git commit -m "Instrument all MCP tools through one wrapper"
```

---

## Task 4: Embedding usage accounting

**Files:**
- Create: `src/blueocean_mcp/telemetry/usage.py`
- Modify: `src/blueocean_mcp/embeddings/openai.py`, `src/blueocean_mcp/embeddings/bedrock.py`, `src/blueocean_mcp/embeddings/fastembed.py`
- Modify: `src/blueocean_mcp/telemetry/instrument.py`
- Test: `tests/telemetry_instrument.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `usage.reset()`, `usage.add(tokens: int | None, ms: float, exact: bool)`, `usage.take() -> dict` returning `{"embed_tokens": int | None, "embed_ms": float, "tokens_exact": int}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/telemetry_instrument.py` and register in `main()`:

```python
def test_usage_accumulates_and_is_isolated_per_call() -> None:
    """Sync tools run through anyio.to_thread.run_sync on a REUSED worker
    thread pool, so a threading.local would leak one call's token count into
    the next call on the same worker. A ContextVar does not."""
    print("== embedding usage accumulates per call, not across calls ==")
    from blueocean_mcp.telemetry import usage

    usage.reset()
    usage.add(10, 1.5, exact=True)
    usage.add(15, 2.5, exact=True)
    taken = usage.take()
    assert taken["embed_tokens"] == 25, taken
    assert abs(taken["embed_ms"] - 4.0) < 0.001, taken
    assert taken["tokens_exact"] == 1, taken

    usage.reset()
    assert usage.take()["embed_tokens"] is None, "reset must clear the accumulator"
    print("  OK")


def test_estimated_usage_is_flagged_inexact() -> None:
    print("== estimated token counts are flagged inexact ==")
    from blueocean_mcp.telemetry import usage

    usage.reset()
    usage.add(40, 0.5, exact=False)
    taken = usage.take()
    assert taken["embed_tokens"] == 40, taken
    assert taken["tokens_exact"] == 0, taken
    print("  OK")


def test_bedrock_usage_accumulates_across_texts() -> None:
    """BedrockEmbedder loops invoke_model once per text. Usage must sum, not
    overwrite with the last response."""
    print("== bedrock sums usage across its per-text calls ==")
    import json as _json

    from blueocean_mcp.embeddings.bedrock import BedrockEmbedder
    from blueocean_mcp.telemetry import usage

    class FakeBody:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return _json.dumps(self._payload).encode()

    class FakeClient:
        def invoke_model(self, **_kwargs):
            return {"body": FakeBody({"embedding": [0.1, 0.2], "inputTextTokenCount": 7})}

    embedder = BedrockEmbedder.__new__(BedrockEmbedder)
    embedder._model_id = "amazon.titan-embed-text-v2:0"
    embedder._client = FakeClient()

    usage.reset()
    vectors = embedder.embed(["a", "b", "c"])
    assert len(vectors) == 3, vectors
    taken = usage.take()
    assert taken["embed_tokens"] == 21, f"expected 3 x 7 = 21, got {taken}"
    assert taken["tokens_exact"] == 1, taken
    print("  OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_instrument`
Expected: FAIL with `ModuleNotFoundError: No module named 'blueocean_mcp.telemetry.usage'`

- [ ] **Step 3: Write `usage.py`**

Create `src/blueocean_mcp/telemetry/usage.py`:

```python
"""Per-call embedding accounting.

A ContextVar, not a threading.local. Sync tool functions are dispatched via
anyio.to_thread.run_sync (mcp/server/mcpserver/utilities/func_metadata.py),
which uses a reused worker thread pool: a thread-local that is not cleared at
call start would report the previous call's tokens. anyio copies the context
into the worker thread, so a ContextVar is correct on both the threaded and
the direct-await path.

The alternative was changing the Embedder ABC to return usage alongside the
vectors. That is a breaking change to a public interface for the sake of a
secondary feature, so usage rides this side channel instead.
"""

from contextvars import ContextVar

_usage: ContextVar[dict | None] = ContextVar("blueocean_embed_usage", default=None)


def reset() -> None:
    _usage.set({"embed_tokens": None, "embed_ms": 0.0, "tokens_exact": 1})


def add(tokens: int | None, ms: float, exact: bool) -> None:
    """Accumulate one embedding call. Providers that loop (Bedrock invokes the
    model once per text) call this once per invocation, so values sum."""
    current = _usage.get()
    if current is None:
        return
    if tokens is not None:
        current["embed_tokens"] = (current["embed_tokens"] or 0) + int(tokens)
    current["embed_ms"] += float(ms)
    if not exact:
        current["tokens_exact"] = 0


def take() -> dict:
    current = _usage.get()
    if current is None:
        return {"embed_tokens": None, "embed_ms": 0.0, "tokens_exact": 1}
    return dict(current)
```

- [ ] **Step 4: Report usage from each provider**

In `src/blueocean_mcp/embeddings/openai.py`, replace `embed`:

```python
    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from ..telemetry import usage

        started = time.perf_counter()
        resp = self._client.embeddings.create(model=self._model, input=texts)
        elapsed_ms = (time.perf_counter() - started) * 1000
        # The API already tells us the exact token count; the old code threw
        # it away with the rest of the response.
        tokens = getattr(getattr(resp, "usage", None), "prompt_tokens", None)
        usage.add(tokens, elapsed_ms, exact=tokens is not None)
        data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in data]
```

In `src/blueocean_mcp/embeddings/bedrock.py`, replace `embed`:

```python
    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from ..telemetry import usage

        vectors: list[list[float]] = []
        for text in texts:
            started = time.perf_counter()
            resp: dict[str, Any] = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps({"inputText": text}),
                contentType="application/json",
                accept="application/json",
            )
            body = resp["body"].read().decode("utf-8")
            parsed = json.loads(body)
            # One invoke_model per text, so this accumulates across the loop.
            usage.add(
                parsed.get("inputTextTokenCount"),
                (time.perf_counter() - started) * 1000,
                exact=parsed.get("inputTextTokenCount") is not None,
            )
            vectors.append(parsed["embedding"])
        return vectors
```

In `src/blueocean_mcp/embeddings/fastembed.py`, wrap the existing `embed` body so it reports estimated tokens:

```python
    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from ..telemetry import usage
        from ..token_budget import estimate_tokens

        started = time.perf_counter()
        vectors = [list(v) for v in self._model.embed(texts)]
        # Local and free, so there is no real count to report. Estimating and
        # flagging it inexact keeps measured and guessed numbers separable
        # instead of summing them and implying equal precision.
        usage.add(
            sum(estimate_tokens(t) for t in texts),
            (time.perf_counter() - started) * 1000,
            exact=False,
        )
        return vectors
```

Note: keep whatever the existing `fastembed.py` body does to produce `vectors`; only the timing, the `usage.add` call and the return are being added around it.

- [ ] **Step 5: Wire the accumulator into the wrapper**

In `src/blueocean_mcp/telemetry/instrument.py`, add the import and reset/collect around the call:

```python
from . import usage
```

Inside `wrapper`, immediately after `started = time.perf_counter()`:

```python
        usage.reset()
```

and in both the success and failure paths, before `_safe_record(...)`:

```python
        row.update(usage.take())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run python -m tests.telemetry_instrument`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/blueocean_mcp/telemetry/usage.py src/blueocean_mcp/telemetry/instrument.py \
        src/blueocean_mcp/embeddings/ tests/telemetry_instrument.py
git commit -m "Capture embedding token usage through a ContextVar accumulator"
```

---

## Task 5: Price resolution

**Files:**
- Create: `src/blueocean_mcp/telemetry/pricing.py`
- Modify: `src/blueocean_mcp/telemetry/instrument.py`
- Test: `tests/telemetry_pricing.py`

**Interfaces:**
- Consumes: `config.DEFAULT_PRICING_FILE`.
- Produces: `pricing.BUILTIN_PRICES: dict[tuple[str, str], float]`, `pricing.resolve(provider, model, pricing_file=None) -> tuple[float | None, str | None]`, `pricing.cost_usd(tokens, price_per_1m) -> float | None`, `pricing.load_file(path) -> dict[str, float]`, `pricing.write_file(path, prices) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/telemetry_pricing.py`:

```python
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


def main() -> None:
    test_builtin_prices_match_the_spec()
    test_resolution_order()
    test_unknown_model_is_null_not_zero()
    test_fastembed_is_free_not_priced_from_the_feed()
    test_cost_math()
    test_write_file_is_atomic_and_readable()
    print("\nTELEMETRY PRICING TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_pricing`
Expected: FAIL with `ImportError: cannot import name 'pricing'`

- [ ] **Step 3: Write the implementation**

Create `src/blueocean_mcp/telemetry/pricing.py`:

```python
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
```

- [ ] **Step 4: Price each event in the wrapper**

In `src/blueocean_mcp/telemetry/instrument.py`, add near the top:

```python
from ..config import DEFAULT_EMBEDDING_PROVIDER
from . import pricing
from ..health import resolve_embedding_model
```

and add a helper, called from both paths right after `row.update(usage.take())`:

```python
def _price(row: dict) -> None:
    """Snapshot the price and its source onto the row, so the number stays
    truthful after the table is updated."""
    tokens = row.get("embed_tokens")
    if not tokens:
        return
    provider = DEFAULT_EMBEDDING_PROVIDER
    model = resolve_embedding_model(provider)
    price, source = pricing.resolve(provider, model)
    row["unit_price_per_1m"] = price
    row["price_source"] = source
    row["est_cost_usd"] = pricing.cost_usd(tokens, price)
```

- [ ] **Step 5: Run the tests**

Run: `uv run python -m tests.telemetry_pricing && uv run python -m tests.telemetry_instrument`
Expected: both PASS

- [ ] **Step 6: Commit**

```bash
git add src/blueocean_mcp/telemetry/pricing.py src/blueocean_mcp/telemetry/instrument.py \
        tests/telemetry_pricing.py
git commit -m "Resolve and snapshot embedding prices per event"
```

---

## Task 6: Search quality and entry_hits

**Files:**
- Modify: `src/blueocean_mcp/telemetry/instrument.py`
- Test: `tests/telemetry_instrument.py` (append)

**Interfaces:**
- Consumes: `writer.record_hits`, `writer.delete_hits`, `TokenAllocation.to_dict()` output (`{"summary", "full", "total_tokens", ...}` where each entry has `id` and `score`).
- Produces: `instrument._EXTRACTORS: dict[str, Callable[[dict, Any], tuple[dict, tuple | None]]]` returning extra event columns plus an optional hits instruction.

- [ ] **Step 1: Write the failing test**

Append to `tests/telemetry_instrument.py` and register in `main()`:

```python
def test_search_quality_and_hits_are_recorded() -> None:
    print("== a search records quality columns and entry_hits ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_search(project: str, query: str) -> dict:
                """Demo."""
                return {
                    "summary": [{"id": "a", "score": 0.81}, {"id": "b", "score": 0.77}],
                    "full": [{"id": "a", "score": 0.81}],
                    "budget": 2000,
                    "total_tokens": 121,
                    "truncated": 0,
                }

            wrapped = instrument(memory_search, "memory_search", writer_factory=lambda: w)
            wrapped(project="p", query="anything", ctx=None)
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            count, top, tokens = conn.execute(
                "SELECT result_count, top_score, tokens_returned FROM events"
            ).fetchone()
            assert count == 2, count
            assert abs(top - 0.81) < 1e-9, top
            assert tokens == 121, tokens
            hits = dict(
                (r[0], (r[1], r[2]))
                for r in conn.execute("SELECT point_id, hits, full_hits FROM entry_hits")
            )
            assert hits["a"] == (1, 1), hits
            assert hits["b"] == (1, 0), hits
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_zero_result_search_is_recorded() -> None:
    print("== a search that finds nothing is still recorded ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_search(project: str, query: str) -> dict:
                """Demo."""
                return {"summary": [], "full": [], "total_tokens": 0}

            wrapped = instrument(memory_search, "memory_search", writer_factory=lambda: w)
            wrapped(project="p", query="nothing", ctx=None)
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            count, top = conn.execute("SELECT result_count, top_score FROM events").fetchone()
            assert count == 0, count
            assert top is None, "no results means no top score, not a score of 0"
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_memory_get_counts_as_a_full_hit() -> None:
    print("== memory_get counts as a full hit ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_get(project: str, point_id: str) -> dict:
                """Demo."""
                return {"found": True, "content": "x"}

            wrapped = instrument(memory_get, "memory_get", writer_factory=lambda: w)
            wrapped(project="p", point_id="a", ctx=None)
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            row = conn.execute(
                "SELECT hits, full_hits FROM entry_hits WHERE point_id = 'a'"
            ).fetchone()
            assert row == (1, 1), row
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_summarize_session_records_its_label() -> None:
    print("== memory_summarize_session records its agent-declared label ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_summarize_session(project: str, area: str, session_id: str,
                                         observations: list, conclusion: str) -> str:
                """Demo."""
                return "point-1"

            instrument(memory_summarize_session, "memory_summarize_session",
                       writer_factory=lambda: w)(
                project="p", area="a", session_id="codex-2026-09-02",
                observations=["x"], conclusion="done", ctx=None,
            )
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            label = conn.execute("SELECT agent_session_label FROM events").fetchone()[0]
            assert label == "codex-2026-09-02", label
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_memory_delete_removes_entry_hits() -> None:
    print("== memory_delete cleans up its entry_hits row ==")
    with tempfile.TemporaryDirectory() as d:
        w = _writer_in(d)
        try:
            def memory_get(project: str, point_id: str) -> dict:
                """Demo."""
                return {"found": True}

            def memory_delete(project: str, point_id: str) -> bool:
                """Demo."""
                return True

            instrument(memory_get, "memory_get", writer_factory=lambda: w)(
                project="p", point_id="a", ctx=None
            )
            instrument(memory_delete, "memory_delete", writer_factory=lambda: w)(
                project="p", point_id="a", ctx=None
            )
            w.flush()

            from blueocean_mcp.telemetry import db
            conn = db.connect(str(Path(d) / "t.db"))
            left = conn.execute("SELECT COUNT(*) FROM entry_hits").fetchone()[0]
            assert left == 0, left
            conn.close()
        finally:
            w.stop()
    print("  OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_instrument`
Expected: FAIL — `result_count` is NULL because no extractor exists yet

- [ ] **Step 3: Add the extractors**

In `src/blueocean_mcp/telemetry/instrument.py`, add above `instrument()`:

```python
def _extract_search(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    """Pull quality columns and hit ids out of a token-budgeted search result.

    Only ids and scores are read. Nothing here touches the query, the summary
    text or the content: see the privacy rule in the spec.
    """
    if not isinstance(result, dict):
        return {}, None
    summary = result.get("summary") or []
    full = result.get("full") or []
    columns = {
        "result_count": len(summary),
        # No results means no top score. Recording 0.0 would be a real score
        # that never happened, and would drag every average down.
        "top_score": summary[0].get("score") if summary else None,
        "tokens_returned": result.get("total_tokens"),
    }
    project = kwargs.get("project")
    if project is None:
        return columns, None
    summary_ids = [e.get("id") for e in summary if e.get("id")]
    full_ids = [e.get("id") for e in full if e.get("id")]
    return columns, ("hits", project, summary_ids, full_ids)


def _extract_get(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    """memory_get counts as a full hit: the intent is identical to expanding
    an entry into the full layer."""
    found = isinstance(result, dict) and result.get("found") is True
    project, point_id = kwargs.get("project"), kwargs.get("point_id")
    if not found or not project or not point_id:
        return {}, None
    return {"result_count": 1}, ("hits", project, [point_id], [point_id])


def _extract_delete(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    project, point_id = kwargs.get("project"), kwargs.get("point_id")
    columns = {"deleted_count": 1 if result else 0}
    if not result or not project or not point_id:
        return columns, None
    return columns, ("delete_hits", project, [point_id])


def _extract_summarize(kwargs: dict, _result: Any) -> tuple[dict, tuple | None]:
    """The agent-declared session label is the only link from an event back to
    a real entry in Qdrant. It is an opaque identifier the agent chose, not
    content, so recording it does not touch the privacy rule."""
    label = kwargs.get("session_id")
    return ({"agent_session_label": label} if label else {}), None


_EXTRACTORS = {
    "memory_search": _extract_search,
    "memory_get": _extract_get,
    "memory_delete": _extract_delete,
    "memory_summarize_session": _extract_summarize,
}
```

In `wrapper`, on the success path only (a failed call has no result to read), after `row["ok"] = 1`:

```python
        extractor = _EXTRACTORS.get(tool_name)
        if extractor is not None:
            try:
                columns, hit_instruction = extractor(kwargs, result)
                row.update(columns)
            except Exception:  # noqa: BLE001 - never break a tool over telemetry
                logger.debug("telemetry extractor failed", exc_info=True)
                hit_instruction = None
        else:
            hit_instruction = None
```

and after `_safe_record(...)`:

```python
        if hit_instruction is not None:
            _safe_hits(writer_factory, hit_instruction)
```

Add the helper next to `_safe_record`:

```python
def _safe_hits(writer_factory: Callable[[], Any], instruction: tuple) -> None:
    try:
        w = writer_factory()
        if w is None:
            return
        if instruction[0] == "hits":
            w.record_hits(instruction[1], instruction[2], instruction[3])
        else:
            w.delete_hits(instruction[1], instruction[2])
    except Exception:  # noqa: BLE001
        logger.debug("telemetry hits update failed", exc_info=True)
```

- [ ] **Step 4: Run the tests**

Run: `uv run python -m tests.telemetry_instrument`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/blueocean_mcp/telemetry/instrument.py tests/telemetry_instrument.py
git commit -m "Record search quality and entry hit counters"
```

---

## Task 7: The privacy sentinel

This task adds no feature. It is the gate that proves the hard rule holds across everything built so far.

**Files:**
- Test: `tests/telemetry_privacy.py`

**Interfaces:**
- Consumes: everything from tasks 1 to 6.
- Produces: nothing.

- [ ] **Step 1: Write the test**

Create `tests/telemetry_privacy.py`:

```python
"""The privacy gate: telemetry must never store query text, memory content,
summaries, entry metadata, or tokens.

The method is a sentinel round trip. Unique strings go into every input, then
every column of every row of every table is dumped and asserted not to contain
them. It is deliberately blunt: a new column added later is covered
automatically, because the test reads the schema rather than a fixed list.

Run with:
    uv run python -m tests.telemetry_privacy
"""

import tempfile
import uuid
from pathlib import Path

from blueocean_mcp.telemetry import db, writer
from blueocean_mcp.telemetry.instrument import instrument

SENTINELS = {
    "query": f"SENTINELQUERY{uuid.uuid4().hex}",
    "content": f"SENTINELCONTENT{uuid.uuid4().hex}",
    "summary": f"SENTINELSUMMARY{uuid.uuid4().hex}",
    "metadata": f"SENTINELMETA{uuid.uuid4().hex}",
    "token": f"SENTINELTOKEN{uuid.uuid4().hex}",
}


def _dump_all_values(conn) -> list[str]:
    values: list[str] = []
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            values.extend(str(v) for v in row)
    return values


def test_no_sentinel_reaches_the_database() -> None:
    print("== no memory content or query text reaches telemetry ==")
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "t.db")
        w = writer.TelemetryWriter(path)
        w.start()
        try:
            def memory_store(project: str, area: str, module: str, content: str,
                             summary: str, metadata: dict | None = None) -> str:
                """Demo."""
                return "point-1"

            def memory_search(project: str, query: str) -> dict:
                """Demo."""
                return {
                    "summary": [{"id": "a", "score": 0.9, "summary": SENTINELS["summary"],
                                 "metadata": {"k": SENTINELS["metadata"]}}],
                    "full": [{"id": "a", "content": SENTINELS["content"]}],
                    "total_tokens": 42,
                }

            def memory_delete(project: str, point_id: str) -> bool:
                """Demo."""
                raise ValueError(f"failed on {SENTINELS['content']}")

            instrument(memory_store, "memory_store", writer_factory=lambda: w)(
                project="p", area="a", module="m",
                content=SENTINELS["content"], summary=SENTINELS["summary"],
                metadata={"secret": SENTINELS["metadata"]}, ctx=None,
            )
            instrument(memory_search, "memory_search", writer_factory=lambda: w)(
                project="p", query=SENTINELS["query"], ctx=None
            )
            try:
                instrument(memory_delete, "memory_delete", writer_factory=lambda: w)(
                    project="p", point_id="a", ctx=None
                )
            except ValueError:
                pass  # expected; the point is what got recorded
            w.flush()

            conn = db.connect(path)
            values = _dump_all_values(conn)
            conn.close()
            haystack = "\n".join(values)
            for name, sentinel in SENTINELS.items():
                assert sentinel not in haystack, (
                    f"{name} sentinel leaked into telemetry. "
                    "Telemetry must never store content, queries, summaries, "
                    "metadata or tokens."
                )
            assert any("memory_search" in v for v in values), (
                "sanity check: the events should still have been written"
            )
        finally:
            w.stop()
    print("  OK: 5 sentinels absent, events present")


def main() -> None:
    test_no_sentinel_reaches_the_database()
    print("\nTELEMETRY PRIVACY TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `uv run python -m tests.telemetry_privacy`
Expected: PASS.

If the error-message sentinel fails, that is a real finding, not a test to loosen: an exception message is carrying memory content. Fix the source of the message or stop recording it; do not widen the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/telemetry_privacy.py
git commit -m "Add privacy sentinel test for telemetry"
```

---

## Task 8: Aggregation queries and the `memory_usage` tool

**Files:**
- Create: `src/blueocean_mcp/telemetry/queries.py`
- Modify: `src/blueocean_mcp/tools.py`
- Test: `tests/telemetry_http.py` (new file, the query half)

**Interfaces:**
- Consumes: `db.connect`.
- Produces: `queries.build_stats(conn, days, tz_offset_minutes=0, project=None) -> dict`, `queries.build_usage_summary(conn, days=7, view=None) -> dict`, and the MCP tool `memory_usage(days: int = 7, view: str | None = None) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/telemetry_http.py`:

```python
"""Tests for telemetry aggregation and its HTTP surface.

Run with:
    uv run python -m tests.telemetry_http
"""

import json
import tempfile
import time
from pathlib import Path

from blueocean_mcp.telemetry import db, queries


def _seed(conn, rows: list[dict]) -> None:
    for row in rows:
        cols = list(row)
        conn.execute(
            f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
    conn.commit()


def test_build_stats_shapes_every_panel() -> None:
    print("== build_stats returns one object with every panel ==")
    now = int(time.time())
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [
            {"ts": now - 60, "kind": "tool", "tool": "memory_search", "project": "p",
             "agent_name": "claude-code", "ok": 1, "total_ms": 40.0,
             "result_count": 2, "top_score": 0.8, "tokens_returned": 100,
             "embed_tokens": 50, "est_cost_usd": 0.000001, "origin": "observed"},
            # embed_tokens with no est_cost_usd: an embedding we paid for but
            # could not price. That is what unpriced_calls counts.
            {"ts": now - 30, "kind": "tool", "tool": "memory_search", "project": "p",
             "agent_name": "codex", "ok": 1, "total_ms": 90.0,
             "result_count": 0, "top_score": None, "tokens_returned": 0,
             "embed_tokens": 30, "origin": "observed"},
            {"ts": now - 10, "kind": "tool", "tool": "memory_store", "project": "p",
             "agent_name": "claude-code", "ok": 0, "error_class": "ValueError",
             "error_msg": "too long", "total_ms": 5.0, "origin": "observed"},
            {"ts": now - 5, "kind": "admin", "tool": "prune", "project": "p",
             "ok": 1, "deleted_count": 3, "origin": "cli-reported"},
        ])
        conn.execute(
            "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
            "VALUES ('p', 'never-used', 0, 0, NULL)"
        )
        conn.commit()

        stats = queries.build_stats(conn, days=7, tz_offset_minutes=420)
        assert stats["tiles"]["calls"] == 4, stats["tiles"]
        assert stats["tiles"]["errors"] == 1, stats["tiles"]
        assert stats["tiles"]["p95_ms"] is not None
        tools = {t["tool"]: t for t in stats["tools"]}
        assert tools["memory_search"]["calls"] == 2, tools
        agents = {a["agent_name"]: a["calls"] for a in stats["agents"]}
        assert agents["claude-code"] == 2, agents
        assert stats["quality"]["searches"] == 2, stats["quality"]
        assert abs(stats["quality"]["zero_result_rate"] - 0.5) < 1e-9, stats["quality"]
        assert stats["unused"][0]["point_id"] == "never-used", stats["unused"]
        assert stats["audit"][0]["tool"] == "prune", stats["audit"]
        assert stats["errors"][0]["error_class"] == "ValueError", stats["errors"]
        assert stats["unpriced_calls"] >= 1, stats
        conn.close()
    print("  OK")


def test_day_bucketing_uses_the_callers_offset() -> None:
    """An event at 23:30 local time in UTC+7 is 16:30 UTC the same day. Bucketing
    by UTC would file it under the wrong local day and quietly shift every
    daily number."""
    print("== daily buckets follow the caller's offset ==")
    # 2026-09-02 23:30 at UTC+7 == 2026-09-02 16:30 UTC == epoch 1788366600
    ts = 1788366600
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [{"ts": ts, "kind": "tool", "tool": "memory_search",
                      "ok": 1, "total_ms": 1.0, "origin": "observed"}])
        local = queries.build_stats(conn, days=3650, tz_offset_minutes=420)["daily"]
        utc = queries.build_stats(conn, days=3650, tz_offset_minutes=0)["daily"]
        assert local[-1]["day"] == "2026-09-02", local
        assert utc[-1]["day"] == "2026-09-02", utc

        # And an event at 00:30 local (17:30 UTC the previous day) lands on the
        # local day, not the UTC one.
        conn.execute("DELETE FROM events")
        # 2026-09-02 00:30 at UTC+7 == 2026-09-01 17:30 UTC == epoch 1788283800.
        # Local and UTC land on different days, which is the whole point.
        _seed(conn, [{"ts": 1788283800, "kind": "tool",
                      "tool": "memory_search", "ok": 1, "origin": "observed"}])
        local = queries.build_stats(conn, days=3650, tz_offset_minutes=420)["daily"]
        utc = queries.build_stats(conn, days=3650, tz_offset_minutes=0)["daily"]
        assert local[-1]["day"] != utc[-1]["day"], (local, utc)
        conn.close()
    print("  OK")


def test_usage_summary_is_small() -> None:
    """The agent-facing view has a token budget. Roughly 4 chars per token is
    the same heuristic token_budget.py uses."""
    print("== memory_usage summary stays inside its budget ==")
    now = int(time.time())
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [
            {"ts": now - i, "kind": "tool", "tool": f"memory_tool_{i % 9}",
             "project": f"project-{i % 7}", "agent_name": f"agent-{i % 5}",
             "ok": 1, "total_ms": float(i), "origin": "observed"}
            for i in range(500)
        ])
        summary = queries.build_usage_summary(conn, days=7)
        rendered = json.dumps(summary, ensure_ascii=False)
        assert len(rendered) / 4 < 500, f"~{len(rendered) // 4} tokens, budget is 500"
        assert len(summary["top_tools"]) <= 5, summary["top_tools"]
        assert len(summary["top_agents"]) <= 3, summary["top_agents"]
        conn.close()
    print("  OK")


def test_usage_summary_view_drilldown() -> None:
    print("== the view parameter drills into one list ==")
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        conn.execute(
            "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
            "VALUES ('p', 'cold', 0, 0, NULL)"
        )
        conn.commit()
        out = queries.build_usage_summary(conn, days=7, view="unused")
        assert out["view"] == "unused", out
        assert out["entries"][0]["point_id"] == "cold", out
        conn.close()
    print("  OK")


def main() -> None:
    test_build_stats_shapes_every_panel()
    test_day_bucketing_uses_the_callers_offset()
    test_usage_summary_is_small()
    test_usage_summary_view_drilldown()
    print("\nTELEMETRY HTTP TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_http`
Expected: FAIL with `ImportError: cannot import name 'queries'`

- [ ] **Step 3: Write `queries.py`**

Create `src/blueocean_mcp/telemetry/queries.py`:

```python
"""Aggregations over the event log.

Percentiles are computed in Python rather than SQL: SQLite has no percentile
function, and the row count is bounded by retention (a heavy 1,000-call day is
90,000 rows at 90 days), which is small enough to sort in memory on a local
dashboard.
"""

import sqlite3
import time
from typing import Any


def _window_start(days: int, now: int | None = None) -> int:
    return (now if now is not None else int(time.time())) - days * 86400


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[index]


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def build_stats(
    conn: sqlite3.Connection,
    days: int,
    tz_offset_minutes: int = 0,
    project: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    since = _window_start(days, now)
    scope = "AND project = ?" if project else ""
    scope_params: tuple = (project,) if project else ()

    durations = _rows(
        conn,
        f"SELECT tool, total_ms, ok FROM events WHERE ts >= ? {scope}",
        (since, *scope_params),
    )
    all_ms = [r["total_ms"] for r in durations if r["total_ms"] is not None]
    calls = len(durations)
    errors = sum(1 for r in durations if r["ok"] == 0)

    per_tool: dict[str, dict] = {}
    for row in durations:
        entry = per_tool.setdefault(
            row["tool"], {"tool": row["tool"], "calls": 0, "errors": 0, "_ms": []}
        )
        entry["calls"] += 1
        if row["ok"] == 0:
            entry["errors"] += 1
        if row["total_ms"] is not None:
            entry["_ms"].append(row["total_ms"])
    tools = []
    for entry in sorted(per_tool.values(), key=lambda e: -e["calls"]):
        ms = entry.pop("_ms")
        entry["p50_ms"] = _percentile(ms, 0.50)
        entry["p95_ms"] = _percentile(ms, 0.95)
        tools.append(entry)

    cost_row = conn.execute(
        f"SELECT COALESCE(SUM(est_cost_usd), 0),"
        f" SUM(CASE WHEN embed_tokens IS NOT NULL AND est_cost_usd IS NULL THEN 1 ELSE 0 END),"
        f" COALESCE(SUM(embed_tokens), 0),"
        f" SUM(CASE WHEN tokens_exact = 0 THEN 1 ELSE 0 END)"
        f" FROM events WHERE ts >= ? {scope}",
        (since, *scope_params),
    ).fetchone()

    quality_row = conn.execute(
        f"SELECT COUNT(*),"
        f" SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END),"
        f" AVG(top_score), AVG(tokens_returned)"
        f" FROM events WHERE ts >= ? AND tool = 'memory_search' {scope}",
        (since, *scope_params),
    ).fetchone()
    searches = quality_row[0] or 0

    offset_hours = tz_offset_minutes / 60.0
    modifier = f"{offset_hours:+.4f} hours"
    daily = _rows(
        conn,
        f"SELECT strftime('%Y-%m-%d', ts, 'unixepoch', ?) AS day, COUNT(*) AS calls"
        f" FROM events WHERE ts >= ? {scope} GROUP BY day ORDER BY day",
        (modifier, since, *scope_params),
    )

    return {
        "window": {"days": days, "since": since, "tz_offset_minutes": tz_offset_minutes},
        "tiles": {
            "calls": calls,
            "errors": errors,
            "error_rate": (errors / calls) if calls else 0.0,
            "p95_ms": _percentile(all_ms, 0.95),
            "est_cost_usd": cost_row[0],
            "embed_tokens": cost_row[2],
        },
        "unpriced_calls": cost_row[1] or 0,
        "estimated_token_calls": cost_row[3] or 0,
        "tools": tools,
        "agents": _rows(
            conn,
            f"SELECT agent_name, agent_version, COUNT(*) AS calls FROM events"
            f" WHERE ts >= ? AND agent_name IS NOT NULL {scope}"
            f" GROUP BY agent_name, agent_version ORDER BY calls DESC",
            (since, *scope_params),
        ),
        "quality": {
            "searches": searches,
            "zero_result_rate": ((quality_row[1] or 0) / searches) if searches else 0.0,
            "mean_top_score": quality_row[2],
            "mean_tokens_returned": quality_row[3],
        },
        "projects": _rows(
            conn,
            f"SELECT project, COUNT(*) AS calls FROM events"
            f" WHERE ts >= ? AND project IS NOT NULL {scope}"
            f" GROUP BY project ORDER BY calls DESC",
            (since, *scope_params),
        ),
        # Not windowed, deliberately: entry_hits is cumulative since first
        # observed. The dashboard labels this panel accordingly.
        "unused": _rows(
            conn,
            "SELECT project, point_id, hits, full_hits, last_seen_at FROM entry_hits"
            " ORDER BY hits ASC, last_seen_at ASC LIMIT 50",
        ),
        "audit": _rows(
            conn,
            f"SELECT ts, tool, project, deleted_count, agent_name, origin FROM events"
            f" WHERE ts >= ? AND (kind = 'admin' OR tool = 'memory_delete') {scope}"
            f" ORDER BY ts DESC LIMIT 50",
            (since, *scope_params),
        ),
        "errors": _rows(
            conn,
            f"SELECT ts, tool, error_class, error_msg, project FROM events"
            f" WHERE ts >= ? AND ok = 0 {scope} ORDER BY ts DESC LIMIT 20",
            (since, *scope_params),
        ),
        "daily": daily,
    }


def build_usage_summary(
    conn: sqlite3.Connection,
    days: int = 7,
    view: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """The agent-facing view. Tight by construction, about 500 tokens: an agent
    that needs more asks for one `view` rather than receiving a truncated blob."""
    since = _window_start(days, now)

    if view == "unused":
        return {
            "view": "unused",
            "entries": _rows(
                conn,
                "SELECT project, point_id, hits, full_hits, last_seen_at FROM entry_hits"
                " ORDER BY hits ASC, last_seen_at ASC LIMIT 20",
            ),
        }
    if view == "tools":
        return {
            "view": "tools",
            "entries": _rows(
                conn,
                "SELECT tool, COUNT(*) AS calls, AVG(total_ms) AS mean_ms,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
                " FROM events WHERE ts >= ? GROUP BY tool ORDER BY calls DESC LIMIT 20",
                (since,),
            ),
        }
    if view == "errors":
        return {
            "view": "errors",
            "entries": _rows(
                conn,
                "SELECT ts, tool, error_class, error_msg FROM events"
                " WHERE ts >= ? AND ok = 0 ORDER BY ts DESC LIMIT 10",
                (since,),
            ),
        }

    stats = build_stats(conn, days=days, now=now)
    return {
        "view": "summary",
        "days": days,
        "calls": stats["tiles"]["calls"],
        "errors": stats["tiles"]["errors"],
        "p95_ms": stats["tiles"]["p95_ms"],
        "est_cost_usd": stats["tiles"]["est_cost_usd"],
        "unpriced_calls": stats["unpriced_calls"],
        "zero_result_rate": stats["quality"]["zero_result_rate"],
        "top_tools": [
            {"tool": t["tool"], "calls": t["calls"]} for t in stats["tools"][:5]
        ],
        "top_agents": [
            {"agent": a["agent_name"], "calls": a["calls"]} for a in stats["agents"][:3]
        ],
        "unused_entries": len(stats["unused"]),
        "hint": "call again with view='unused' | 'tools' | 'errors' for detail",
    }
```

- [ ] **Step 4: Add the `memory_usage` tool**

In `src/blueocean_mcp/tools.py`, add alongside the other tool functions:

```python
    def memory_usage(days: int = 7, view: str | None = None) -> dict:
        """Report how memory has been used recently: call counts, latency,
        search quality, embedding cost, and entries never retrieved.

        Defaults to a compact summary over the last 7 days. Pass ``view`` as
        ``"unused"``, ``"tools"`` or ``"errors"`` for one drill-down list.
        Returns ``{"enabled": False}`` when telemetry is switched off.
        """
        from .telemetry import db as telemetry_db
        from .telemetry import is_enabled
        from .telemetry.queries import build_usage_summary

        if not is_enabled():
            return {"enabled": False, "reason": "BLUEOCEAN_TELEMETRY=0"}
        conn = telemetry_db.connect()
        try:
            return build_usage_summary(conn, days=days, view=view)
        finally:
            conn.close()
```

and add it to the `registrations` list:

```python
        (memory_usage, "memory_usage",
         "Report recent memory usage: calls, latency, search quality, cost, unused entries."),
```

It is already on `INSTRUMENT_DENYLIST`, so the loop registers it uninstrumented. `tests/telemetry_instrument.py::test_all_tools_instrumented_except_the_denylist` now proves that: 9 tools registered, 8 instrumented.

- [ ] **Step 5: Run the tests**

Run: `uv run python -m tests.telemetry_http && uv run python -m tests.telemetry_instrument`
Expected: both PASS

- [ ] **Step 6: Commit**

```bash
git add src/blueocean_mcp/telemetry/queries.py src/blueocean_mcp/tools.py tests/telemetry_http.py
git commit -m "Add telemetry aggregation queries and the memory_usage tool"
```

---

## Task 9: HTTP surface

**Files:**
- Create: `src/blueocean_mcp/telemetry/routes.py`
- Modify: `src/blueocean_mcp/__main__.py`
- Test: `tests/telemetry_http.py` (append)

**Interfaces:**
- Consumes: `queries.build_stats`, `db.connect`, `pricing.write_file`, `writer.get_writer`, `auth.wrap_with_auth`.
- Produces: `routes.telemetry_routes() -> list[starlette.routing.Route]` covering `GET /dashboard`, `GET /api/stats`, `POST /api/audit`, `POST /api/prices`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/telemetry_http.py` and register in `main()`. This step also
adds the imports its helpers need, which Task 8 deliberately left out rather
than leaving them unused:

```python
import subprocess
import urllib.error
import urllib.request

from ._helpers import BIN

PORT = 8798


def _start_server(env_extra: dict, token: str | None = None) -> subprocess.Popen:
    import os
    env = {**os.environ, **env_extra}
    args = [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(PORT)]
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"server exited early:\n{err}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise TimeoutError("server did not start")


def _get(path: str, token: str | None = None) -> tuple[int, dict | str]:
    url = f"http://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_stats_requires_a_token() -> None:
    print("== /api/stats and /dashboard require the token ==")
    with tempfile.TemporaryDirectory() as d:
        proc = _start_server({
            "BLUEOCEAN_AUTH_TOKEN": "secret-token",
            "BLUEOCEAN_TELEMETRY_DB": str(Path(d) / "t.db"),
        })
        try:
            for path in ("/api/stats", "/dashboard", "/api/audit", "/api/prices"):
                status, _ = _get(path)
                assert status == 401, f"{path} returned {status}, expected 401"
            status, body = _get("/api/stats", token="secret-token")
            assert status == 200, (status, body)
            assert "tiles" in body, body
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    print("  OK")


def test_disabled_returns_503_not_404() -> None:
    """A 404 makes a deliberate configuration look like a broken deployment."""
    print("== telemetry off answers 503 with an explanation ==")
    proc = _start_server({"BLUEOCEAN_TELEMETRY": "0", "BLUEOCEAN_AUTH_TOKEN": ""})
    try:
        for path in ("/api/stats", "/dashboard"):
            status, body = _get(path)
            assert status == 503, f"{path} returned {status}"
            assert "BLUEOCEAN_TELEMETRY" in str(body), body
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    print("  OK")


def test_audit_row_cannot_claim_to_be_observed() -> None:
    """One shared token means anyone who can call a tool can post an audit row.
    The server stamps what it can verify and refuses the rest."""
    print("== posted audit rows are stamped cli-reported ==")
    with tempfile.TemporaryDirectory() as d:
        db_file = str(Path(d) / "t.db")
        proc = _start_server({
            "BLUEOCEAN_AUTH_TOKEN": "secret-token",
            "BLUEOCEAN_TELEMETRY_DB": db_file,
        })
        try:
            payload = json.dumps({
                "tool": "prune", "project": "p", "deleted_count": 9,
                "origin": "observed", "ts": 1, "agent_name": "someone-else",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/audit", data=payload,
                headers={"Authorization": "Bearer secret-token",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 202, resp.status
            time.sleep(1.0)
            status, body = _get("/api/stats", token="secret-token")
            audit = [r for r in body["audit"] if r["tool"] == "prune"]
            assert audit, body["audit"]
            assert audit[0]["origin"] == "cli-reported", audit[0]
            assert audit[0]["ts"] != 1, "the server stamps its own timestamp"
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    print("  OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_http`
Expected: FAIL — the new routes 404 rather than 401/200

- [ ] **Step 3: Write `routes.py`**

Create `src/blueocean_mcp/telemetry/routes.py`:

```python
"""Starlette routes for the telemetry surface.

None of these are auth-exempt. docker-compose really does run with
--host 0.0.0.0, so the dashboard is reachable off-host and must sit behind the
same bearer token as everything else.
"""

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import db, is_enabled, pricing
from .queries import build_stats
from .writer import get_writer

_DISABLED_BODY = {
    "status": "disabled",
    "detail": "Telemetry is off. Set BLUEOCEAN_TELEMETRY=1 to enable it.",
}

_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")


def _disabled() -> JSONResponse:
    return JSONResponse(_DISABLED_BODY, status_code=503)


async def _stats(request: Request) -> JSONResponse:
    if not is_enabled():
        return _disabled()
    try:
        days = int(request.query_params.get("days", "7"))
        tz_offset = int(request.query_params.get("tz_offset_minutes", "0"))
    except ValueError:
        return JSONResponse({"error": "days and tz_offset_minutes must be integers"}, 400)
    project = request.query_params.get("project")
    conn = db.connect()
    try:
        body = build_stats(conn, days=days, tz_offset_minutes=tz_offset, project=project)
    finally:
        conn.close()
    writer = get_writer()
    body["dropped_events"] = writer.dropped if writer is not None else 0
    return JSONResponse(body)


async def _audit(request: Request) -> JSONResponse:
    """Record an audit row reported by the CLI.

    The server stamps the timestamp and origin. A posted row may not claim
    origin='observed': that value is reserved for calls this process handled
    itself. With one shared token this is the strongest guarantee available,
    and the docs say so rather than implying more.
    """
    if not is_enabled():
        return _disabled()
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, 400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, 400)

    import time as _time

    allowed = {"tool", "project", "area", "module", "deleted_count", "ok",
               "error_class", "error_msg", "agent_name"}
    row = {k: v for k, v in payload.items() if k in allowed}
    if not row.get("tool"):
        return JSONResponse({"error": "tool is required"}, 400)
    row["error_msg"] = (str(row["error_msg"])[:200] if row.get("error_msg") else None)
    row["ts"] = int(_time.time())
    row["kind"] = "admin"
    row["origin"] = "cli-reported"
    row.setdefault("ok", 1)

    writer = get_writer()
    if writer is not None:
        writer.record(row)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _prices(request: Request) -> JSONResponse:
    """Accept a price map fetched by the CLI and write the pricing file.

    The CLI makes the outbound call; the server owns the file. That keeps the
    server from reaching the network on its own and keeps the host from
    guessing what path the file has inside the container.
    """
    if not is_enabled():
        return _disabled()
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, 400)
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, dict) or not prices:
        return JSONResponse({"error": "prices must be a non-empty object"}, 400)
    clean = {str(k): float(v) for k, v in prices.items() if isinstance(v, (int, float))}
    pricing.write_file(None, clean)
    return JSONResponse({"status": "written", "models": len(clean)})


async def _dashboard(_request: Request) -> HTMLResponse:
    if not is_enabled():
        return _disabled()
    return HTMLResponse(
        _DASHBOARD_HTML.read_text(),
        headers={"Referrer-Policy": "no-referrer"},
    )


def telemetry_routes() -> list[Route]:
    return [
        Route("/api/stats", _stats),
        Route("/api/audit", _audit, methods=["POST"]),
        Route("/api/prices", _prices, methods=["POST"]),
        Route("/dashboard", _dashboard),
    ]
```

- [ ] **Step 4: Mount the routes and manage the writer's lifecycle**

In `src/blueocean_mcp/__main__.py`, inside `_run()`, after the `/health` insert:

```python
        from .telemetry import is_enabled, shutdown
        from .telemetry.routes import telemetry_routes

        if is_enabled():
            for route in telemetry_routes():
                mcp_app.router.routes.insert(0, route)
```

and wrap the serve call so the queue is drained on exit:

```python
        try:
            await server.serve()
        finally:
            shutdown()
```

Leave `exempt_paths` as `frozenset({"/health"})`. The telemetry routes are deliberately not in it.

- [ ] **Step 5: Create a placeholder dashboard file so the route can be served**

Task 12 writes the real page. For now:

```bash
printf '<!doctype html><title>BlueOcean telemetry</title><p>Dashboard pending.</p>\n' \
  > src/blueocean_mcp/telemetry/dashboard.html
```

- [ ] **Step 6: Run the tests**

Run: `uv run python -m tests.telemetry_http && uv run python -m tests.auth`
Expected: both PASS. `tests/auth.py` proves the existing auth behaviour is unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/blueocean_mcp/telemetry/routes.py src/blueocean_mcp/telemetry/dashboard.html \
        src/blueocean_mcp/__main__.py tests/telemetry_http.py
git commit -m "Serve telemetry over authenticated HTTP routes"
```

---

## Task 10: `blueocean-admin usage`

**Files:**
- Create: `src/blueocean_mcp/telemetry/client.py`
- Modify: `src/blueocean_mcp/admin.py`
- Test: `tests/telemetry_cli.py`

**Interfaces:**
- Consumes: `config.DEFAULT_SERVER_URL`, `queries.build_stats` (only on the `--db` path), `pricing`.
- Produces: `client.fetch_stats(days, tz_offset_minutes, project, server_url=None, token=None) -> dict`, `client.post_audit(row, server_url=None, token=None) -> bool`, `client.post_prices(prices, server_url=None, token=None) -> bool`, `client.ServerUnavailable` exception; `admin.cmd_usage(args)`.

- [ ] **Step 1: Write the failing test**

Create `tests/telemetry_cli.py`:

```python
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
    return subprocess.run([ADMIN, *args], env=env, capture_output=True, text=True)


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m tests.telemetry_cli`
Expected: FAIL with `ImportError: cannot import name 'client'`

- [ ] **Step 3: Write `client.py`**

Create `src/blueocean_mcp/telemetry/client.py`:

```python
"""The admin CLI's view of telemetry: over HTTP, never off disk.

SQLite's locking is not dependable across a Docker Desktop bind mount, so the
server process is the only one that opens the database. The CLI asks the
server. When the server is not there, that is an error the operator sees, not
something to paper over by opening the file anyway.
"""

import json
import urllib.error
import urllib.request

from ..config import DEFAULT_SERVER_URL


class ServerUnavailable(RuntimeError):
    """Raised when the telemetry server cannot be reached or refuses."""


def _request(
    path: str,
    server_url: str | None,
    token: str | None,
    method: str = "GET",
    payload: dict | None = None,
) -> dict:
    base = (server_url or DEFAULT_SERVER_URL).rstrip("/")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise ServerUnavailable(
            f"could not reach telemetry on {base}{path}: HTTP {e.code} {detail}"
        ) from e
    except OSError as e:
        raise ServerUnavailable(f"could not reach telemetry on {base}: {e}") from e


def fetch_stats(
    days: int,
    tz_offset_minutes: int,
    project: str | None,
    server_url: str | None = None,
    token: str | None = None,
) -> dict:
    query = f"?days={days}&tz_offset_minutes={tz_offset_minutes}"
    if project:
        query += f"&project={urllib.request.quote(project)}"
    return _request(f"/api/stats{query}", server_url, token)


def post_audit(
    row: dict, server_url: str | None = None, token: str | None = None
) -> bool:
    _request("/api/audit", server_url, token, method="POST", payload=row)
    return True


def post_prices(
    prices: dict, server_url: str | None = None, token: str | None = None
) -> bool:
    _request("/api/prices", server_url, token, method="POST", payload={"prices": prices})
    return True
```

- [ ] **Step 4: Add the `usage` subcommand**

In `src/blueocean_mcp/admin.py`, add the handler:

```python
def _local_tz_offset_minutes() -> int:
    import time as _time

    return -(_time.altzone if _time.daylight and _time.localtime().tm_isdst else _time.timezone) // 60


def _print_table(title: str, rows: list[dict], columns: list[str]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print("  " + "  ".join(str(row.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def cmd_usage(args: argparse.Namespace) -> None:
    from .telemetry import client

    if args.refresh_prices:
        cmd_refresh_prices(args)
        return

    tz_offset = _local_tz_offset_minutes()
    if args.db:
        # Explicit opt-in: the caller is telling us there is no server, which
        # is true for stdio-only development. Never reached by accident.
        from .telemetry import db as telemetry_db
        from .telemetry.queries import build_stats

        conn = telemetry_db.connect(args.db)
        try:
            stats = build_stats(conn, days=args.days, tz_offset_minutes=tz_offset,
                                project=args.project)
        finally:
            conn.close()
    else:
        try:
            stats = client.fetch_stats(
                days=args.days,
                tz_offset_minutes=tz_offset,
                project=args.project,
                token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None,
            )
        except client.ServerUnavailable as e:
            print(
                f"{e}\nStart the server, or pass --db <path> to read a local "
                "telemetry file directly (development only).",
                file=sys.stderr,
            )
            raise SystemExit(1) from e

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    if args.audit:
        _print_table("Audit", stats["audit"], ["ts", "tool", "project", "deleted_count", "origin"])
        return
    if args.unused:
        _print_table(
            "Never retrieved (since first observed)",
            stats["unused"], ["project", "point_id", "hits", "full_hits", "last_seen_at"],
        )
        return

    tiles = stats["tiles"]
    print(f"Window: last {stats['window']['days']} day(s)")
    print(
        f"  calls={tiles['calls']}  errors={tiles['errors']}  "
        f"p95={tiles['p95_ms']}ms  cost=${tiles['est_cost_usd']:.6f}  "
        f"unpriced={stats['unpriced_calls']}"
    )
    key = {"tool": "tool", "agent": "agent_name", "project": "project", "day": "day"}[args.by]
    source = {"tool": stats["tools"], "agent": stats["agents"],
              "project": stats["projects"], "day": stats["daily"]}[args.by]
    columns = [key, "calls"] + (["p50_ms", "p95_ms", "errors"] if args.by == "tool" else [])
    _print_table(f"By {args.by}", source, columns)
```

Add `import sys` to the imports at the top of `admin.py` if it is not already there, and register the subparser inside `main()`:

```python
    p_usage = sub.add_parser("usage")
    p_usage.add_argument("--project", default=None,
                         help="Scope to one project (default: every project)")
    p_usage.add_argument("--days", type=int, default=7)
    p_usage.add_argument("--by", choices=["tool", "agent", "project", "day"], default="tool")
    p_usage.add_argument("--audit", action="store_true", help="Show the audit trail instead")
    p_usage.add_argument("--unused", action="store_true",
                         help="Show entries never retrieved, since first observed")
    p_usage.add_argument("--json", action="store_true")
    p_usage.add_argument("--db", default=None,
                         help="Read this telemetry file directly instead of asking the "
                              "server. Development only: the server must not be running.")
    p_usage.add_argument("--refresh-prices", action="store_true",
                         help="Fetch embedding prices from OpenRouter and hand them to the server")
    p_usage.set_defaults(func=cmd_usage)
```

- [ ] **Step 5: Report CLI destructive operations to the audit trail**

At the end of `cmd_prune` and `cmd_restore` in `admin.py`, after the operation succeeds:

```python
    _report_audit({"tool": "prune", "project": args.project, "deleted_count": deleted})
```

(use `"restore"` and the restored count in `cmd_restore`), with this helper:

```python
def _report_audit(row: dict) -> None:
    """Send an audit row to the server. If the server is not reachable we do
    NOT write the file: that would be a silent fallback to the host's default
    path while the real database lives at the container's, producing a record
    nobody ever reads. Print it instead so the operator still has it."""
    from .telemetry import client

    try:
        client.post_audit(row, token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None)
    except client.ServerUnavailable as e:
        print(f"warning: audit row not recorded ({e})", file=sys.stderr)
        print(json.dumps({"unrecorded_audit": row}), file=sys.stderr)
```

- [ ] **Step 6: Run the tests**

Run: `uv run python -m tests.telemetry_cli && uv run python -m tests.backup`
Expected: both PASS. `tests/backup.py` proves `prune`/`restore` still behave with the audit hook attached.

- [ ] **Step 7: Commit**

```bash
git add src/blueocean_mcp/telemetry/client.py src/blueocean_mcp/admin.py tests/telemetry_cli.py
git commit -m "Add blueocean-admin usage reading telemetry over HTTP"
```

---

## Task 11: OpenRouter price refresh

**Files:**
- Modify: `src/blueocean_mcp/telemetry/pricing.py`
- Modify: `src/blueocean_mcp/admin.py`
- Test: `tests/telemetry_pricing.py` (append), `tests/telemetry_cli.py` (append)

**Interfaces:**
- Consumes: `client.post_prices`, `pricing.write_file`.
- Produces: `pricing.OPENROUTER_MODELS_URL: str`, `pricing.parse_openrouter(payload: dict) -> dict[str, float]`, `pricing.fetch_openrouter(timeout=10) -> dict[str, float]`; `admin.cmd_refresh_prices(args)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/telemetry_pricing.py` and register in `main()`:

```python
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
```

Append to `tests/telemetry_cli.py` and register in `main()`:

```python
def test_refresh_prices_never_opens_a_local_file() -> None:
    print("== --refresh-prices posts to the server, opens no file ==")
    with tempfile.TemporaryDirectory() as d:
        pricing_file = Path(d) / "pricing.json"
        result = _run(
            ["usage", "--refresh-prices"],
            {
                "BLUEOCEAN_SERVER_URL": f"http://127.0.0.1:{DEAD_PORT}",
                "BLUEOCEAN_PRICING_FILE": str(pricing_file),
                "BLUEOCEAN_OFFLINE_TEST": "1",
            },
        )
        assert result.returncode != 0, result.stdout
        assert not pricing_file.exists(), (
            "without --db the CLI must hand prices to the server, never write them"
        )
    print("  OK")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m tests.telemetry_pricing`
Expected: FAIL with `AttributeError: module ... has no attribute 'parse_openrouter'`

- [ ] **Step 3: Add the fetch and parse functions**

Append to `src/blueocean_mcp/telemetry/pricing.py`:

```python
# The embeddings feed is a different endpoint from /api/v1/models, which
# returns chat models only and contains no embedding models at all. No API key
# is required. Verified 2026-09-02: 33 models, pricing.prompt in USD per token.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/embeddings/models"


def retains_data(model_id: str) -> bool:
    """OpenRouter's `:free` tier states that requests and embeddings may be
    retained and used for training. Callers must label these rather than show
    an attractive $0."""
    return model_id.endswith(":free")


def parse_openrouter(payload: dict) -> dict[str, float]:
    """Convert the feed's USD-per-token strings into USD per 1M tokens.

    A model with no usable price is skipped, not recorded as free: unknown and
    zero mean different things everywhere else in this file.
    """
    out: dict[str, float] = {}
    for model in payload.get("data", []):
        model_id = model.get("id")
        raw = (model.get("pricing") or {}).get("prompt")
        if not model_id or raw is None:
            continue
        try:
            out[model_id] = float(raw) * 1_000_000
        except (TypeError, ValueError):
            continue
    return out


def fetch_openrouter(timeout: float = 10.0) -> dict[str, float]:
    import json as _json
    import urllib.request

    with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=timeout) as resp:
        payload = _json.loads(resp.read().decode())
    return parse_openrouter(payload)
```

- [ ] **Step 4: Add the CLI command**

In `src/blueocean_mcp/admin.py`:

```python
def cmd_refresh_prices(args: argparse.Namespace) -> None:
    """Fetch embedding prices from OpenRouter and hand them to the server.

    This is the only command that sends anything off this machine. The server
    deliberately never calls out on its own: an automatic refresh would make
    an outbound request nobody asked for and add a hidden network dependency
    to a dashboard meant to work air-gapped.
    """
    from .telemetry import client, pricing

    if os.getenv("BLUEOCEAN_OFFLINE_TEST") == "1":
        prices = {"openai/text-embedding-3-small": 0.02}
    else:
        prices = pricing.fetch_openrouter()
    if not prices:
        print("OpenRouter returned no usable prices", file=sys.stderr)
        raise SystemExit(1)

    if args.db:
        pricing.write_file(None, prices)
        print(f"Wrote {len(prices)} model prices to the local pricing file")
        return

    try:
        client.post_prices(prices, token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None)
    except client.ServerUnavailable as e:
        print(
            f"{e}\nPrices were fetched but not stored. Start the server, or pass "
            "--db to write the local pricing file directly (development only).",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    flagged = [m for m in prices if pricing.retains_data(m)]
    print(f"Sent {len(prices)} model prices to the server")
    if flagged:
        print(
            f"note: {len(flagged)} of these are OpenRouter ':free' models, which may "
            "retain requests and embeddings for training"
        )
```

- [ ] **Step 5: Run the tests**

Run: `uv run python -m tests.telemetry_pricing && uv run python -m tests.telemetry_cli`
Expected: both PASS

- [ ] **Step 6: Commit**

```bash
git add src/blueocean_mcp/telemetry/pricing.py src/blueocean_mcp/admin.py \
        tests/telemetry_pricing.py tests/telemetry_cli.py
git commit -m "Refresh embedding prices from OpenRouter on demand"
```

---

## Task 12: The dashboard page

**Files:**
- Modify: `src/blueocean_mcp/telemetry/dashboard.html` (replacing the placeholder)
- Modify: `pyproject.toml`
- Test: manual, described below

**Interfaces:**
- Consumes: `GET /api/stats?days=&tz_offset_minutes=`.
- Produces: nothing importable.

- [ ] **Step 1: Package the HTML file**

The page is loaded from disk at request time, so it must ship with the wheel. Add to `pyproject.toml`:

```toml
[tool.setuptools.package-data]
blueocean_mcp = ["telemetry/dashboard.html"]
```

- [ ] **Step 2: Write the page**

Replace `src/blueocean_mcp/telemetry/dashboard.html` with a single file: no build step, no CDN, inline SVG only, so it works air-gapped.

```html
<!doctype html>
<meta charset="utf-8">
<title>BlueOcean memory usage</title>
<style>
  :root { color-scheme: light dark; --fg: #16181d; --muted: #6b7280; --line: #e5e7eb; --bg: #fbfbfd; }
  @media (prefers-color-scheme: dark) {
    :root { --fg: #e8eaed; --muted: #9aa0a6; --line: #2a2e35; --bg: #14161a; }
  }
  body { margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
         font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  h2 { font-size: 14px; margin: 28px 0 8px; }
  .muted { color: var(--muted); font-size: 12px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-top: 16px; }
  .tile { border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
  .tile .v { font-size: 22px; font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
           font-variant-numeric: tabular-nums; }
  th { font-weight: 600; color: var(--muted); font-size: 12px; }
  .wrap { overflow-x: auto; }
  button { font: inherit; padding: 6px 12px; border: 1px solid var(--line);
           border-radius: 6px; background: transparent; color: inherit; cursor: pointer; }
</style>

<h1>BlueOcean memory usage</h1>
<p class="muted" id="window">loading…</p>
<p>
  <label>Range
    <select id="days">
      <option value="1">24h</option>
      <option value="7" selected>7 days</option>
      <option value="30">30 days</option>
    </select>
  </label>
  <button id="refresh">Refresh</button>
</p>

<div class="tiles" id="tiles"></div>
<div id="panels"></div>

<script>
// The token arrives in the URL because clients that cannot set an
// Authorization header pass it that way. Strip it from the address bar
// immediately and keep it in memory only: the page is also served with
// Referrer-Policy: no-referrer so it cannot leak onward.
const params = new URLSearchParams(location.search);
const TOKEN = params.get("token");
if (TOKEN) {
  params.delete("token");
  const clean = location.pathname + (params.toString() ? "?" + params : "");
  history.replaceState({}, "", clean);
}

const el = (tag, attrs = {}, kids = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else node.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) {
    node.append(kid instanceof Node ? kid : document.createTextNode(kid));
  }
  return node;
};

const num = (v, digits = 0) =>
  v === null || v === undefined ? "-" : Number(v).toFixed(digits);

function table(rows, columns) {
  const t = el("table");
  t.append(el("tr", {}, columns.map((c) => el("th", {}, c))));
  if (!rows.length) {
    t.append(el("tr", {}, el("td", { colspan: columns.length }, "none")));
    return t;
  }
  for (const row of rows) {
    t.append(el("tr", {}, columns.map((c) => el("td", {}, String(row[c] ?? "-")))));
  }
  return t;
}

function sparkline(daily) {
  const w = 480, h = 60, pad = 2;
  const values = daily.map((d) => d.calls);
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? (w - pad * 2) / (values.length - 1) : 0;
  const points = values
    .map((v, i) => `${pad + i * step},${h - pad - (v / max) * (h - pad * 2)}`)
    .join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(h));
  const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  path.setAttribute("points", points);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.5");
  svg.append(path);
  return svg;
}

function panel(title, node, note) {
  const box = el("section");
  box.append(el("h2", {}, title));
  if (note) box.append(el("p", { class: "muted" }, note));
  box.append(el("div", { class: "wrap" }, node));
  return box;
}

async function load() {
  const days = document.getElementById("days").value;
  const tz = -new Date().getTimezoneOffset();
  const headers = TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};
  const resp = await fetch(`/api/stats?days=${days}&tz_offset_minutes=${tz}`, { headers });
  if (!resp.ok) {
    document.getElementById("window").textContent =
      `Could not load stats: HTTP ${resp.status}`;
    return;
  }
  const s = await resp.json();

  document.getElementById("window").textContent =
    `Last ${s.window.days} day(s) · ${s.unpriced_calls} unpriced call(s) · ` +
    `${s.estimated_token_calls} call(s) with estimated token counts · ` +
    `${s.dropped_events} dropped event(s)`;

  const tiles = [
    ["Calls", num(s.tiles.calls)],
    ["p95 latency", `${num(s.tiles.p95_ms, 1)} ms`],
    ["Error rate", `${num(s.tiles.error_rate * 100, 1)}%`],
    ["Est. cost", `$${num(s.tiles.est_cost_usd, 6)}`],
  ];
  const tileBox = document.getElementById("tiles");
  tileBox.replaceChildren(
    ...tiles.map(([label, value]) =>
      el("div", { class: "tile" }, [el("div", { class: "muted" }, label),
                                    el("div", { class: "v" }, value)])
    )
  );

  const panels = document.getElementById("panels");
  panels.replaceChildren(
    panel("Calls per day", sparkline(s.daily)),
    panel("By tool", table(s.tools, ["tool", "calls", "p50_ms", "p95_ms", "errors"])),
    panel("By agent", table(s.agents, ["agent_name", "agent_version", "calls"])),
    panel("Search quality", table([s.quality],
      ["searches", "zero_result_rate", "mean_top_score", "mean_tokens_returned"])),
    panel("By project", table(s.projects, ["project", "calls"])),
    panel("Never retrieved", table(s.unused,
      ["project", "point_id", "hits", "full_hits", "last_seen_at"]),
      "Counted since first observed, not within the selected range."),
    panel("Audit", table(s.audit, ["ts", "tool", "project", "deleted_count", "origin"]),
      "Rows marked cli-reported were sent by a client. This trail explains " +
      "accidents; with one shared token it cannot prove a row was not forged."),
    panel("Recent errors", table(s.errors, ["ts", "tool", "error_class", "error_msg"]))
  );
}

document.getElementById("refresh").addEventListener("click", load);
document.getElementById("days").addEventListener("change", load);
load();
</script>
```

- [ ] **Step 3: Verify it by hand**

```bash
uv run blueocean-mcp --transport streamable-http --host 127.0.0.1 --port 8765 &
uv run python -m tests.smoke          # generate some events
open "http://127.0.0.1:8765/dashboard?token=$BLUEOCEAN_AUTH_TOKEN"
```

Expected: the tiles show non-zero calls, the token disappears from the address bar on load, the "Never retrieved" panel carries its "since first observed" note, and the page renders with no network requests other than `/api/stats`.

- [ ] **Step 4: Confirm the packaged file ships**

Run: `uv run python -c "from pathlib import Path; import blueocean_mcp.telemetry.routes as r; print(r._DASHBOARD_HTML.exists())"`
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add src/blueocean_mcp/telemetry/dashboard.html pyproject.toml
git commit -m "Add the single-file telemetry dashboard"
```

---

## Task 13: Deployment and documentation

**Files:**
- Modify: `docker-compose.yml`, `Dockerfile`, `.gitignore`, `README.md`
- Test: manual, described below

**Interfaces:**
- Consumes: everything.
- Produces: a `./data` bind mount holding `telemetry.db` and `pricing.json`.

- [ ] **Step 1: Give the container a writable data directory**

In `Dockerfile`, alongside the existing `useradd` line, create the directory before dropping privileges:

```dockerfile
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser && \
    mkdir -p /data && chown -R appuser:appuser /app /data && \
    chmod -R a+rwX /tmp/fastembed_cache
```

- [ ] **Step 2: Mount it and point the server at it**

In `docker-compose.yml`, under the `blueocean-mcp` service, add:

```yaml
    volumes:
      # A bind mount rather than a named volume so the host can back the file
      # up and inspect it. The service had no volumes at all, so a telemetry
      # database written inside the container died on every rebuild.
      - ./data:/data
    environment:
      - BLUEOCEAN_AUTH_TOKEN=${BLUEOCEAN_AUTH_TOKEN:-}
      # Both files live in the bind mount, and the SERVER is the only process
      # that opens them: SQLite locking is not dependable across this mount,
      # so blueocean-admin reads over HTTP instead.
      - BLUEOCEAN_TELEMETRY_DB=/data/telemetry.db
      - BLUEOCEAN_PRICING_FILE=/data/pricing.json
```

(The `environment:` block already exists; add the two new lines to it rather than duplicating the key.)

- [ ] **Step 3: Keep the data out of git**

Append to `.gitignore`:

```
# Telemetry database and price cache, bind-mounted into the container.
data/
```

- [ ] **Step 4: Document it**

Add a "Usage telemetry" section to `README.md` after "Running the tests", covering: what is recorded and what is deliberately not; `BLUEOCEAN_TELEMETRY=0`; the three readers with one example each; the audit trail's honest limit; and how to refresh prices.

```markdown
## Usage telemetry

Every MCP tool call is recorded to a local SQLite event log: which tool, which
project, which agent (from the MCP `clientInfo` handshake), how long it took,
whether it failed, how many results a search returned, and what the embedding
cost. It is on by default and never leaves the machine.

It never stores query text, memory content, summaries, entry metadata, or
bearer tokens. A test asserts this by pushing unique sentinel strings through
every input and dumping every column to prove they are absent.

Turn it off with `BLUEOCEAN_TELEMETRY=0`, in which case no database file is
opened and the HTTP endpoints answer 503.

Three ways to read it:

```bash
# From the terminal (reads over HTTP; the server owns the file)
uv run blueocean-admin usage --days 7 --by tool
uv run blueocean-admin usage --unused     # entries never retrieved

# In the browser
open "http://127.0.0.1:8765/dashboard?token=$BLUEOCEAN_AUTH_TOKEN"
```

Agents can call the `memory_usage` tool, which returns a compact summary
inside a token budget, with `view="unused" | "tools" | "errors"` for detail.

Embedding prices come from a small built-in table. To refresh them from
OpenRouter's public embedding price feed:

```bash
uv run blueocean-admin usage --refresh-prices
```

That is the only command in the project that sends anything off the machine.
The server never calls out on its own.

Note on the audit trail: destructive operations are recorded, and rows the
server observed itself are distinguished from rows a client reported. With a
single shared auth token this explains accidents; it cannot prove a row was
not forged.
```

- [ ] **Step 5: Verify the whole thing end to end**

```bash
docker compose up -d --build
sleep 20
curl -s -H "Authorization: Bearer $BLUEOCEAN_AUTH_TOKEN" \
     "http://localhost:8765/api/stats?days=7" | head -c 400
ls -la ./data
docker compose down && docker compose up -d
curl -s -H "Authorization: Bearer $BLUEOCEAN_AUTH_TOKEN" \
     "http://localhost:8765/api/stats?days=7" | head -c 200
```

Expected: `./data/telemetry.db` exists on the host, and the call counts survive the restart. That is the check that the bind mount actually works, which is the failure the whole storage design exists to prevent.

- [ ] **Step 6: Run the full suite**

```bash
for t in smoke auth mcp_e2e cross_agent mcp_full_coverage concurrency limits \
         project_names backup health http_transport telemetry_db \
         telemetry_instrument telemetry_pricing telemetry_privacy \
         telemetry_http telemetry_cli; do
  echo "== $t =="; uv run python -m tests.$t || break
done
```

Expected: every file prints its PASSED line.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml Dockerfile .gitignore README.md
git commit -m "Persist telemetry across container rebuilds and document it"
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: §4.1 events to Task 1, §4.2 `entry_hits` to Tasks 1 and 6, §4.3 sessions to Task 3, §5 instrumentation to Tasks 3 and 4, §6 cost to Tasks 5 and 11, §7.1 `memory_usage` to Task 8, §7.2 CLI to Tasks 10 and 11, §7.3 dashboard to Task 12, §7.4 `/api/stats` to Task 9, §8 storage and `/api/audit` to Tasks 9, 10 and 13, §9 privacy to Task 7, §10's 17 tests to the test files listed above, §11's order to the task order, §13's open risks to the notes carried in the code comments.

**Gap found and closed during review.** `agent_session_label` (§4.3) was created in Task 1 and populated nowhere. Task 6 now carries an extractor that fills it from `memory_summarize_session`'s `session_id` argument, plus the test that proves it.

**Known remaining limit.** The per-process UUID that stands in for `session_id` on stdio (§4.3) is not implemented: on stdio `ctx.headers` is `None`, so `session_id` stays NULL there. Grouping by session works over streamable-http, which is how every registered agent connects; stdio is the development path. Add the fallback UUID in Task 3's `_agent_identity` if stdio grouping is ever wanted.

**Type consistency.** `writer.record(row: dict)` everywhere; `writer.record_hits(project, summary_ids, full_ids)` matches `_safe_hits`; `pricing.resolve` returns `(float | None, str | None)` and both call sites unpack two values; `queries.build_stats(conn, days, tz_offset_minutes, project, now)` is called with keywords in every caller; `client.fetch_stats` returns the same dict shape the dashboard reads.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-02-telemetry-usage.md`. Two execution options:

**1. Subagent-Driven (recommended)** - a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
