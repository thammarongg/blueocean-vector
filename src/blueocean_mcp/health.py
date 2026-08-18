"""Health check endpoint for the streamable-http transport.

Exposed at ``/health`` (never behind the bearer-token auth -- health check
tooling generally doesn't have credentials, and Kubernetes/Docker health
checks shouldn't need to know an app secret).

This is a real readiness check, not just "the process is alive": it also
verifies Qdrant is reachable, since a server that's up but can't talk to its
database isn't actually able to serve `memory_*` calls. It also reports the
active embedding provider/model, and -- for cloud providers only -- whether
their credentials are actually valid.

fastembed is deliberately excluded from the credential check: its model
loads synchronously in ``build_server()``, before the process can ever be up
to serve ``/health`` at all, so a broken load already crashes startup. A
cloud provider (openai/bedrock) has no such guarantee -- a bad API key or a
network issue only surfaces on the first real ``memory_store``/``search``
call unless something checks for it here.

The cloud-provider check deliberately does NOT call the embeddings endpoint
itself (billed per token) -- it uses each provider's free control-plane call
instead, which validates the same API key/network path. Its result is also
cached for ``DEFAULT_HEALTH_EMBED_TTL`` seconds so that docker-compose.yml's
10s healthcheck interval doesn't turn into a provider API call on every
single probe.

Designed to be reusable as-is if this later moves from `docker compose` to
Kubernetes: point a Deployment's `livenessProbe`/`readinessProbe` at the same
`GET /health` path (Kubernetes does NOT read docker-compose.yml's
`healthcheck:` field, nor a Dockerfile's `HEALTHCHECK` instruction -- it
needs its own probe defined in the Pod spec, but that probe can hit this same
endpoint).
"""

import os
import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import DEFAULT_EMBED_MODEL, DEFAULT_HEALTH_EMBED_TTL

# Providers we can validate without calling the billed embeddings endpoint.
_SELFTEST_PROVIDERS = frozenset({"openai", "bedrock"})


def resolve_embedding_model(provider: str) -> str:
    """Best-effort model name for diagnostics, mirroring create_embedder()'s
    own per-provider resolution so /health reports what's actually running.
    """
    if provider == "fastembed":
        return os.getenv("BLUEOCEAN_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    if provider == "openai":
        from .embeddings.openai import DEFAULT_MODEL

        return DEFAULT_MODEL
    if provider == "bedrock":
        from .embeddings.bedrock import DEFAULT_MODEL_ID

        return os.getenv("BLUEOCEAN_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    return "unknown"


def _run_provider_check(provider: str, model: str) -> tuple[bool, str | None]:
    """Free/cheap credential+reachability check for cloud embedding
    providers -- never calls the billed embed endpoint. Returns (True, None)
    for fastembed or any provider this function doesn't recognize.
    """
    try:
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL")
            )
            client.models.retrieve(model)
        elif provider == "bedrock":
            import boto3

            client = boto3.client("bedrock", region_name=os.getenv("AWS_REGION", "us-east-1"))
            client.get_foundation_model(modelIdentifier=model)
        return True, None
    except Exception as e:  # noqa: BLE001 -- any failure means "unhealthy"
        return False, str(e)


class _SelfTestCache:
    """Caches the cloud-provider self-test result per provider for `ttl`
    seconds, so a chatty health-check probe doesn't turn into a provider API
    call (and, for rate-limited providers, a real risk) on every single hit.
    """

    def __init__(self, ttl: float, now_fn: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._now = now_fn
        self._cache: dict[str, tuple[float, bool, str | None]] = {}

    def check(
        self,
        provider: str,
        model: str,
        check_fn: Callable[[str, str], tuple[bool, str | None]] = _run_provider_check,
    ) -> tuple[bool, str | None]:
        now = self._now()
        cached = self._cache.get(provider)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1], cached[2]
        ok, error = check_fn(provider, model)
        self._cache[provider] = (now, ok, error)
        return ok, error


def build_health_route(
    qdrant_url: str,
    embedding_provider: str,
    embedding_model: str,
    *,
    embed_ttl: float = DEFAULT_HEALTH_EMBED_TTL,
):
    """Returns an async Starlette route handler for GET /health."""

    selftest_cache = _SelfTestCache(embed_ttl)

    async def health(_request: Request) -> JSONResponse:
        from qdrant_client import QdrantClient

        body: dict = {"embedding": {"provider": embedding_provider, "model": embedding_model}}

        qdrant_ok = True
        try:
            client = QdrantClient(url=qdrant_url, timeout=3)
            client.get_collections()
            body["qdrant"] = "reachable"
        except Exception as e:  # noqa: BLE001 -- any failure means "unhealthy"
            qdrant_ok = False
            body["qdrant"] = "unreachable"
            body["error"] = str(e)

        if embedding_provider in _SELFTEST_PROVIDERS:
            embedding_ok, embed_error = selftest_cache.check(embedding_provider, embedding_model)
        else:
            embedding_ok, embed_error = True, None
        body["embedding"]["ok"] = embedding_ok
        if embed_error is not None:
            body["embedding"]["error"] = embed_error

        status_code = 200 if (qdrant_ok and embedding_ok) else 503
        body = {"status": "ok" if status_code == 200 else "unhealthy", **body}
        return JSONResponse(body, status_code=status_code)

    return health
