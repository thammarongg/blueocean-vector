"""MCP tool definitions for the blueocean memory server."""

from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import DEFAULT_MAX_TOKENS, DEFAULT_TOP_K
from .token_budget import allocate
from .vector_store import VectorStore


def register_tools(mcp: MCPServer, store: VectorStore) -> None:
    def memory_store(
        project: str,
        area: str,
        module: str,
        content: str,
        summary: str,
        importance: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Explicitly persist a memory entry for a project.

        ``content`` is the full detail; ``summary`` is a short condensed form
        that agents see first to save tokens. ``importance`` is 1-5 (5 =
        critical decision/architecture). ``area``/``module`` scope the entry
        within the project.
        """
        return store.store(
            project=project,
            area=area,
            module=module,
            content=content,
            summary=summary,
            importance=importance,
            metadata=metadata,
        )

    def memory_search(
        project: str,
        query: str,
        area: str | None = None,
        module: str | None = None,
        top_k: int = DEFAULT_TOP_K,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        time_range_start: int | None = None,
        time_range_end: int | None = None,
        importance_min: int | None = None,
    ) -> dict:
        """Semantically search a project's memory.

        Returns a token-budgeted result: ``summary`` layer (condensed) plus
        ``full`` layer (full content of the top matches that fit within
        ``max_tokens``). Scope with ``area``/``module``/``time_range``/
        ``importance_min`` to keep large project collections fast and cheap.
        """
        time_range = None
        if time_range_start is not None or time_range_end is not None:
            time_range = (
                time_range_start if time_range_start is not None else 0,
                time_range_end if time_range_end is not None else 2**63 - 1,
            )
        candidates = store.search(
            project,
            query,
            top_k=top_k,
            area=area,
            module=module,
            time_range=time_range,
            importance_min=importance_min,
        )
        return allocate(candidates, max_tokens).to_dict()

    def memory_get(project: str, point_id: str) -> dict:
        """Fetch the full content of a single memory entry by ID.

        Returns ``{"found": False}`` if no entry exists with that ID.
        """
        entry = store.get(project, point_id)
        if entry is None:
            return {"found": False}
        return {"found": True, **entry}

    def memory_delete(project: str, point_id: str) -> bool:
        """Delete a single memory entry by ID."""
        return store.delete(project, point_id)

    def memory_list_projects() -> dict:
        """List all projects that have a memory collection."""
        return {"projects": store.list_projects()}

    def memory_manifest(project: str) -> dict:
        """Show the areas/modules present in a project's memory.

        Use this to discover valid ``area``/``module`` scopes before searching,
        so queries target the relevant slice of a large project.
        """
        return store.manifest(project)

    def memory_summarize_session(
        project: str,
        area: str,
        session_id: str,
        observations: list[str],
        conclusion: str,
        importance: int = 3,
    ) -> str:
        """Store a condensed summary of a work session for later retrieval.

        ``observations`` are short bullet facts; ``conclusion`` is the outcome.
        Combined into one entry so a new agent can quickly pick up context
        without re-summarizing.
        """
        content = (
            f"Session {session_id} observations:\n"
            + "\n".join(f"- {o}" for o in observations)
            + f"\n\nConclusion: {conclusion}"
        )
        summary = f"[{area} session {session_id}] {conclusion}"
        return store.store(
            project=project,
            area=area,
            module=session_id,
            content=content,
            summary=summary,
            importance=importance,
            metadata={"kind": "session_summary", "session_id": session_id},
        )

    def memory_stats(project: str) -> dict:
        """Admin: collection stats (count, importance/area distribution, size)."""
        return store.stats(project)

    def memory_usage(days: int = 7, view: str | None = None) -> dict:
        """Report how memory has been used recently: call counts, latency,
        search quality, embedding cost, and entries never retrieved.

        Defaults to a compact summary over the last 7 days. Pass ``view`` as
        ``"unused"``, ``"tools"`` or ``"errors"`` for one drill-down list.
        Returns ``{"enabled": False}`` when telemetry is switched off.
        """
        from .telemetry import db as telemetry_db
        from .telemetry import is_enabled
        from .telemetry.queries import build_usage_summary

        if not is_enabled():
            return {"enabled": False, "reason": "BLUEOCEAN_TELEMETRY=0"}
        conn = telemetry_db.connect()
        try:
            return build_usage_summary(conn, days=days, view=view)
        finally:
            conn.close()

    from .telemetry.instrument import INSTRUMENT_DENYLIST, instrument

    registrations = [
        (memory_store, "memory_store",
         "Persist a memory entry for a project (content + condensed summary + importance + area/module)."),
        (memory_search, "memory_search",
         "Semantic search a project's memory with token-budgeted return (summary + full layers)."),
        (memory_get, "memory_get",
         "Fetch the full content of a single memory entry by ID."),
        (memory_delete, "memory_delete",
         "Delete a single memory entry by ID."),
        (memory_list_projects, "memory_list_projects",
         "List all projects that have a memory collection."),
        (memory_manifest, "memory_manifest",
         "Show the areas/modules present in a project's memory."),
        (memory_summarize_session, "memory_summarize_session",
         "Store a condensed summary of a work session for later retrieval."),
        (memory_stats, "memory_stats",
         "Admin: collection stats (count, importance/area distribution, size)."),
        (memory_usage, "memory_usage",
         "Report recent memory usage: calls, latency, search quality, cost, unused entries."),
    ]

    # Instrumenting here, in one loop over the registration list, rather than
    # decorating each function: a tool added later cannot silently vanish from
    # the stats by someone forgetting a decorator.
    for fn, name, description in registrations:
        tool = fn if name in INSTRUMENT_DENYLIST else instrument(fn, name)
        mcp.add_tool(tool, name=name, description=description)
