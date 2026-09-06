"""Kind-filtered search, and session summaries that don't mint a Module.

Two decisions from the "End-of-session memory reliability" map, both about
keeping the memory store legible as the number of session-level records grows:

1. ``memory_search`` must be able to select or exclude entries by ``kind``.
   Without it there is no way to keep a class of record out of ordinary
   semantic search, so any always-written record competes for the token
   budget with the entries an agent actually asked for.

2. ``memory_summarize_session`` must not mint a Module per session. It used
   to pass ``module=session_id``, which is why one project's ``planning``
   area accumulated eleven one-off ``task_<hex>`` modules that say nothing
   about what they contain. The session id belongs in metadata, where it
   already is, not in the manifest.

Run with:
    uv run python -m tests.memory_kind
"""

import asyncio
import json
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import BIN, reset_project

PROJECT = "kind-filter-project"
SESSION_ID = "2026-09-06-alpha"


def result_json(res) -> dict:
    return json.loads(res.content[0].text)


def result_text(res) -> str:
    return res.content[0].text


def ids(result: dict) -> set[str]:
    """Every point id the search returned, across both layers."""
    return {row["id"] for row in result["summary"]}


async def main() -> None:
    reset_project(PROJECT)

    async with AsyncExitStack() as stack:
        params = StdioServerParameters(command=BIN, args=[], cwd="/")
        read, write = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()

        print("== seed: one ordinary entry, one session summary ==")
        ordinary_id = result_text(
            await session.call_tool(
                "memory_store",
                {
                    "project": PROJECT,
                    "area": "backend",
                    "module": "auth",
                    "content": "Token refresh uses a mutex to avoid a race.",
                    "summary": "Token refresh guarded by a mutex",
                    "importance": 4,
                },
            )
        )
        summary_id = result_text(
            await session.call_tool(
                "memory_summarize_session",
                {
                    "project": PROJECT,
                    "area": "backend",
                    "session_id": SESSION_ID,
                    "observations": ["Looked at the token refresh race"],
                    "conclusion": "Token refresh race fixed by adding a mutex",
                    "importance": 4,
                },
            )
        )
        assert ordinary_id and summary_id
        print(f"  ordinary={ordinary_id[:8]} summary={summary_id[:8]}")

        query = {"project": PROJECT, "query": "token refresh mutex"}

        print("== default search still returns both kinds ==")
        both = ids(result_json(await session.call_tool("memory_search", query)))
        assert ordinary_id in both, "default search dropped the ordinary entry"
        assert summary_id in both, "default search dropped the session summary"
        print(f"  {len(both)} entries, both kinds present")

        print("== kind= selects only that kind ==")
        only_summaries = ids(
            result_json(
                await session.call_tool(
                    "memory_search", {**query, "kind": "session_summary"}
                )
            )
        )
        assert only_summaries == {summary_id}, (
            f"kind='session_summary' should return only {summary_id[:8]}, "
            f"got {sorted(i[:8] for i in only_summaries)}"
        )
        print("  only the session summary came back")

        print("== exclude_kinds= omits that kind ==")
        without_summaries = ids(
            result_json(
                await session.call_tool(
                    "memory_search", {**query, "exclude_kinds": ["session_summary"]}
                )
            )
        )
        assert without_summaries == {ordinary_id}, (
            f"exclude_kinds=['session_summary'] should return only "
            f"{ordinary_id[:8]}, got {sorted(i[:8] for i in without_summaries)}"
        )
        print("  the session summary was excluded, the ordinary entry kept")

        print("== an entry with no kind survives exclusion ==")
        # Entries stored before `kind` existed carry no such payload key at
        # all. Excluding a kind must not silently drop them.
        assert ordinary_id in without_summaries
        print("  unkinded entry not dropped by exclude_kinds")

        print("== summarize_session does not mint a Module ==")
        manifest = result_json(
            await session.call_tool("memory_manifest", {"project": PROJECT})
        )
        modules = manifest["areas"]["backend"]["modules"]
        assert SESSION_ID not in modules, (
            f"session id leaked into the manifest as a Module: {modules}"
        )
        print(f"  backend modules: {modules}")

        print("== the session id is still recorded, in metadata ==")
        entry = result_json(
            await session.call_tool(
                "memory_get", {"project": PROJECT, "point_id": summary_id}
            )
        )
        assert entry["found"]
        assert entry["metadata"]["session_id"] == SESSION_ID, (
            f"session_id missing from metadata: {entry['metadata']}"
        )
        assert entry["metadata"]["kind"] == "session_summary"
        print(f"  metadata carries session_id={SESSION_ID}")

    print("\nAll kind-filter and module-minting checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
