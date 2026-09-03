"""Tests for telemetry aggregation and its HTTP surface.

Run with:
    uv run python -m tests.telemetry_http
"""

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from blueocean_mcp.telemetry import db, queries

from ._helpers import BIN

PORT = 8798


def _seed(conn, rows: list[dict]) -> None:
    for row in rows:
        cols = list(row)
        conn.execute(
            f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
    conn.commit()


def test_build_stats_shapes_every_panel() -> None:
    print("== build_stats returns one object with every panel ==")
    now = int(time.time())
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [
            {"ts": now - 60, "kind": "tool", "tool": "memory_search", "project": "p",
             "agent_name": "claude-code", "ok": 1, "total_ms": 40.0,
             "result_count": 2, "top_score": 0.8, "tokens_returned": 100,
             "embed_tokens": 50, "est_cost_usd": 0.000001, "origin": "observed"},
            {"ts": now - 30, "kind": "tool", "tool": "memory_search", "project": "p",
             "agent_name": "codex", "ok": 1, "total_ms": 90.0,
             "result_count": 0, "top_score": None, "tokens_returned": 0,
             "embed_tokens": 30, "origin": "observed"},
            {"ts": now - 10, "kind": "tool", "tool": "memory_store", "project": "p",
             "agent_name": "claude-code", "ok": 0, "error_class": "ValueError",
             "error_msg": "too long", "total_ms": 5.0, "origin": "observed"},
            {"ts": now - 5, "kind": "admin", "tool": "prune", "project": "p",
             "ok": 1, "deleted_count": 3, "origin": "cli-reported"},
        ])
        conn.execute(
            "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
            "VALUES ('p', 'never-used', 0, 0, NULL)"
        )
        conn.commit()

        stats = queries.build_stats(conn, days=7, tz_offset_minutes=420)
        assert stats["tiles"]["calls"] == 4, stats["tiles"]
        assert stats["tiles"]["errors"] == 1, stats["tiles"]
        assert stats["tiles"]["p95_ms"] is not None
        tools = {t["tool"]: t for t in stats["tools"]}
        assert tools["memory_search"]["calls"] == 2, tools
        agents = {a["agent_name"]: a["calls"] for a in stats["agents"]}
        assert agents["claude-code"] == 2, agents
        assert stats["quality"]["searches"] == 2, stats["quality"]
        assert abs(stats["quality"]["zero_result_rate"] - 0.5) < 1e-9, stats["quality"]
        assert stats["unused"][0]["point_id"] == "never-used", stats["unused"]
        assert stats["audit"][0]["tool"] == "prune", stats["audit"]
        assert stats["errors"][0]["error_class"] == "ValueError", stats["errors"]
        assert stats["unpriced_calls"] >= 1, stats
        conn.close()
    print("  OK")


def test_day_bucketing_uses_the_callers_offset() -> None:
    """An event at 23:30 local time in UTC+7 is 16:30 UTC the same day. Bucketing
    by UTC would file it under the wrong local day and quietly shift every
    daily number."""
    print("== daily buckets follow the caller's offset ==")
    # 2026-09-02 23:30 in UTC+7 == 2026-09-02 16:30 UTC == epoch 1788366600
    ts = 1788366600
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [{"ts": ts, "kind": "tool", "tool": "memory_search",
                      "ok": 1, "total_ms": 1.0, "origin": "observed"}])
        local = queries.build_stats(conn, days=3650, tz_offset_minutes=420)["daily"]
        utc = queries.build_stats(conn, days=3650, tz_offset_minutes=0)["daily"]
        assert local[-1]["day"] == "2026-09-02", local
        assert utc[-1]["day"] == "2026-09-02", utc

        # And an event at 00:30 local (17:30 UTC the previous day) lands on the
        # local day, not the UTC one.
        conn.execute("DELETE FROM events")
        # 2026-09-02 00:30 in UTC+7 is 2026-09-01 17:30 UTC.
        _seed(conn, [{"ts": 1788283800, "kind": "tool",
                      "tool": "memory_search", "ok": 1, "origin": "observed"}])
        local = queries.build_stats(conn, days=3650, tz_offset_minutes=420)["daily"]
        utc = queries.build_stats(conn, days=3650, tz_offset_minutes=0)["daily"]
        assert local[-1]["day"] != utc[-1]["day"], (local, utc)
        conn.close()
    print("  OK")


def test_usage_summary_is_small() -> None:
    """The agent-facing view has a token budget. Roughly 4 chars per token is
    the same heuristic token_budget.py uses."""
    print("== memory_usage summary stays inside its budget ==")
    now = int(time.time())
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        _seed(conn, [
            {"ts": now - i, "kind": "tool", "tool": f"memory_tool_{i % 9}",
             "project": f"project-{i % 7}", "agent_name": f"agent-{i % 5}",
             "ok": 1, "total_ms": float(i), "origin": "observed"}
            for i in range(500)
        ])
        summary = queries.build_usage_summary(conn, days=7)
        rendered = json.dumps(summary, ensure_ascii=False)
        assert len(rendered) / 4 < 500, f"~{len(rendered) // 4} tokens, budget is 500"
        assert len(summary["top_tools"]) <= 5, summary["top_tools"]
        assert len(summary["top_agents"]) <= 3, summary["top_agents"]
        conn.close()
    print("  OK")


def test_usage_summary_view_drilldown() -> None:
    print("== the view parameter drills into one list ==")
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(str(Path(d) / "t.db"))
        conn.execute(
            "INSERT INTO entry_hits (project, point_id, hits, full_hits, last_seen_at) "
            "VALUES ('p', 'cold', 0, 0, NULL)"
        )
        conn.commit()
        out = queries.build_usage_summary(conn, days=7, view="unused")
        assert out["view"] == "unused", out
        assert out["entries"][0]["point_id"] == "cold", out
        conn.close()
    print("  OK")


def _start_server(env_extra: dict, token: str | None = None) -> subprocess.Popen:
    import os
    env = {**os.environ, **env_extra}
    args = [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(PORT)]
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"server exited early:\n{err}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise TimeoutError("server did not start")


def _get(path: str, token: str | None = None) -> tuple[int, dict | str]:
    url = f"http://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_stats_requires_a_token() -> None:
    print("== /api/stats and /dashboard require the token ==")
    with tempfile.TemporaryDirectory() as d:
        proc = _start_server({
            "BLUEOCEAN_AUTH_TOKEN": "secret-token",
            "BLUEOCEAN_TELEMETRY_DB": str(Path(d) / "t.db"),
        })
        try:
            for path in ("/api/stats", "/dashboard", "/api/audit", "/api/prices"):
                status, _ = _get(path)
                assert status == 401, f"{path} returned {status}, expected 401"
            status, body = _get("/api/stats", token="secret-token")
            assert status == 200, (status, body)
            assert "tiles" in body, body
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    print("  OK")


def test_disabled_returns_503_not_404() -> None:
    """A 404 makes a deliberate configuration look like a broken deployment."""
    print("== telemetry off answers 503 with an explanation ==")
    proc = _start_server({"BLUEOCEAN_TELEMETRY": "0", "BLUEOCEAN_AUTH_TOKEN": ""})
    try:
        for path in ("/api/stats", "/dashboard"):
            status, body = _get(path)
            assert status == 503, f"{path} returned {status}"
            assert "BLUEOCEAN_TELEMETRY" in str(body), body
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    print("  OK")


def test_audit_row_cannot_claim_to_be_observed() -> None:
    """One shared token means anyone who can call a tool can post an audit row.
    The server stamps what it can verify and refuses the rest."""
    print("== posted audit rows are stamped cli-reported ==")
    with tempfile.TemporaryDirectory() as d:
        db_file = str(Path(d) / "t.db")
        proc = _start_server({
            "BLUEOCEAN_AUTH_TOKEN": "secret-token",
            "BLUEOCEAN_TELEMETRY_DB": db_file,
        })
        try:
            payload = json.dumps({
                "tool": "prune", "project": "p", "deleted_count": 9,
                "origin": "observed", "ts": 1, "agent_name": "someone-else",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/audit", data=payload,
                headers={"Authorization": "Bearer secret-token",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 202, resp.status
            time.sleep(1.0)
            _, body = _get("/api/stats", token="secret-token")
            audit = [r for r in body["audit"] if r["tool"] == "prune"]
            assert audit, body["audit"]
            assert audit[0]["origin"] == "cli-reported", audit[0]
            assert audit[0]["ts"] != 1, "the server stamps its own timestamp"
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    print("  OK")


def main() -> None:
    test_build_stats_shapes_every_panel()
    test_day_bucketing_uses_the_callers_offset()
    test_usage_summary_is_small()
    test_usage_summary_view_drilldown()
    test_stats_requires_a_token()
    test_disabled_returns_503_not_404()
    test_audit_row_cannot_claim_to_be_observed()
    print("\nTELEMETRY HTTP TEST PASSED")


if __name__ == "__main__":
    main()
