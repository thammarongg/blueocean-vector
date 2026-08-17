"""Bearer-token authentication for the streamable-http transport.

We deliberately do NOT use MCPServer's built-in OAuth-oriented auth system
(it requires issuer_url/resource_server_url and is aimed at full OAuth
resource servers). For a simple shared-secret token, a small ASGI middleware
wrapping the Starlette app is simpler, has no fake OAuth metadata, and is
decoupled from that subsystem's API (which has changed significantly between
mcp library versions).

Accepts the token via EITHER:
  - the standard `Authorization: Bearer <token>` header (supported natively by
    Claude Code, Gemini/Antigravity, and Codex's --bearer-token-env-var), or
  - a `?token=<token>` query parameter on the URL, for MCP clients that don't
    expose a way to set custom headers when registering a remote server by
    URL (e.g. Kiro's `mcp add --url` has no --header/--bearer flag).

stdio transport is not wrapped: each stdio server is a locally-spawned
subprocess, already gated by OS process-spawn permissions, not a network
listener.
"""

import hmac
import secrets

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def generate_token() -> str:
    """Generate a cryptographically random bearer token."""
    return secrets.token_urlsafe(32)


def _extract_token(scope: Scope) -> str | None:
    request = Request(scope)
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return request.query_params.get("token")


class BearerTokenMiddleware:
    """ASGI middleware requiring a matching bearer token on every request,
    except for a fixed set of exempt paths (health checks shouldn't need
    credentials). Wraps the app in place -- no extra Mount/routing layer,
    since nesting the streamable-http app inside another Starlette Mount
    was found to break its session-id/path handling.
    """

    def __init__(
        self, app: ASGIApp, expected_token: str, exempt_paths: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        self.expected_token = expected_token
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        token = _extract_token(scope)
        if not token or not hmac.compare_digest(token, self.expected_token):
            response = JSONResponse(
                {"error": "unauthorized", "error_description": "Missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def wrap_with_auth(
    app: Starlette, token: str | None, exempt_paths: frozenset[str] = frozenset()
) -> ASGIApp:
    """Wrap a Starlette app with bearer-token auth, if a token is configured."""
    if not token:
        return app
    return BearerTokenMiddleware(app, token, exempt_paths)