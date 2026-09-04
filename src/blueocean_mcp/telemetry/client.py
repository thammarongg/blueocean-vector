"""The admin CLI's view of telemetry: over HTTP, never off disk.

SQLite's locking is not dependable across a Docker Desktop bind mount, so the
server process is the only one that opens the database. The CLI asks the
server. When the server is not there, that is an error the operator sees, not
something to paper over by opening the file anyway.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from ..config import DEFAULT_SERVER_URL


class ServerUnavailable(RuntimeError):
    """Raised when the telemetry server cannot be reached or refuses."""


def _request(
    path: str,
    server_url: str | None,
    token: str | None,
    method: str = "GET",
    payload: dict | None = None,
) -> dict:
    base = (server_url or DEFAULT_SERVER_URL).rstrip("/")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise ServerUnavailable(
            f"could not reach telemetry on {base}{path}: HTTP {e.code} {detail}"
        ) from e
    except OSError as e:
        raise ServerUnavailable(f"could not reach telemetry on {base}: {e}") from e


def fetch_stats(
    days: int,
    tz_offset_minutes: int,
    project: str | None,
    server_url: str | None = None,
    token: str | None = None,
) -> dict:
    query = f"?days={days}&tz_offset_minutes={tz_offset_minutes}"
    if project:
        query += f"&project={urllib.parse.quote(project)}"
    return _request(f"/api/stats{query}", server_url, token)


def post_audit(
    row: dict, server_url: str | None = None, token: str | None = None
) -> bool:
    _request("/api/audit", server_url, token, method="POST", payload=row)
    return True


def post_prices(
    prices: dict, server_url: str | None = None, token: str | None = None
) -> bool:
    _request("/api/prices", server_url, token, method="POST", payload={"prices": prices})
    return True
