"""The Hermes provider's bridge, against a real server over HTTP.

Hermes calls a memory provider synchronously; the MCP client is async and
keeps a session alive across calls. These tests pin what a person chatting
with Hermes would notice: recall works with the right token, a wrong token or
a stopped server costs one quick failure rather than a five-second wait every
turn, writes survive a short outage, and a server restart (docker rebuild)
does not break the session until Hermes restarts.

Run with:
    uv run python -m tests.hermes_bridge
"""

import time

from ._helpers import reset_project
from ._hermes import load_plugin_module, start_server, stop_server

bridge = load_plugin_module("bridge")

PORT = 8796
URL = f"http://127.0.0.1:{PORT}/mcp"
TOKEN = "bridge-test-token"
PROJECT = "hermes-bridge-project"


def test_breaker_unit() -> None:
    print("== breaker: opens on failure, logs once per outage, closes on success ==")
    now = [0.0]
    b = bridge.Breaker(cooldown=30.0, clock=lambda: now[0])
    assert b.allow() and b.remaining() == 0.0
    assert b.failure() is True, "first failure starts an outage"
    assert not b.allow() and b.remaining() == 30.0
    now[0] = 10.0
    assert b.failure() is False, "a failure inside an outage is not a new outage"
    assert b.remaining() == 30.0, "a new failure restarts the cooldown"
    now[0] = 41.0
    assert b.allow()
    assert b.success() is True and b.success() is False
    print("  OK")


def test_round_trip(proc_url: str) -> None:
    print("== call/submit/drain against a live server ==")
    b = bridge.Bridge(proc_url, TOKEN)
    point_id = b.call("memory_store", {
        "project": PROJECT, "area": "backend", "module": "cache",
        "content": "We chose redis for the session cache.",
        "summary": "Chose redis for the session cache", "importance": 4,
    })
    assert isinstance(point_id, str) and point_id, point_id
    found = b.call("memory_search", {"project": PROJECT, "query": "session cache choice"})
    assert point_id in {r["id"] for r in found["summary"]}

    b.submit("memory_store", {
        "project": PROJECT, "area": "hermes", "module": "turns",
        "content": "User: cache?\n\nAssistant: redis", "summary": "cache? -> redis",
        "importance": 1, "metadata": {"kind": "hermes_turn"},
    })
    assert b.drain(10.0), "queued write did not finish"
    traces = b.call("memory_search", {"project": PROJECT, "query": "cache redis",
                                      "kind": "hermes_turn"})
    assert len(traces["summary"]) == 1, traces
    print("  OK")


def test_tool_error_is_not_an_outage(proc_url: str) -> None:
    print("== a rejected argument raises ToolCallError and keeps the breaker shut ==")
    b = bridge.Bridge(proc_url, TOKEN)
    try:
        b.call("memory_store", {"project": "Not A Valid Name", "area": "a", "module": "m",
                                "content": "c", "summary": "s"})
    except bridge.ToolCallError:
        pass
    else:
        raise AssertionError("an invalid project name must raise ToolCallError")
    assert b.call("memory_list_projects", {})["projects"], "breaker opened on a tool error"
    print("  OK")


def test_wrong_token(proc_url: str) -> None:
    print("== a wrong token fails once, clearly, then fails fast ==")
    b = bridge.Bridge(proc_url, "not-the-token", cooldown=30.0)
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "BLUEOCEAN_AUTH_TOKEN" in str(exc), str(exc)
    else:
        raise AssertionError("a wrong token must raise")
    started = time.monotonic()
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "paused" in str(exc), str(exc)
    assert time.monotonic() - started < 0.1, "an open breaker must not wait"
    print("  OK")


def test_dead_server() -> None:
    print("== a stopped server: quick failure, fast skips, retry after cooldown ==")
    dead = "http://127.0.0.1:8794/mcp"
    b = bridge.Bridge(dead, TOKEN, cooldown=1.0)
    started = time.monotonic()
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "unreachable" in str(exc), str(exc)
    assert time.monotonic() - started < 2.0, "a refused connection should fail fast"
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "paused" in str(exc), str(exc)
    time.sleep(1.1)
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "unreachable" in str(exc), "after the cooldown it must try again"
    print("  OK")


def test_restart_reconnects() -> None:
    print("== a server restart (docker rebuild) is absorbed by one reconnect ==")
    proc = start_server(PORT, TOKEN)
    b = bridge.Bridge(URL, TOKEN)
    try:
        assert b.call("memory_list_projects", {})["projects"] is not None
    finally:
        stop_server(proc)
    proc = start_server(PORT, TOKEN)
    try:
        assert b.call("memory_list_projects", {})["projects"] is not None
    finally:
        stop_server(proc)
    print("  OK")


def test_writes_survive_outage() -> None:
    print("== writes queued while the server is down are delivered when it is back ==")
    b = bridge.Bridge(URL, TOKEN, cooldown=1.0)
    b.submit("memory_store", {
        "project": PROJECT, "area": "hermes", "module": "turns",
        "content": "User: during outage\n\nAssistant: queued", "summary": "outage -> queued",
        "importance": 1, "metadata": {"kind": "hermes_turn"},
    })
    time.sleep(1.5)  # at least one failed attempt
    proc = start_server(PORT, TOKEN)
    try:
        assert b.drain(15.0), "the queued write was never delivered"
        traces = b.call("memory_search", {"project": PROJECT, "query": "outage queued",
                                          "kind": "hermes_turn"})
        assert any("outage" in r["summary"] for r in traces["summary"]), traces
    finally:
        stop_server(proc)
    print("  OK")


def main() -> None:
    reset_project(PROJECT)
    test_breaker_unit()
    proc = start_server(PORT, TOKEN)
    try:
        test_round_trip(URL)
        test_tool_error_is_not_an_outage(URL)
        test_wrong_token(URL)
    finally:
        stop_server(proc)
    test_dead_server()
    test_restart_reconnects()
    test_writes_survive_outage()
    print("\nHERMES BRIDGE TEST PASSED")


if __name__ == "__main__":
    main()
