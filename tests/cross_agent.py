"""Cross-agent memory sharing test.

Demonstrates the core value prop: an agent (e.g. Claude Code) stores memory via
the MCP server, then a *different* agent (e.g. Codex/Cursor) spawns the same MCP
server and retrieves it — including Thai text. Since all agents point at the
same venv binary and same Qdrant, memory persists across them.

Run with:
    uv run python -m tests.cross_agent
"""

import asyncio
import json
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import BIN, reset_project

PROJECT = "cross-agent-demo"


async def spawn(stack: AsyncExitStack) -> ClientSession:
    params = StdioServerParameters(command=BIN, args=[], cwd="/")
    read, write = await stack.enter_async_context(stdio_client(params))
    session = ClientSession(read, write)
    await stack.enter_async_context(session)
    await session.initialize()
    return session


async def main() -> None:
    reset_project(PROJECT)

    async with AsyncExitStack() as stack:
        # --- "Agent A" (e.g. Claude Code) writes memory ---
        a = await spawn(stack)
        store_res = await a.call_tool(
            "memory_store",
            {
                "project": PROJECT,
                "area": "backend",
                "module": "payment",
                "content": (
                    "Payment service uses PromptPay QR for THB and Stripe for "
                    "international cards. Commission split logic lives in the "
                    "checkout module. " * 6
                ),
                "summary": "Payment: PromptPay(THB) + Stripe(INTL), commission in checkout",
                "importance": 5,
            },
        )
        stored_id = store_res.content[0].text
        print(f"[Agent A/Claude] stored: {stored_id[:8]}...")

        # Store a Thai entry too.
        thai_store_res = await a.call_tool(
            "memory_store",
            {
                "project": PROJECT,
                "area": "infra",
                "module": "deploy",
                "content": (
                    "โปรเจกต์นี้ deploy ขึ้น ECS Fargate ผ่าน GitHub Actions "
                    "เมื่อ merge ไป main แล้วจะสร้าง docker image และรัน migration "
                    "อัตโนมัติ ก่อนจะสลับ traffic"
                ),
                "summary": "Infra: Deploy ขึ้น ECS Fargate ผ่าน GH Actions, มี auto-migration",
                "importance": 4,
            },
        )
        thai_id = thai_store_res.content[0].text
        print(f"[Agent A/Claude] stored Thai entry: {thai_id[:8]}...")

        # --- "Agent B" (e.g. Codex) spawns the SAME server and retrieves ---
        b = await spawn(stack)
        search_res = await b.call_tool(
            "memory_search",
            {"project": PROJECT, "query": "โปรเจกต์ deploy ยังไง", "max_tokens": 2000},
        )
        result = json.loads(search_res.content[0].text)
        print("\n[Agent B/Codex] searched 'โปรเจกต์ deploy ยังไง'")
        print(f"  summaries ({len(result['summary'])}):")
        for s in result["summary"]:
            print(f"    - {s['summary']}")
        assert result["summary"], "Agent B could not retrieve any memory written by Agent A"
        returned_ids = {s["id"] for s in result["summary"]}
        assert thai_id in returned_ids, (
            f"Agent B's search did not return the exact Thai entry Agent A "
            f"stored (found ids: {returned_ids})"
        )
        assert any(
            "deploy" in s["summary"].lower() or "Deploy" in s["summary"]
            for s in result["summary"]
        ), "Thai deploy memory not found"

        # Token budget respected.
        print(f"\n  budget={result['budget']} total_tokens={result['total_tokens']}")
        assert result["total_tokens"] <= result["budget"]

        print("\nCROSS-AGENT TEST PASSED — memory shared across agents")


if __name__ == "__main__":
    asyncio.run(main())