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
import subprocess
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from blueocean_mcp import admin
from blueocean_mcp.config import DEFAULT_QDRANT_URL
from blueocean_mcp.vector_store import collection_name

from ._helpers import BIN, REPO_ROOT, reset_project

PROJECT = "trace-kinds-project"
ADMIN = str(REPO_ROOT / ".venv" / "bin" / "blueocean-admin")
DEAD_PORT = 8799  # nothing listens here: an audit row from a refused prune goes nowhere
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

        print("== a new collection indexes kind (every default search filters on it) ==")
        schema = QdrantClient(url=DEFAULT_QDRANT_URL).get_collection(collection_name(PROJECT)).payload_schema
        assert "kind" in schema, f"no payload index on kind: {sorted(schema)}"
        assert schema["kind"].data_type == qmodels.PayloadSchemaType.KEYWORD, schema["kind"]
        print("  OK")

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


def count_kind(kind: str | None) -> int:
    """Points of one kind (None = points with no kind key at all)."""
    client = QdrantClient(url=DEFAULT_QDRANT_URL)
    if kind is None:
        flt = qmodels.Filter(must=[qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="kind"))])
    else:
        flt = qmodels.Filter(must=[qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind))])
    return client.count(collection_name(PROJECT), count_filter=flt, exact=True).count


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def blank_kind_is_refused() -> None:
    """`--kind "$K"` with K unset must not widen a prune to every kind."""
    print("== prune --kind '' is refused, by the CLI and by cmd_prune ==")
    before = {k: count_kind(k) for k in (None, "hermes_turn", "session_summary", "custom_trace")}
    env = {**os.environ, "BLUEOCEAN_SERVER_URL": f"http://127.0.0.1:{DEAD_PORT}"}
    for blank in ("", "   "):
        res = subprocess.run([ADMIN, "prune", PROJECT, "--kind", blank, "--max-importance", "5"],
                             capture_output=True, text=True, env=env, timeout=120, check=False)
        assert res.returncode == 2, f"blank --kind {blank!r} was not refused: rc={res.returncode}\n{res.stdout}{res.stderr}"
        assert "--kind" in res.stderr, res.stderr

    original = admin._report_audit
    admin._report_audit = lambda row: True
    try:
        for blank in ("", "  "):
            try:
                admin.cmd_prune(_Args(project=PROJECT, older_days=30, max_importance=5,
                                      kind=blank, dry_run=False))
            except ValueError as exc:
                assert "kind" in str(exc), exc
            else:
                raise AssertionError(f"cmd_prune accepted a blank kind {blank!r}")
    finally:
        admin._report_audit = original

    after = {k: count_kind(k) for k in before}
    assert after == before, f"a refused prune deleted entries: {before} -> {after}"
    print("  refused, nothing deleted")


def prune_by_kind() -> None:
    """--kind removes only that kind, and works without any other filter."""
    print("== prune --kind hermes_turn removes traces only ==")
    before_plain = count_kind(None)
    assert count_kind("hermes_turn") == 1 and before_plain >= 1

    # The audit row goes to a telemetry server this test does not run; its
    # failure path is covered in tests/telemetry_cli.py.
    original = admin._report_audit
    admin._report_audit = lambda row: True
    try:
        admin.cmd_prune(_Args(project=PROJECT, older_days=None, max_importance=None,
                              kind="hermes_turn", dry_run=False))
    finally:
        admin._report_audit = original

    assert count_kind("hermes_turn") == 0, "the trace survived prune --kind"
    assert count_kind(None) == before_plain, "prune --kind deleted unkinded entries"
    assert count_kind("session_summary") == 1, "prune --kind deleted another kind"
    assert count_kind("custom_trace") == 1, "prune --kind deleted another kind"
    print("  traces gone, everything else intact")


async def main() -> None:
    reset_project(PROJECT)
    seeded = await default_kinds()
    await overridden_kinds(seeded)
    blank_kind_is_refused()
    prune_by_kind()
    print("\nTRACE KINDS TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
