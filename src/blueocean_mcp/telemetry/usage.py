"""Per-call embedding accounting.

A ContextVar, not a threading.local. Sync tool functions are dispatched via
anyio.to_thread.run_sync (mcp/server/mcpserver/utilities/func_metadata.py),
which uses a reused worker thread pool: a thread-local that is not cleared at
call start would report the previous call's tokens. anyio copies the context
into the worker thread, so a ContextVar is correct on both the threaded and
the direct-await path.

The alternative was changing the Embedder ABC to return usage alongside the
vectors. That is a breaking change to a public interface for the sake of a
secondary feature, so usage rides this side channel instead.
"""

from contextvars import ContextVar

_usage: ContextVar[dict | None] = ContextVar("blueocean_embed_usage", default=None)


def reset() -> None:
    _usage.set({"embed_tokens": None, "embed_ms": 0.0, "tokens_exact": 1})


def add(tokens: int | None, ms: float, exact: bool) -> None:
    """Accumulate one embedding call. Providers that loop (Bedrock invokes the
    model once per text) call this once per invocation, so values sum."""
    current = _usage.get()
    if current is None:
        return
    if tokens is not None:
        current["embed_tokens"] = (current["embed_tokens"] or 0) + int(tokens)
    current["embed_ms"] += float(ms)
    if not exact:
        current["tokens_exact"] = 0


def take() -> dict:
    current = _usage.get()
    if current is None:
        return {"embed_tokens": None, "embed_ms": 0.0, "tokens_exact": 1}
    return dict(current)
