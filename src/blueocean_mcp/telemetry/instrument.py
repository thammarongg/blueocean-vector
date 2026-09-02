"""One wrapper, applied to every registered tool in one loop.

Two things here are load-bearing and easy to "simplify" into a silent bug.

1. The wrapper sets BOTH __signature__ and __annotations__. The mcp library
   locates the Context parameter with typing.get_type_hints (annotations) but
   builds the agent-visible JSON schema from the signature. Set only one and
   either ctx is never injected, or ctx appears in the schema agents see.
   Neither raises.
2. Nothing in here may raise. A telemetry problem must not become a memory
   failure, so every telemetry call sits inside a bare except.
"""

import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context

from . import usage
from .writer import get_writer

logger = logging.getLogger(__name__)

# Reading the meter is not using the memory. If memory_usage were recorded, an
# open dashboard would inflate the numbers it displays and memory_usage would
# become the top tool within days.
INSTRUMENT_DENYLIST = frozenset({"memory_usage"})

_ERROR_MSG_MAX = 200


def _agent_identity(ctx: Any) -> dict:
    """Best-effort identity from the MCP handshake. Every field is
    client-supplied: fine as a grouping key, never an identity assertion."""
    out: dict = {}
    if ctx is None:
        return out
    try:
        params = ctx.session.client_params
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        if info is not None:
            out["agent_name"] = getattr(info, "name", None)
            out["agent_version"] = getattr(info, "version", None)
    except Exception:
        logger.debug("agent client identity unavailable", exc_info=True)
    try:
        headers = ctx.headers or {}
        out["session_id"] = headers.get("mcp-session-id")
    except Exception:
        logger.debug("agent session identity unavailable", exc_info=True)
    return out


def instrument(
    fn: Callable, tool_name: str, writer_factory: Callable[[], Any] = get_writer
) -> Callable:
    original_sig = inspect.signature(fn)
    ctx_param = inspect.Parameter(
        "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context
    )

    @functools.wraps(fn)
    def wrapper(*args, ctx: Context | None = None, **kwargs):
        started = time.perf_counter()
        usage.reset()
        row: dict = {
            "ts": int(time.time()),
            "kind": "tool",
            "tool": tool_name,
            "origin": "observed",
        }
        for field in ("project", "area", "module"):
            if field in kwargs:
                row[field] = kwargs[field]
        row.update(_agent_identity(ctx))

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            row["ok"] = 0
            row["error_class"] = type(exc).__name__
            row["error_msg"] = str(exc)[:_ERROR_MSG_MAX]
            row["total_ms"] = (time.perf_counter() - started) * 1000
            row.update(usage.take())
            _safe_record(writer_factory, row)
            raise
        row["ok"] = 1
        row["total_ms"] = (time.perf_counter() - started) * 1000
        row.update(usage.take())
        _safe_record(writer_factory, row)
        return result

    # Both, deliberately. See the module docstring.
    wrapper.__signature__ = original_sig.replace(
        parameters=[*original_sig.parameters.values(), ctx_param]
    )
    wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "ctx": Context}
    return wrapper


def _safe_record(writer_factory: Callable[[], Any], row: dict) -> None:
    try:
        w = writer_factory()
        if w is not None:
            w.record(row)
    except Exception:
        logger.debug("telemetry record failed", exc_info=True)
