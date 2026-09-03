"""Starlette routes for the telemetry surface.

None of these are auth-exempt. docker-compose really does run with
--host 0.0.0.0, so the dashboard is reachable off-host and must sit behind the
same bearer token as everything else.
"""

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import db, is_enabled, pricing
from .queries import build_stats
from .writer import get_writer

_DISABLED_BODY = {
    "status": "disabled",
    "detail": "Telemetry is off. Set BLUEOCEAN_TELEMETRY=1 to enable it.",
}

_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")


def _disabled() -> JSONResponse:
    return JSONResponse(_DISABLED_BODY, status_code=503)


async def _stats(request: Request) -> JSONResponse:
    if not is_enabled():
        return _disabled()
    try:
        days = int(request.query_params.get("days", "7"))
        tz_offset = int(request.query_params.get("tz_offset_minutes", "0"))
    except ValueError:
        return JSONResponse({"error": "days and tz_offset_minutes must be integers"}, 400)
    project = request.query_params.get("project")
    conn = db.connect()
    try:
        body = build_stats(conn, days=days, tz_offset_minutes=tz_offset, project=project)
    finally:
        conn.close()
    writer = get_writer()
    body["dropped_events"] = writer.dropped if writer is not None else 0
    return JSONResponse(body)


async def _audit(request: Request) -> JSONResponse:
    """Record an audit row reported by the CLI.

    The server stamps the timestamp and origin. A posted row may not claim
    origin='observed': that value is reserved for calls this process handled
    itself. With one shared token this is the strongest guarantee available,
    and the docs say so rather than implying more.
    """
    if not is_enabled():
        return _disabled()
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, 400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, 400)

    import time as _time

    allowed = {"tool", "project", "area", "module", "deleted_count", "ok",
               "error_class", "error_msg", "agent_name"}
    row = {k: v for k, v in payload.items() if k in allowed}
    if not row.get("tool"):
        return JSONResponse({"error": "tool is required"}, 400)
    row["error_msg"] = (str(row["error_msg"])[:200] if row.get("error_msg") else None)
    row["ts"] = int(_time.time())
    row["kind"] = "admin"
    row["origin"] = "cli-reported"
    row.setdefault("ok", 1)

    writer = get_writer()
    if writer is not None:
        writer.record(row)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _prices(request: Request) -> JSONResponse:
    """Accept a price map fetched by the CLI and write the pricing file.

    The CLI makes the outbound call; the server owns the file. That keeps the
    server from reaching the network on its own and keeps the host from
    guessing what path the file has inside the container.
    """
    if not is_enabled():
        return _disabled()
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, 400)
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, dict) or not prices:
        return JSONResponse({"error": "prices must be a non-empty object"}, 400)
    clean = {str(k): float(v) for k, v in prices.items() if isinstance(v, (int, float))}
    pricing.write_file(None, clean)
    return JSONResponse({"status": "written", "models": len(clean)})


async def _dashboard(_request: Request) -> HTMLResponse:
    if not is_enabled():
        return _disabled()
    return HTMLResponse(
        _DASHBOARD_HTML.read_text(),
        headers={"Referrer-Policy": "no-referrer"},
    )


def telemetry_routes() -> list[Route]:
    return [
        Route("/api/stats", _stats),
        Route("/api/audit", _audit, methods=["POST"]),
        Route("/api/prices", _prices, methods=["POST"]),
        Route("/dashboard", _dashboard),
    ]
