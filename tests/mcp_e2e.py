"""End-to-end test of the actual MCP server over stdio.

Spawns the blueocean-mcp server and talks to it over the MCP stdio transport:
lists tools, stores a memory, searches it, and checks token-budgeted output.

Run with:
    uv run python -m tests.mcp_e2e
"""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import REPO_ROOT, reset_project

PROJECT = "mcp-e2e-project"


async def main() -> None:
    reset_project(PROJECT)

    server_params = StdioServerParameters(
        command="uv",
        args=["run", "blueocean-mcp"],
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("== tools ==")
            print("  ", names)
            assert "memory_store" in names
            assert "memory_search" in names
            assert "memory_manifest" in names

            print("== memory_store ==")
            store_res = await session.call_tool(
                "memory_store",
                {
                    "project": PROJECT,
                    "area": "backend",
                    "module": "api",
                    "content": (
                        "REST API versioned with /v1, rate limited at 100 req/min "
                        "per token. All responses are JSON. " * 5
                    ),
                    "summary": "API: /v1, rate limit 100/min, JSON",
                    "importance": 4,
                },
            )
            point_id = store_res.content[0].text
            print(f"  stored {point_id[:8]}...")
            assert point_id

            print("== memory_search (budgeted) ==")
            search_res = await session.call_tool(
                "memory_search",
                {
                    "project": PROJECT,
                    "query": "what is the rate limit?",
                    "area": "backend",
                    "max_tokens": 2000,
                },
            )
            import json

            result = json.loads(search_res.content[0].text)
            print("  budget:", result["budget"], "total_tokens:", result["total_tokens"])
            assert result["summary"], "expected at least one summary"
            assert result["total_tokens"] <= result["budget"]
            # Not just "something came back" -- the specific entry we stored,
            # identified by its id, must be the one found.
            assert result["summary"][0]["id"] == point_id, (
                "search did not return the entry we just stored as the top hit"
            )
            assert "rate limit" in result["summary"][0]["summary"].lower()

            print("\nMCP E2E TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())