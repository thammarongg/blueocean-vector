"""Trace kinds stay out of every search that does not ask for them.

A Turn Trace is written every Hermes turn. Claude Code, Codex and Cursor call
memory_search without exclude_kinds, so if the exclusion were left to the
caller, traces would crowd the real entries out of their token budget. The
server therefore excludes the kinds in BLUEOCEAN_TRACE_KINDS whenever the
caller does not name a kind, and still returns them when it does.

Run with:
    uv run python -m tests.trace_kinds
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import BIN, reset_project

PROJECT = "trace-kinds-project"
QUERY = {"project": PROJECT, "query": "which cache did we pick, redis or memcached"}


def result_json(res) -> dict:
    return json.loads(res.content[0].text)


def ids(result: dict) -> set[str]:
    return {row["id"] for row in result["summary"]}


async def open_session(stack: AsyncExitStack, trace_kinds: str | None) -> ClientSession:
    env = dict(os.environ)
    env.pop("BLUEOCEAN_TRACE_KINDS", None)
    if trace_kinds is not None:
        env["BLUEOCEAN_TRACE_KINDS"] = trace_kinds
    params = StdioServerParameters(command=BIN, args=[], cwd="/", env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = ClientSession(read, write)
    await stack.enter_async_context(session)
    await session.initialize()
    return session


async def store(session: ClientSession, summary: str, kind: str | None) -> str:
    args = {
        "project": PROJECT,
        "area": "backend",
        "module": "cache",
        "content": f"{summary}. We compared redis and memcached for the cache.",
        "summary": summary,
        "importance": 3,
    }
    if kind is not None:
        args["metadata"] = {"kind": kind}
    res = await session.call_tool("memory_store", args)
    return res.content[0].text


async def search(session: ClientSession, **extra) -> set[str]:
    return ids(result_json(await session.call_tool("memory_search", {**QUERY, **extra})))


async def default_kinds() -> dict[str, str]:
    """With the default setting, hermes_turn is a trace kind."""
    async with AsyncExitStack() as stack:
        session = await open_session(stack, None)
        seeded = {
            "plain": await store(session, "Chose redis for the cache", None),
            "summary": await store(session, "Session settled on redis", "session_summary"),
            "trace": await store(session, "User asked about redis -> picked it", "hermes_turn"),
        }

        print("== default search hides the trace, keeps everything else ==")
        found = await search(session)
        assert seeded["trace"] not in found, "a hermes_turn trace leaked into default search"
        assert seeded["plain"] in found, "an entry without a kind was dropped"
        assert seeded["summary"] in found, "a session summary was dropped"
        print(f"  {len(found)} entries, no trace")

        print("== kind='hermes_turn' still reaches the trace ==")
        only = await search(session, kind="hermes_turn")
        assert only == {seeded["trace"]}, f"expected only the trace, got {only}"
        print("  only the trace came back")

        print("== caller exclusions compose with trace exclusion ==")
        found = await search(session, exclude_kinds=["session_summary"])
        assert found == {seeded["plain"]}, f"expected only the plain entry, got {found}"
        print("  plain entry only")
        return seeded


async def overridden_kinds(seeded: dict[str, str]) -> None:
    """BLUEOCEAN_TRACE_KINDS replaces the default list."""
    async with AsyncExitStack() as stack:
        session = await open_session(stack, "custom_trace")
        custom = await store(session, "Custom trace about the redis cache", "custom_trace")

        print("== an overridden list hides its own kinds instead ==")
        found = await search(session)
        assert custom not in found, "custom_trace should be hidden when configured"
        assert seeded["trace"] in found, "hermes_turn is no longer a trace kind here"
        print("  custom_trace hidden, hermes_turn visible")

    async with AsyncExitStack() as stack:
        session = await open_session(stack, "")
        print("== an empty list disables trace exclusion ==")
        found = await search(session)
        assert seeded["trace"] in found and custom in found
        print("  everything visible")


async def main() -> None:
    reset_project(PROJECT)
    seeded = await default_kinds()
    await overridden_kinds(seeded)
    print("\nTRACE KINDS TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
