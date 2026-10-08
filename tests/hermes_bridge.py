"""The Hermes provider's bridge, against a real server over HTTP.

Hermes calls a memory provider synchronously; the MCP client is async and
keeps a session alive across calls. These tests pin what a person chatting
with Hermes would notice: recall works with the right token, a wrong token or
a stopped server costs one quick failure rather than a five-second wait every
turn, writes survive a short outage, and a server restart (docker rebuild)
does not break the session until Hermes restarts. Two callers that hit the
same dead session reconnect once, without tearing down each other's session.

Run with:
    uv run python -m tests.hermes_bridge
"""

import asyncio
import concurrent.futures
import time
from types import SimpleNamespace

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


def test_stalled_session_leaves_room_to_reconnect() -> None:
    print("== a session that stops answering leaves time for the reconnect ==")

    class Stalled:
        async def call_tool(self, tool, args):
            await asyncio.sleep(3600)

    class Fresh:
        async def call_tool(self, tool, args):
            return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')],
                                   is_error=False)

    sessions = [Stalled(), Fresh()]

    class Scripted(bridge.Bridge):
        async def _ensure_session(self):
            return sessions.pop(0)

    b = Scripted("http://127.0.0.1:9/mcp", TOKEN, call_timeout=2.0)
    started = time.monotonic()
    assert b.call("memory_list_projects", {}) == {"ok": True}, "the retry never ran"
    assert time.monotonic() - started < 2.0
    print("  OK")


def test_retired_owner_never_publishes(proc_url: str) -> None:
    print("== a session that finishes connecting after it was given up on closes itself ==")
    real = bridge.ClientSession
    gate = asyncio.Event()
    slow_owners: list = []

    class SlowFirstInit(real):
        async def initialize(self):
            if not slow_owners:
                slow_owners.append(asyncio.current_task())
                await gate.wait()
            return await super().initialize()

    bridge.ClientSession = SlowFirstInit
    try:
        # The caller gives up while the first session is still connecting; a
        # later call connects a second one. The first must not replace it.
        b = bridge.Bridge(proc_url, TOKEN, cooldown=0.2)
        try:
            b.call("memory_list_projects", {}, timeout=0.3)
        except bridge.BridgeUnavailable:
            pass
        else:
            raise AssertionError("the first call should have timed out")
        time.sleep(0.3)
        assert b.call("memory_list_projects", {})["projects"] is not None
        current = b._session
        b._loop.call_soon_threadsafe(gate.set)
        _wait_until(slow_owners[0].done, 5.0, "the late session lingered")
        assert b._session is current, "a late session replaced the current one"
        assert b.call("memory_list_projects", {})["projects"] is not None

        # An attempt that times out while connecting retires that half-open
        # session rather than leaving it to linger.
        slow_owners.clear()
        gate.clear()
        b = bridge.Bridge(proc_url, TOKEN, call_timeout=1.0)
        assert b.call("memory_list_projects", {}, timeout=5.0)["projects"] is not None
        _wait_until(slow_owners[0].done, 2.0, "a half-open session was left behind")
    finally:
        bridge.ClientSession = real
        b._loop.call_soon_threadsafe(gate.set)  # never leave a gated session waiting
    print("  OK")


def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.02)


def test_concurrent_callers_share_a_reconnect() -> None:
    print("== two callers on a dead session reconnect without tearing each other down ==")
    both_on_old = asyncio.Event()
    new_in_flight = asyncio.Event()
    late_reset_done = asyncio.Event()
    resets: list = []

    class Ordered(bridge.Bridge):
        """Forces the losing order: the second caller's failure on the old
        session is handled only after the first caller is mid-call on the new
        session."""

        old = None
        gated_new = False

        async def _reset_session(self, *args):
            resets.append(args)
            if len(resets) == 2:
                await new_in_flight.wait()
                await super()._reset_session(*args)
                late_reset_done.set()
            else:
                await super()._reset_session(*args)

        async def _ensure_session(self):
            session = await super()._ensure_session()
            if self.old is not None and session is not self.old and not self.gated_new:
                self.gated_new = True
                real_call = session.call_tool

                async def first_call_waits(*a, **k):
                    session.call_tool = real_call
                    new_in_flight.set()
                    await late_reset_done.wait()
                    return await real_call(*a, **k)

                session.call_tool = first_call_waits
            return session

    proc = start_server(PORT, TOKEN)
    try:
        b = Ordered(URL, TOKEN)
        assert b.call("memory_list_projects", {})["projects"] is not None
        old = b._session
        arrived: list = []
        real_old_call = old.call_tool

        async def wait_for_both(*a, **k):
            arrived.append(1)
            if len(arrived) == 2:
                both_on_old.set()
            await both_on_old.wait()
            return await real_old_call(*a, **k)

        old.call_tool = wait_for_both
        b.old = old
    finally:
        stop_server(proc)
    proc = start_server(PORT, TOKEN)
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(b.call, "memory_list_projects", {}) for _ in range(2)]
            for done in calls:
                assert done.result()["projects"] is not None
        assert len(resets) == 2, resets
        assert b.call("memory_list_projects", {})["projects"] is not None, "breaker opened"
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
    test_stalled_session_leaves_room_to_reconnect()
    proc = start_server(PORT, TOKEN)
    try:
        test_round_trip(URL)
        test_tool_error_is_not_an_outage(URL)
        test_wrong_token(URL)
        test_retired_owner_never_publishes(URL)
    finally:
        stop_server(proc)
    test_dead_server()
    test_restart_reconnects()
    test_concurrent_callers_share_a_reconnect()
    test_writes_survive_outage()
    print("\nHERMES BRIDGE TEST PASSED")


if __name__ == "__main__":
    main()
