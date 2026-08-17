"""Full MCP tool-coverage test — exercises every tool once via stdio.

Run with:
    uv run python -m tests.mcp_full_coverage
"""

import asyncio
import json
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import BIN, reset_project

PROJECT = "full-coverage-project"


def result_json(res) -> dict:
    return json.loads(res.content[0].text)


def result_text(res) -> str:
    return res.content[0].text


async def main() -> None:
    reset_project(PROJECT)

    async with AsyncExitStack() as stack:
        params = StdioServerParameters(command=BIN, args=[], cwd="/")
        read, write = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()

        print("== memory_store ==")
        store_res = await session.call_tool(
            "memory_store",
            {
                "project": PROJECT,
                "area": "backend",
                "module": "auth",
                "content": "Sessions expire after 24h and are stored in Redis.",
                "summary": "Sessions: 24h TTL in Redis",
                "importance": 4,
                "metadata": {"tag": "security"},
            },
        )
        point_id = result_text(store_res)
        assert point_id
        print(f"  stored {point_id[:8]}...")

        print("== memory_summarize_session ==")
        session_res = await session.call_tool(
            "memory_summarize_session",
            {
                "project": PROJECT,
                "area": "backend",
                "session_id": "s1",
                "observations": ["Investigated auth bug", "Fixed token refresh race"],
                "conclusion": "Token refresh race fixed by adding a mutex",
                "importance": 5,
            },
        )
        session_point_id = result_text(session_res)
        assert session_point_id
        print(f"  stored session summary {session_point_id[:8]}...")

        print("== memory_manifest ==")
        manifest_res = await session.call_tool("memory_manifest", {"project": PROJECT})
        manifest = result_json(manifest_res)
        print(f"  areas: {list(manifest['areas'].keys())}")
        assert "backend" in manifest["areas"]

        print("== memory_list_projects ==")
        list_res = await session.call_tool("memory_list_projects", {})
        projects = result_json(list_res)["projects"]
        print(f"  projects: {projects}")
        assert PROJECT in projects

        print("== memory_search ==")
        search_res = await session.call_tool(
            "memory_search",
            {"project": PROJECT, "query": "how long do sessions last?", "max_tokens": 2000},
        )
        search_result = result_json(search_res)
        print(f"  summaries: {len(search_result['summary'])}")
        assert search_result["summary"]
        # Must actually be the entry we stored, not just "something".
        assert search_result["summary"][0]["id"] == point_id
        assert "24h" in search_result["summary"][0]["summary"]

        print("== memory_get ==")
        get_res = await session.call_tool(
            "memory_get", {"project": PROJECT, "point_id": point_id}
        )
        got = result_json(get_res)
        assert got["found"] is True
        assert "Redis" in got["content"]
        assert got["metadata"]["tag"] == "security"
        print(f"  got entry, metadata tag = {got['metadata']['tag']}")

        print("== memory_get (nonexistent id) ==")
        import uuid

        missing_res = await session.call_tool(
            "memory_get", {"project": PROJECT, "point_id": str(uuid.uuid4())}
        )
        missing = result_json(missing_res)
        assert missing == {"found": False}
        print("  correctly returned {'found': False} for nonexistent id")

        print("== memory_stats ==")
        stats_res = await session.call_tool("memory_stats", {"project": PROJECT})
        stats = result_json(stats_res)
        print(f"  total_entries: {stats['total_entries']}")
        assert stats["total_entries"] == 2

        print("== memory_delete (existing) ==")
        del_res = await session.call_tool(
            "memory_delete", {"project": PROJECT, "point_id": point_id}
        )
        deleted = result_json(del_res)
        assert deleted is True
        print(f"  delete existing -> {deleted}")

        print("== memory_delete (already deleted) ==")
        del_res2 = await session.call_tool(
            "memory_delete", {"project": PROJECT, "point_id": point_id}
        )
        deleted2 = result_json(del_res2)
        assert deleted2 is False
        print(f"  delete again -> {deleted2}")

        print("== memory_stats after delete ==")
        stats_res2 = await session.call_tool("memory_stats", {"project": PROJECT})
        stats2 = result_json(stats_res2)
        assert stats2["total_entries"] == 1
        print(f"  total_entries: {stats2['total_entries']}")

        print("\nFULL MCP TOOL COVERAGE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())