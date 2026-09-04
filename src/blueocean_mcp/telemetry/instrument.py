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
import os
import pathlib
import time
import uuid
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context

from ..config import DEFAULT_EMBEDDING_PROVIDER
from ..health import resolve_embedding_model
from . import pricing, usage
from .writer import get_writer

logger = logging.getLogger(__name__)

# Reading the meter is not using the memory. If memory_usage were recorded, an
# open dashboard would inflate the numbers it displays and memory_usage would
# become the top tool within days.
INSTRUMENT_DENYLIST = frozenset({"memory_usage"})

_ERROR_MSG_MAX = 200

# Generated once at import. Stands in for the transport session id on stdio,
# where there are no headers to carry one.
_PROCESS_SESSION_ID = uuid.uuid4().hex


_REDACTED = "<redacted: contained caller text>"
_REDACTED_FOREIGN = "<redacted: third-party exception>"

# Our own package directory. An exception whose deepest traceback frame is
# outside it came from a library we call, and those libraries quote the
# payload they choked on - which is the user's memory.
_PACKAGE_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

# Arguments already stored in their own columns, and not content. Excluding
# them keeps an ordinary message like "collection blueocean_myproject not
# found" readable instead of redacting it for naming the project.
_NON_SENSITIVE_KEYS = frozenset({"project", "area", "module"})

# Below this length a caller argument matches ordinary words in an
# infrastructure message and would redact everything.
_MIN_SENSITIVE_LEN = 8


def _caller_strings(kwargs: dict) -> list[str]:
    out: list[str] = []
    for key, value in kwargs.items():
        if key in _NON_SENSITIVE_KEYS:
            continue
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, dict):
            out.extend(str(v) for v in value.values())
    return out


def _raised_by_us(exc: BaseException) -> bool:
    """True when the deepest traceback frame is inside this package."""
    tb, filename = exc.__traceback__, None
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    return bool(filename) and filename.startswith(_PACKAGE_ROOT)


def _safe_error_message(exc: Exception, kwargs: dict) -> str | None:
    """Never let an exception message carry memory content into telemetry.

    Auditing every dependency's message formatting forever is not a plan, so
    a message survives only if we raised it ourselves and it does not quote
    the caller. The cost is accepted: a Qdrant ConnectionError arrives as a
    class name without its text.
    """
    message = str(exc)
    if not message:
        return None
    if not _raised_by_us(exc):
        return _REDACTED_FOREIGN
    for value in _caller_strings(kwargs):
        if len(value) >= _MIN_SENSITIVE_LEN and value in message:
            return _REDACTED
    return message[:_ERROR_MSG_MAX]


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
        # streamable-http issues an mcp-session-id and the client echoes it
        # back. stdio has no headers at all, so without the fallback every
        # stdio call in every process recorded NULL and was indistinguishable
        # from every other. The process id at least groups one run together.
        out["session_id"] = headers.get("mcp-session-id") or _PROCESS_SESSION_ID
    except Exception:
        logger.debug("agent session identity unavailable", exc_info=True)
    return out


def _extract_search(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    """Pull quality columns and hit ids out of a token-budgeted search result.

    Only ids and scores are read. Nothing here touches the query, the summary
    text or the content: see the privacy rule in the spec.
    """
    if not isinstance(result, dict):
        return {}, None
    summary = result.get("summary") or []
    full = result.get("full") or []
    columns = {
        "result_count": len(summary),
        # No results means no top score. Recording 0.0 would be a real score
        # that never happened, and would drag every average down.
        "top_score": summary[0].get("score") if summary else None,
        "tokens_returned": result.get("total_tokens"),
    }
    project = kwargs.get("project")
    if project is None:
        return columns, None
    summary_ids = [e.get("id") for e in summary if e.get("id")]
    full_ids = [e.get("id") for e in full if e.get("id")]
    return columns, ("hits", project, summary_ids, full_ids)


def _extract_get(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    """memory_get counts as a full hit: the intent is identical to expanding
    an entry into the full layer."""
    found = isinstance(result, dict) and result.get("found") is True
    project, point_id = kwargs.get("project"), kwargs.get("point_id")
    if not found or not project or not point_id:
        return {}, None
    return {"result_count": 1}, ("hits", project, [point_id], [point_id])


def _extract_delete(kwargs: dict, result: Any) -> tuple[dict, tuple | None]:
    project, point_id = kwargs.get("project"), kwargs.get("point_id")
    columns = {"deleted_count": 1 if result else 0}
    if not result or not project or not point_id:
        return columns, None
    return columns, ("delete_hits", project, [point_id])


def _extract_summarize(kwargs: dict, _result: Any) -> tuple[dict, tuple | None]:
    """The agent-declared session label is the only link from an event back to
    a real entry in Qdrant. It is an opaque identifier the agent chose, not
    content, so recording it does not touch the privacy rule."""
    label = kwargs.get("session_id")
    return ({"agent_session_label": label} if label else {}), None


_EXTRACTORS = {
    "memory_search": _extract_search,
    "memory_get": _extract_get,
    "memory_delete": _extract_delete,
    "memory_summarize_session": _extract_summarize,
}


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
            row["error_msg"] = _safe_error_message(exc, kwargs)
            row["total_ms"] = (time.perf_counter() - started) * 1000
            row.update(usage.take())
            _price(row)
            _safe_record(writer_factory, row)
            raise
        row["ok"] = 1
        extractor = _EXTRACTORS.get(tool_name)
        if extractor is not None:
            try:
                columns, hit_instruction = extractor(kwargs, result)
                row.update(columns)
            except Exception:
                logger.debug("telemetry extractor failed", exc_info=True)
                hit_instruction = None
        else:
            hit_instruction = None
        row["total_ms"] = (time.perf_counter() - started) * 1000
        row.update(usage.take())
        _price(row)
        _safe_record(writer_factory, row)
        if hit_instruction is not None:
            _safe_hits(writer_factory, hit_instruction)
        return result

    # Both, deliberately. See the module docstring.
    wrapper.__signature__ = original_sig.replace(
        parameters=[*original_sig.parameters.values(), ctx_param]
    )
    wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "ctx": Context}
    return wrapper


def _price(row: dict) -> None:
    """Snapshot the price and its source onto the row, so the number stays
    truthful after the table is updated."""
    tokens = row.get("embed_tokens")
    if not tokens:
        return
    # Guarded like _safe_record and _safe_hits. This ran unguarded, so a
    # malformed pricing file propagated out of the memory operation itself.
    # Leaving the row unpriced is the right failure: NULL means unknown, which
    # is what we actually know here, and 0.0 would invent a free call.
    try:
        # Read the environment at call time, the way /health does. __main__
        # applies --embedding to os.environ only AFTER config was imported, so
        # the module-level default is stale whenever the flag is used, and
        # every OpenAI/Bedrock call would be priced at fastembed's $0.00.
        provider = os.getenv("BLUEOCEAN_EMBEDDING") or DEFAULT_EMBEDDING_PROVIDER
        model = resolve_embedding_model(provider)
        price, source = pricing.resolve(provider, model)
    except Exception:
        logger.debug("telemetry price lookup failed", exc_info=True)
        return
    row["unit_price_per_1m"] = price
    row["price_source"] = source
    row["est_cost_usd"] = pricing.cost_usd(tokens, price)


def _safe_record(writer_factory: Callable[[], Any], row: dict) -> None:
    try:
        w = writer_factory()
        if w is not None:
            w.record(row)
    except Exception:
        logger.debug("telemetry record failed", exc_info=True)


def _safe_hits(writer_factory: Callable[[], Any], instruction: tuple) -> None:
    try:
        w = writer_factory()
        if w is None:
            return
        if instruction[0] == "hits":
            w.record_hits(instruction[1], instruction[2], instruction[3])
        else:
            w.delete_hits(instruction[1], instruction[2])
    except Exception:
        logger.debug("telemetry hits update failed", exc_info=True)
