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
        last_purge = time.monotonic()
        try:
            db.purge_old(conn, TELEMETRY_RETENTION_DAYS)
        except Exception:
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
                logger.debug("telemetry connection close failed", exc_info=True)

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
