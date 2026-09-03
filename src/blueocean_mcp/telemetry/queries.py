"""Aggregations over the event log.

Percentiles are computed in Python rather than SQL: SQLite has no percentile
function, and the row count is bounded by retention (a heavy 1,000-call day is
90,000 rows at 90 days), which is small enough to sort in memory on a local
dashboard.
"""

import sqlite3
import time
from typing import Any


def _window_start(days: int, now: int | None = None) -> int:
    return (now if now is not None else int(time.time())) - days * 86400


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[index]


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def build_stats(
    conn: sqlite3.Connection,
    days: int,
    tz_offset_minutes: int = 0,
    project: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    since = _window_start(days, now)
    scope = "AND project = ?" if project else ""
    scope_params: tuple = (project,) if project else ()

    durations = _rows(
        conn,
        f"SELECT tool, total_ms, ok FROM events WHERE ts >= ? {scope}",
        (since, *scope_params),
    )
    all_ms = [r["total_ms"] for r in durations if r["total_ms"] is not None]
    calls = len(durations)
    errors = sum(1 for r in durations if r["ok"] == 0)

    per_tool: dict[str, dict] = {}
    for row in durations:
        entry = per_tool.setdefault(
            row["tool"], {"tool": row["tool"], "calls": 0, "errors": 0, "_ms": []}
        )
        entry["calls"] += 1
        if row["ok"] == 0:
            entry["errors"] += 1
        if row["total_ms"] is not None:
            entry["_ms"].append(row["total_ms"])
    tools = []
    for entry in sorted(per_tool.values(), key=lambda e: -e["calls"]):
        ms = entry.pop("_ms")
        entry["p50_ms"] = _percentile(ms, 0.50)
        entry["p95_ms"] = _percentile(ms, 0.95)
        tools.append(entry)

    cost_row = conn.execute(
        f"SELECT COALESCE(SUM(est_cost_usd), 0),"
        f" SUM(CASE WHEN embed_tokens IS NOT NULL AND est_cost_usd IS NULL THEN 1 ELSE 0 END),"
        f" COALESCE(SUM(embed_tokens), 0),"
        f" SUM(CASE WHEN tokens_exact = 0 THEN 1 ELSE 0 END)"
        f" FROM events WHERE ts >= ? {scope}",
        (since, *scope_params),
    ).fetchone()

    quality_row = conn.execute(
        f"SELECT COUNT(*),"
        f" SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END),"
        f" AVG(top_score), AVG(tokens_returned)"
        f" FROM events WHERE ts >= ? AND tool = 'memory_search' {scope}",
        (since, *scope_params),
    ).fetchone()
    searches = quality_row[0] or 0

    offset_hours = tz_offset_minutes / 60.0
    modifier = f"{offset_hours:+.4f} hours"
    daily = _rows(
        conn,
        f"SELECT strftime('%Y-%m-%d', ts, 'unixepoch', ?) AS day, COUNT(*) AS calls"
        f" FROM events WHERE ts >= ? {scope} GROUP BY day ORDER BY day",
        (modifier, since, *scope_params),
    )

    return {
        "window": {"days": days, "since": since, "tz_offset_minutes": tz_offset_minutes},
        "tiles": {
            "calls": calls,
            "errors": errors,
            "error_rate": (errors / calls) if calls else 0.0,
            "p95_ms": _percentile(all_ms, 0.95),
            "est_cost_usd": cost_row[0],
            "embed_tokens": cost_row[2],
        },
        "unpriced_calls": cost_row[1] or 0,
        "estimated_token_calls": cost_row[3] or 0,
        "tools": tools,
        "agents": _rows(
            conn,
            f"SELECT agent_name, agent_version, COUNT(*) AS calls FROM events"
            f" WHERE ts >= ? AND agent_name IS NOT NULL {scope}"
            f" GROUP BY agent_name, agent_version ORDER BY calls DESC",
            (since, *scope_params),
        ),
        "quality": {
            "searches": searches,
            "zero_result_rate": ((quality_row[1] or 0) / searches) if searches else 0.0,
            "mean_top_score": quality_row[2],
            "mean_tokens_returned": quality_row[3],
        },
        "projects": _rows(
            conn,
            f"SELECT project, COUNT(*) AS calls FROM events"
            f" WHERE ts >= ? AND project IS NOT NULL {scope}"
            f" GROUP BY project ORDER BY calls DESC",
            (since, *scope_params),
        ),
        # Not windowed, deliberately: entry_hits is cumulative since first
        # observed. The dashboard labels this panel accordingly.
        "unused": _rows(
            conn,
            "SELECT project, point_id, hits, full_hits, last_seen_at FROM entry_hits"
            " ORDER BY hits ASC, last_seen_at ASC LIMIT 50",
        ),
        "audit": _rows(
            conn,
            f"SELECT ts, tool, project, deleted_count, agent_name, origin FROM events"
            f" WHERE ts >= ? AND (kind = 'admin' OR tool = 'memory_delete') {scope}"
            f" ORDER BY ts DESC LIMIT 50",
            (since, *scope_params),
        ),
        "errors": _rows(
            conn,
            f"SELECT ts, tool, error_class, error_msg, project FROM events"
            f" WHERE ts >= ? AND ok = 0 {scope} ORDER BY ts DESC LIMIT 20",
            (since, *scope_params),
        ),
        "daily": daily,
    }


def build_usage_summary(
    conn: sqlite3.Connection,
    days: int = 7,
    view: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """The agent-facing view. Tight by construction, about 500 tokens: an agent
    that needs more asks for one `view` rather than receiving a truncated blob."""
    since = _window_start(days, now)

    if view == "unused":
        return {
            "view": "unused",
            "entries": _rows(
                conn,
                "SELECT project, point_id, hits, full_hits, last_seen_at FROM entry_hits"
                " ORDER BY hits ASC, last_seen_at ASC LIMIT 20",
            ),
        }
    if view == "tools":
        return {
            "view": "tools",
            "entries": _rows(
                conn,
                "SELECT tool, COUNT(*) AS calls, AVG(total_ms) AS mean_ms,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors"
                " FROM events WHERE ts >= ? GROUP BY tool ORDER BY calls DESC LIMIT 20",
                (since,),
            ),
        }
    if view == "errors":
        return {
            "view": "errors",
            "entries": _rows(
                conn,
                "SELECT ts, tool, error_class, error_msg FROM events"
                " WHERE ts >= ? AND ok = 0 ORDER BY ts DESC LIMIT 10",
                (since,),
            ),
        }

    stats = build_stats(conn, days=days, now=now)
    return {
        "view": "summary",
        "days": days,
        "calls": stats["tiles"]["calls"],
        "errors": stats["tiles"]["errors"],
        "p95_ms": stats["tiles"]["p95_ms"],
        "est_cost_usd": stats["tiles"]["est_cost_usd"],
        "unpriced_calls": stats["unpriced_calls"],
        "zero_result_rate": stats["quality"]["zero_result_rate"],
        "top_tools": [
            {"tool": t["tool"], "calls": t["calls"]} for t in stats["tools"][:5]
        ],
        "top_agents": [
            {"agent": a["agent_name"], "calls": a["calls"]} for a in stats["agents"][:3]
        ],
        "unused_entries": len(stats["unused"]),
        "hint": "call again with view='unused' | 'tools' | 'errors' for detail",
    }
