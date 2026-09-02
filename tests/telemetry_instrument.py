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
    test_memory_usage_is_on_the_denylist()
    test_all_tools_instrumented_except_the_denylist()
    print("\nTELEMETRY INSTRUMENT TEST PASSED")


if __name__ == "__main__":
    main()
