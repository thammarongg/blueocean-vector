"""Starlette routes for the telemetry surface.

None of these are auth-exempt. docker-compose really does run with
--host 0.0.0.0, so the dashboard is reachable off-host and must sit behind the
same bearer token as everything else.
"""

import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import db, is_enabled, pricing
from .queries import build_stats
from .writer import get_writer

_DISABLED_BODY = {
    "status": "disabled",
    "detail": "Telemetry is off. Set BLUEOCEAN_TELEMETRY=1 to enable it.",
}

_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")

# A posted audit row is checked by type as well as by name. The field lists
# name the columns a caller may set; the three helpers below decide what a
# value has to look like before it reaches SQLite. Without them a value
# SQLite cannot bind (a list, an object) raises inside the writer thread,
# and writer._run answers any write failure by disabling telemetry for the
# life of the process - so one malformed POST would quietly end recording
# until the next restart.
_AUDIT_TEXT_MAX = 120
_ERROR_CLASS_MAX = 64
_AUDIT_TEXT_FIELDS = ("tool", "project", "area", "module", "agent_name")
_AUDIT_INT_FIELDS = ("deleted_count", "ok")


def _audit_text(value: Any) -> str | None:
    """Keep a bounded string, drop anything else. project, area and module
    are content-free dimensions by the spec's own reckoning (section 9), so
    these are capped, never inspected."""
    if not isinstance(value, str) or not value:
        return None
    return value[:_AUDIT_TEXT_MAX]


def _audit_int(value: Any) -> int | None:
    """Keep a real integer. bool is an int subclass, and would land as 0 or 1
    for a caller who meant something else, so it is refused with the rest."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _audit_error_class(value: Any) -> str | None:
    """error_class survives on a posted row because section 8.1 calls it a
    classification: "the classification survives, only the unverifiable text
    goes". That reasoning holds only while the value really is a class name,
    and nothing enforced it - so arbitrary caller text could reach the
    database through this field exactly as it once did through error_msg.
    A dotted identifier is kept; anything else is dropped rather than
    truncated, because 64 characters of someone's memory is still memory.
    """
    if not isinstance(value, str) or not value or len(value) > _ERROR_CLASS_MAX:
        return None
    if not all(part.isidentifier() for part in value.split(".")):
        return None
    return value


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

    error_msg is discarded rather than stored. Unlike the messages this
    server records from exceptions it observed itself (sanitized on the way
    in by instrument.py), a posted error_msg is arbitrary caller text, and
    the privacy rule says caller text never reaches the database. The
    request is still accepted: error_class carries the classification, and
    rejecting the row would lose the audit trail over a field we never
    wanted. Existing rows from before this rule are swept NULL by the v2
    schema migration.
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

    # No error_msg here, on purpose: see the docstring above.
    row: dict[str, Any] = {}
    for key in _AUDIT_TEXT_FIELDS:
        text = _audit_text(payload.get(key))
        if text is not None:
            row[key] = text
    for key in _AUDIT_INT_FIELDS:
        number = _audit_int(payload.get(key))
        if number is not None:
            row[key] = number
    error_class = _audit_error_class(payload.get("error_class"))
    if error_class is not None:
        row["error_class"] = error_class

    if not row.get("tool"):
        return JSONResponse({"error": "tool is required"}, 400)
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
    if not isinstance(prices, dict):
        return JSONResponse({"error": "prices must be an object"}, 400)
    clean = {
        str(k): float(v)
        for k, v in prices.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    # Tested after the filter, not before it: a map whose values are all
    # unusable passes a non-empty check on the way in, and writing it would
    # replace the pricing file with nothing, costing every model its price.
    if not clean:
        return JSONResponse(
            {"error": "prices must contain at least one numeric price"}, 400
        )
    pricing.write_file(None, clean)
    return JSONResponse({"status": "written", "models": len(clean)})


async def _dashboard(_request: Request) -> Response:
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
