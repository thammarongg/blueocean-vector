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
from typing import ClassVar

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
            assert msg == "<redacted: third-party exception>", msg
            conn.close()
        finally:
            w.stop()
    print("  OK")


def test_error_message_redacts_caller_text() -> None:
    print("== caller text is redacted from recorded error messages ==")

    from blueocean_mcp.config import MAX_CONTENT_CHARS
    from blueocean_mcp.embeddings import create_embedder
    from blueocean_mcp.vector_store import VectorStore, collection_name

    class Capture:
        def __init__(self):
            self.rows = []

        def record(self, row):
            self.rows.append(row)

    def capture_call(fn, **kwargs) -> dict:
        capture = Capture()
        wrapped = instrument(fn, "memory_demo", writer_factory=lambda: capture)
        try:
            wrapped(**kwargs, ctx=None)
        except ValueError:
            pass
        assert len(capture.rows) == 1, capture.rows
        return capture.rows[0]

    caller_text = "caller-secret-content-123"
    row = capture_call(create_embedder, provider=caller_text)
    assert row["error_msg"] == "<redacted: contained caller text>", row
    assert row["error_class"] == "ValueError", row

    store = VectorStore.__new__(VectorStore)
    content = "unrelated caller text" * (MAX_CONTENT_CHARS // 10)
    row = capture_call(
        store.store,
        project="p",
        area="a",
        module="m",
        content=content,
        summary="s",
        importance=3,
    )
    assert row["error_msg"] == (
        f"content is {len(content)} chars, over the {MAX_CONTENT_CHARS}-char "
        "limit (BLUEOCEAN_MAX_CONTENT_CHARS) -- shorten it or split it into "
        "multiple entries"
    ), row
    assert row["error_class"] == "ValueError", row

    project = "Long Project Name"
    row = capture_call(collection_name, project=project)
    assert project in row["error_msg"], row
    assert row["error_class"] == "ValueError", row
    print("  OK")


def test_exception_raised_in_test_helper_is_redacted_by_origin() -> None:
    print("== an exception raised outside the package is redacted by origin ==")

    class Capture:
        def __init__(self):
            self.rows = []

        def record(self, row):
            self.rows.append(row)

    def third_party_helper() -> None:
        raise RuntimeError("private datastore payload")

    capture = Capture()
    wrapped = instrument(third_party_helper, "memory_demo", writer_factory=lambda: capture)
    try:
        wrapped(ctx=None)
    except RuntimeError:
        pass

    assert len(capture.rows) == 1, capture.rows
    row = capture.rows[0]
    assert row["error_msg"] == "<redacted: third-party exception>", row
    assert row["error_class"] == "RuntimeError", row
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
            hits = {
                r[0]: (r[1], r[2])
                for r in conn.execute("SELECT point_id, hits, full_hits FROM entry_hits")
            }
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


def main() -> None:
    test_ctx_is_injected_and_hidden()
    test_call_is_recorded_with_timing()
    test_failure_is_recorded_and_reraised()
    test_error_message_redacts_caller_text()
    test_exception_raised_in_test_helper_is_redacted_by_origin()
    test_telemetry_failure_does_not_break_the_tool()
    test_client_info_is_recorded()
    test_missing_client_info_is_not_fatal()
    test_memory_usage_is_on_the_denylist()
    test_all_tools_instrumented_except_the_denylist()
    test_usage_accumulates_and_is_isolated_per_call()
    test_estimated_usage_is_flagged_inexact()
    test_bedrock_usage_accumulates_across_texts()
    test_search_quality_and_hits_are_recorded()
    test_zero_result_search_is_recorded()
    test_memory_get_counts_as_a_full_hit()
    test_summarize_session_records_its_label()
    test_memory_delete_removes_entry_hits()
    print("\nTELEMETRY INSTRUMENT TEST PASSED")


if __name__ == "__main__":
    main()
