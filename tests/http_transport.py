"""Proves the MCP server works over a real network URL (streamable-http),
not just local stdio process-spawn. This is what lets ANY MCP client that
supports "add server by URL" (Claude Code, Cursor, Codex, Gemini/Antigravity,
Kiro, and unknown/future tools) register blueocean without us writing any
tool-specific config-file-editing code.

Starts the server as a real subprocess bound to a TCP port, then connects
two independent HTTP client sessions (simulating two different agents/tools)
against that one shared, already-running server -- mirroring how a URL-based
registration is actually used in practice (unlike stdio, where each tool
spawns its own separate process).

Run with:
    uv run python -m tests.http_transport
"""

import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ._helpers import BIN, reset_project

PORT = 8799
URL = f"http://127.0.0.1:{PORT}/mcp"
PROJECT = "http-transport-project"


def wait_for_server(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    """Poll the port until the server responds, or the process dies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"Server process exited early:\n{err}")
        try:
            req = urllib.request.Request(
                URL, headers={"Accept": "application/json, text/event-stream"}
            )
            urllib.request.urlopen(req, timeout=1)
            return  # got *some* HTTP response -> server is listening
        except urllib.error.HTTPError:
            return  # non-2xx is fine -- proves the port is bound & serving
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("Server did not start listening in time")


async def connect(stack: AsyncExitStack) -> ClientSession:
    read, write = await stack.enter_async_context(streamable_http_client(URL))
    session = ClientSession(read, write)
    await stack.enter_async_context(session)
    await session.initialize()
    return session


def result_json(res) -> dict:
    return json.loads(res.content[0].text)


async def run_over_http() -> None:
    async with AsyncExitStack() as stack:
        # "Tool A" connects over the network URL and writes memory.
        a = await connect(stack)
        tools = await a.list_tools()
        tool_names = sorted(t.name for t in tools.tools)
        print(f"[Tool A] connected over HTTP, sees tools: {tool_names}")
        assert "memory_store" in tool_names, "server did not expose tools over HTTP"

        store_res = await a.call_tool(
            "memory_store",
            {
                "project": PROJECT,
                "area": "infra",
                "module": "url-registration",
                "content": (
                    "This entry was written over the streamable-http MCP "
                    "transport, proving the server can be registered purely "
                    "by URL in any MCP-http-capable tool (Claude Code, Cursor, "
                    "Codex, Gemini/Antigravity, Kiro) without editing that "
                    "tool's config file format in our own code."
                ),
                "summary": "HTTP transport works: URL-based registration verified",
                "importance": 5,
            },
        )
        point_id = store_res.content[0].text
        assert point_id, "memory_store over HTTP returned no id"
        print(f"[Tool A] stored over HTTP: {point_id[:8]}...")

        # "Tool B" opens a SEPARATE HTTP connection to the SAME running
        # server (no new process spawned -- this is the point of URL-based
        # registration: one shared server, many independent client sessions).
        b = await connect(stack)
        search_res = await b.call_tool(
            "memory_search",
            {
                "project": PROJECT,
                "query": "how do other tools register this server?",
                "max_tokens": 2000,
            },
        )
        result = result_json(search_res)
        print("[Tool B] searched over a SEPARATE HTTP connection")
        print(f"  summaries: {len(result['summary'])}")
        assert result["summary"], "Tool B could not read Tool A's memory over HTTP"
        assert result["summary"][0]["id"] == point_id, (
            "Tool B's search did not return the exact entry Tool A stored"
        )
        assert any(
            "url" in s["summary"].lower() for s in result["summary"]
        ), "expected the stored entry to be found by Tool B"

        print("\nHTTP TRANSPORT TEST PASSED — server is reachable and functional via URL")


def main() -> None:
    reset_project(PROJECT)

    proc = subprocess.Popen(
        [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_server(proc)
        print(f"Server listening at {URL}")
        asyncio.run(run_over_http())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()