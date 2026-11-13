"""Session identity without the protocol's help.

The 2026-07-28 MCP spec removed protocol sessions and the Mcp-Session-Id
header. Telemetry used to key a Session on that header and fall back to one
per-process id, so every modern client on the shared HTTP server collapsed
into a single session. These tests pin the replacement: the server derives a
session from (agent name, agent version, caller, project), renews it after an
idle gap, and records where each id came from in `session_source`.

The unit half drives SessionTracker with a fake clock. The end-to-end half
starts a real server and talks to it with a real 2026-07-28 client and a real
handshake-era client.

Run with:
    uv run python -m tests.session_identity
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

from blueocean_mcp.telemetry.sessions import SessionTracker, caller_fingerprint

from ._helpers import BIN, reset_project

PORT = 8792
URL = f"http://127.0.0.1:{PORT}/mcp"
PROJECT = "session-identity-project"
OTHER_PROJECT = "session-identity-other"
IDLE_SECONDS = 2


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def key(agent: str = "a", project: str = "p", caller: str = "addr:127.0.0.1") -> tuple:
    return (agent, "1", caller, project)


# ------------------------------------------------------------------ unit ----

def test_same_key_same_session() -> None:
    print("== one key, two calls, one session ==")
    t = SessionTracker(idle_seconds=60, clock=FakeClock())
    assert t.session_for(key()) == t.session_for(key())
    print("  OK")


def test_idle_gap_renews() -> None:
    print("== a call after the idle gap starts a new session ==")
    clock = FakeClock()
    t = SessionTracker(idle_seconds=60, clock=clock)
    first = t.session_for(key())
    clock.now += 59
    assert t.session_for(key()) == first, "within the gap must keep the session"
    clock.now += 61
    assert t.session_for(key()) != first, "past the gap must renew the session"
    print("  OK")


def test_keys_are_independent() -> None:
    print("== different agent, project or caller -> different sessions ==")
    t = SessionTracker(idle_seconds=60, clock=FakeClock())
    base = t.session_for(key())
    others = {
        t.session_for(key(agent="b")),
        t.session_for(key(project="q")),
        t.session_for(key(caller="addr:10.0.0.9")),
    }
    assert base not in others and len(others) == 3, (base, others)
    print("  OK")


def test_eviction_drops_least_recent() -> None:
    print("== at the bound, the least recently seen key is dropped ==")
    t = SessionTracker(idle_seconds=60, max_keys=2, clock=FakeClock())
    a = t.session_for(key(agent="A"))
    b = t.session_for(key(agent="B"))
    assert t.session_for(key(agent="A")) == a
    t.session_for(key(agent="C"))  # evicts B, the least recently seen
    assert t.session_for(key(agent="A")) == a, "A was touched recently and must survive"
    assert t.session_for(key(agent="B")) != b, "B was evicted and must get a new id"
    assert len(t) <= 2
    print("  OK")


def test_concurrent_calls() -> None:
    print("== many threads at once: no exception, bound respected ==")
    t = SessionTracker(idle_seconds=60, max_keys=40)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(500):
                t.session_for(key(agent=str((n * 7 + i) % 50)))
        except BaseException as exc:  # noqa: BLE001 - the test is that nothing raises
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors
    assert len(t) <= 40, len(t)
    print("  OK")


def test_fingerprint() -> None:
    print("== header and query-param token fingerprint alike, never verbatim ==")
    by_header = caller_fingerprint({"authorization": "Bearer t1-secret-value"}, None, "127.0.0.1")
    by_query = caller_fingerprint({}, "t1-secret-value", "127.0.0.1")
    assert by_header == by_query, (by_header, by_query)
    assert "t1-secret-value" not in by_header
    assert caller_fingerprint({}, None, "127.0.0.1") == "addr:127.0.0.1"
    assert caller_fingerprint(None, None, None) == "unknown"
    assert caller_fingerprint({"authorization": "Bearer other"}, None, "127.0.0.1") != by_header
    # The server has one shared token, so the token alone says nothing about
    # who is calling: with auth on, two machines must still be told apart.
    elsewhere = caller_fingerprint({"authorization": "Bearer t1-secret-value"}, None, "10.0.0.9")
    assert elsewhere != by_header, "a shared token must not merge callers on different machines"
    print("  OK")


# ------------------------------------------------------------ end to end ----

def wait_for_server(proc: subprocess.Popen, timeout: float = 30.0, url: str = URL) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"Server process exited early:\n{err}")
        try:
            urllib.request.urlopen(
                urllib.request.Request(url, headers={"Accept": "application/json, text/event-stream"}),
                timeout=1,
            )
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("Server did not start listening in time")


def read_rows(db_file: Path, expect: int, timeout: float = 10.0) -> list[tuple]:
    """The server's writer thread batches; poll until the rows land."""
    deadline = time.time() + timeout
    rows: list[tuple] = []
    while time.time() < deadline:
        try:
            with sqlite3.connect(db_file) as reader:
                rows = reader.execute(
                    "SELECT agent_name, session_id, session_source, project, tool "
                    "FROM events WHERE tool != 'memory_usage' ORDER BY id"
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if len(rows) >= expect:
            return rows
        time.sleep(0.3)
    raise AssertionError(f"expected {expect} telemetry rows, got {len(rows)}: {rows}")


async def modern_calls(name: str, project: str, n: int) -> str | None:
    """A 2026-07-28 client. mode="auto" probes server/discover (and so
    receives the instructions); a client pinned with mode="2026-07-28" skips
    the probe and never sees them, which the spec allows -- the reason the
    save-as-you-go guidance also lives in the tool descriptions."""
    from mcp.client import Client
    from mcp.types import Implementation

    async with Client(URL, mode="auto", client_info=Implementation(name=name, version="1")) as c:
        assert c.protocol_version == "2026-07-28", c.protocol_version
        for _ in range(n):
            res = await c.call_tool("memory_search", {"project": project, "query": "anything"})
            assert not res.is_error, res
        return c.instructions


async def legacy_call(project: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(streamable_http_client(URL))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        res = await session.call_tool("memory_search", {"project": project, "query": "anything"})
        assert not res.is_error, res


def test_end_to_end() -> None:
    print("== real server: modern clients get derived sessions, legacy keeps its header ==")
    reset_project(PROJECT)
    reset_project(OTHER_PROJECT)
    with tempfile.TemporaryDirectory() as d:
        db_file = Path(d) / "telemetry.db"
        env = {
            **os.environ,
            "BLUEOCEAN_TELEMETRY_DB": str(db_file),
            "BLUEOCEAN_SESSION_IDLE_SECONDS": str(IDLE_SECONDS),
        }
        proc = subprocess.Popen(
            [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(PORT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_server(proc)
            instructions = asyncio.run(modern_calls("modern-a", PROJECT, 2))
            asyncio.run(modern_calls("modern-b", PROJECT, 1))
            asyncio.run(modern_calls("modern-a", OTHER_PROJECT, 1))
            time.sleep(IDLE_SECONDS + 1)
            asyncio.run(modern_calls("modern-a", PROJECT, 1))
            asyncio.run(legacy_call(PROJECT))
            rows = read_rows(db_file, expect=6)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    print("  rows:", json.dumps(rows, indent=None))
    a1, a2, b, a_other, a_later, legacy = rows[:6]

    assert instructions, "a 2026-07-28 client must still receive the server instructions"
    assert a1[0] == "modern-a", f"agent name lost on the modern path: {a1}"
    for row in (a1, a2, b, a_other, a_later):
        assert row[2] == "derived", f"modern client must get a derived session: {row}"
        assert row[1], f"derived session id must not be empty: {row}"
    assert a1[1] == a2[1], "two calls from one agent on one project must share a session"
    assert b[1] != a1[1], "a different agent must get a different session"
    assert a_other[1] != a1[1], "the same agent on another project is another session"
    assert a_later[1] != a1[1], "a call after the idle gap must start a new session"
    assert legacy[2] == "header", f"handshake-era client must keep its header id: {legacy}"
    print("  OK")


AUTH_PORT = 8791
AUTH_URL = f"http://127.0.0.1:{AUTH_PORT}/mcp"
TOKEN = "session-identity-token-7f3a9c2e51"


def raw_call(url: str, headers: dict, client_info: dict | None) -> int:
    """A bare 2026-07-28 tools/call, so the client info can be left out
    entirely -- the SDK's Client always fills in a default name."""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if client_info:
        meta["io.modelcontextprotocol/clientInfo"] = client_info
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "memory_list_projects", "arguments": {}, "_meta": meta},
    }
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": "tools/call",
        "mcp-name": "memory_list_projects",
        **headers,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
        return resp.status


def test_authenticated_end_to_end() -> None:
    print("== auth on: header and ?token= share a session, anonymous still records, token never stored ==")
    with tempfile.TemporaryDirectory() as d:
        db_file = Path(d) / "telemetry.db"
        err_file = Path(d) / "stderr.log"
        env = {**os.environ, "BLUEOCEAN_AUTH_TOKEN": TOKEN, "BLUEOCEAN_TELEMETRY_DB": str(db_file)}
        with open(err_file, "w") as err:
            proc = subprocess.Popen(
                [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(AUTH_PORT)],
                env=env, stdout=subprocess.DEVNULL, stderr=err, text=True,
            )
            try:
                wait_for_server(proc, url=AUTH_URL)
                agent = {"name": "kiro-like", "version": "1"}
                assert raw_call(AUTH_URL, {"authorization": f"Bearer {TOKEN}"}, agent) == 200
                assert raw_call(f"{AUTH_URL}?token={TOKEN}", {}, agent) == 200
                assert raw_call(AUTH_URL, {"authorization": f"Bearer {TOKEN}"}, None) == 200
                deadline = time.time() + 10
                rows: list = []
                while time.time() < deadline and len(rows) < 3:
                    try:
                        with sqlite3.connect(db_file) as reader:
                            rows = reader.execute(
                                "SELECT agent_name, session_id, session_source FROM events ORDER BY id"
                            ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                    time.sleep(0.3)
                with sqlite3.connect(db_file) as reader:
                    tables = [r[0] for r in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    stored = "\n".join(
                        str(v) for t in tables for row in reader.execute(f"SELECT * FROM {t}") for v in row
                    )
            finally:
                proc.terminate()
                proc.wait(timeout=10)
        stderr = err_file.read_text()

    print("  rows:", rows)
    assert len(rows) >= 3, rows
    by_header, by_query, anonymous = rows[:3]
    assert by_header[2] == by_query[2] == "derived", rows
    assert by_header[1] == by_query[1], "the same caller by header and by ?token= must share a session"
    assert anonymous[2] == "derived" and anonymous[1], f"a client with no info must still record: {anonymous}"
    assert anonymous[1] != by_header[1], "an anonymous client is not the named agent's session"
    assert TOKEN not in stored, "the bearer token reached the telemetry database"
    assert TOKEN not in stderr, "the bearer token reached the server log"
    print("  OK")


def test_stdio_end_to_end() -> None:
    print("== real stdio server: calls land on the per-process id ==")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    reset_project(PROJECT)
    with tempfile.TemporaryDirectory() as d:
        db_file = Path(d) / "telemetry.db"
        params = StdioServerParameters(
            command=BIN,
            args=["--transport", "stdio"],
            env={**os.environ, "BLUEOCEAN_TELEMETRY_DB": str(db_file)},
        )

        async def run() -> None:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
                await s.initialize()
                for _ in range(2):
                    await s.call_tool("memory_search", {"project": PROJECT, "query": "x"})

        asyncio.run(run())
        rows = read_rows(db_file, expect=2)
    assert all(r[2] == "process" for r in rows), rows
    assert rows[0][1] and rows[0][1] == rows[1][1], rows
    print("  OK")


if __name__ == "__main__":
    test_same_key_same_session()
    test_idle_gap_renews()
    test_keys_are_independent()
    test_eviction_drops_least_recent()
    test_concurrent_calls()
    test_fingerprint()
    test_end_to_end()
    test_authenticated_end_to_end()
    test_stdio_end_to_end()
    print("\nSESSION IDENTITY TEST PASSED")
