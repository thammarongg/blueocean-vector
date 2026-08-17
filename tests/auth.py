"""Verifies bearer-token auth on the streamable-http transport.

Checks that when BLUEOCEAN_AUTH_TOKEN / --auth-token is set:
  - requests with no token are rejected (401)
  - requests with the wrong token are rejected (401)
  - requests with the correct token succeed, via EITHER the standard
    `Authorization: Bearer` header OR a `?token=` query param (the latter
    exists because some MCP clients, e.g. Kiro's `mcp add --url`, have no
    way to set custom headers when registering a remote server by URL).

Run with:
    uv run python -m tests.auth
"""

import asyncio
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ._helpers import BIN, reset_project

PORT = 8798
TOKEN = "test-secret-token-12345"
WRONG_TOKEN = "wrong-token-99999"
PROJECT = "auth-test-project"


def wait_for_server(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{PORT}/mcp"
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"Server process exited early:\n{err}")
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json, text/event-stream"}
            )
            urllib.request.urlopen(req, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # any HTTP response (even 401) proves the port is bound
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("Server did not start listening in time")


def check_http_status(headers: dict, expect_status: int) -> int:
    """Raw HTTP request (no MCP session) just to check the auth status code."""
    url = f"http://127.0.0.1:{PORT}/mcp"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json, text/event-stream", **headers}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=3)
        status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == expect_status, f"expected {expect_status}, got {status}"
    return status


async def check_full_mcp_session(url: str, *, via: str, headers: dict | None = None) -> None:
    """A full MCP session (initialize + tool call) must succeed with valid auth,
    whether the token is supplied via header or via URL query param.
    """
    import httpx2

    async with AsyncExitStack() as stack:
        http_client = None
        if headers:
            http_client = httpx2.AsyncClient(headers=headers)
            await stack.enter_async_context(http_client)

        read, write = await stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        tools = await session.list_tools()
        assert any(t.name == "memory_store" for t in tools.tools)

        store_res = await session.call_tool(
            "memory_store",
            {
                "project": PROJECT,
                "area": "test",
                "module": "auth",
                "content": f"authenticated write via {via}",
                "summary": f"authenticated write via {via}",
                "importance": 3,
            },
        )
        assert store_res.content[0].text, f"authenticated store via {via} did not return an id"
        print(f"  full MCP session via {via}: OK")


def main() -> None:
    reset_project(PROJECT)

    proc = subprocess.Popen(
        [
            BIN,
            "--transport", "streamable-http",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--auth-token", TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_server(proc)
        print(f"Server (with auth) listening at http://127.0.0.1:{PORT}/mcp")

        print("== no token -> 401 ==")
        check_http_status({}, 401)

        print("== wrong token (header) -> 401 ==")
        check_http_status({"Authorization": f"Bearer {WRONG_TOKEN}"}, 401)

        print("== wrong token (query param) -> 401 ==")
        # query param variant checked via a separate raw request below
        url = f"http://127.0.0.1:{PORT}/mcp?token={WRONG_TOKEN}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json, text/event-stream"}
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            raise AssertionError("expected 401 for wrong query-param token")
        except urllib.error.HTTPError as e:
            assert e.code == 401

        print("== correct token via header -> full MCP session works ==")
        asyncio.run(
            check_full_mcp_session(
                f"http://127.0.0.1:{PORT}/mcp",
                via="Authorization header",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        )

        print("== correct token via query param -> full MCP session works ==")
        asyncio.run(
            check_full_mcp_session(
                f"http://127.0.0.1:{PORT}/mcp?token={TOKEN}",
                via="query param",
            )
        )

        print("\nAUTH TEST PASSED — unauthenticated/wrong-token requests rejected, "
              "correct token accepted")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()