"""Health check endpoint for the streamable-http transport.

Exposed at ``/health`` (never behind the bearer-token auth -- health check
tooling generally doesn't have credentials, and Kubernetes/Docker health
checks shouldn't need to know an app secret).

This is a real readiness check, not just "the process is alive": it also
verifies Qdrant is reachable, since a server that's up but can't talk to its
database isn't actually able to serve `memory_*` calls.

Designed to be reusable as-is if this later moves from `docker compose` to
Kubernetes: point a Deployment's `livenessProbe`/`readinessProbe` at the same
`GET /health` path (Kubernetes does NOT read docker-compose.yml's
`healthcheck:` field, nor a Dockerfile's `HEALTHCHECK` instruction -- it
needs its own probe defined in the Pod spec, but that probe can hit this same
endpoint).
"""

from starlette.requests import Request
from starlette.responses import JSONResponse


def build_health_route(qdrant_url: str):
    """Returns an async Starlette route handler for GET /health."""

    async def health(_request: Request) -> JSONResponse:
        from qdrant_client import QdrantClient

        try:
            client = QdrantClient(url=qdrant_url, timeout=3)
            client.get_collections()
            return JSONResponse({"status": "ok", "qdrant": "reachable"}, status_code=200)
        except Exception as e:  # noqa: BLE001 -- any failure means "unhealthy"
            return JSONResponse(
                {"status": "unhealthy", "qdrant": "unreachable", "error": str(e)},
                status_code=503,
            )

    return health