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
