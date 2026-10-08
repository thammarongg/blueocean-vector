"""Text the provider stores and injects.

Before a turn is stored as a Turn Trace it loses any recalled-memory fence
(otherwise recall would feed itself back into the store) and any credential
pasted into chat (the store is read by every agent on the machine). Recalled
entries are de-duplicated by id per session and formatted for the prompt.

Patterns anchor with lookarounds on ASCII characters, never ``\\b``: Python
treats Thai letters as word characters, so ``\\b`` finds no boundary between
Thai text and a token typed right after it.

Imports nothing from Hermes.
"""

from __future__ import annotations

import re
import time

TRACE_SIDE_LIMIT = 2000
SUMMARY_SIDE_LIMIT = 150

_FENCE_RE = re.compile(r"<\s*memory-context\s*>[\s\S]*?<\s*/\s*memory-context\s*>", re.IGNORECASE)

_START = r"(?<![A-Za-z0-9_])"
_SECRET_PATTERNS = [
    re.compile(_START + r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(_START + r"AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(_START + r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(_START + r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(_START + r"xox[abpr]-[A-Za-z0-9-]{10,}"),
]
_ASSIGNMENT_RE = re.compile(
    _START + r"(password|passwd|token|secret|api_key|apikey)(\s*[:=]\s*)(\S+)", re.IGNORECASE
)

REDACTED = "[REDACTED]"


def strip_memory_context(text: str | None) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def redact(text: str) -> str:
    text = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clean_turn_side(text: str | None, limit: int = TRACE_SIDE_LIMIT) -> str:
    return clip(redact(strip_memory_context(text)), limit)


def trace_content(user: str, assistant: str) -> str:
    return f"User: {user}\n\nAssistant: {assistant}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def trace_summary(user: str, assistant: str) -> str:
    return (f"{clip(_one_line(user), SUMMARY_SIDE_LIMIT)} -> "
            f"{clip(_one_line(assistant), SUMMARY_SIDE_LIMIT)}")


def unseen(result: dict, seen: set[str]) -> tuple[list[dict], list[dict]]:
    """The summary and full rows of a memory_search result not injected yet."""
    summaries = [r for r in result.get("summary", []) if str(r.get("id")) not in seen]
    fulls = [r for r in result.get("full", []) if str(r.get("id")) not in seen]
    return summaries, fulls


def _date(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError, OverflowError):
        return "?"


def format_recall(project: str, summaries: list[dict], fulls: list[dict]) -> str:
    lines = [f"BlueOcean project memory - {project}"]
    for row in summaries:
        meta = row.get("metadata") or {}
        lines.append(
            f"- [{meta.get('area', '?')}/{meta.get('module', '?')} · "
            f"{_date(meta.get('timestamp'))} · importance {meta.get('importance', '?')}] "
            f"{row.get('summary', '')}"
        )
    if fulls:
        lines += ["", "Details:"]
        lines += [f"[{str(row.get('id'))[:8]}] {row.get('content', '')}" for row in fulls]
    return "\n".join(lines)
