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

_FENCE_OPEN_RE = re.compile(r"<\s*memory-context\s*>", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"<\s*/\s*memory-context\s*>", re.IGNORECASE)

# Every pattern below is anchored and bounded so that its work stays linear (or close to
# it) on adversarial input: a run of identifier characters must not be rescanned from every
# position.
_START = r"(?<![A-Za-z0-9_])"
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=-]"
_SECRET_PATTERNS = [
    # A PEM or PGP block; one with no END marker runs to the end of the text.
    re.compile(
        r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----"
        r"(?:[\s\S]*?-----END [A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----|[\s\S]*$)"
    ),
    re.compile(
        _START + r"Bearer\s{1,10}(?:(?=" + _TOKEN_CHARS + r"*\d)" + _TOKEN_CHARS + r"{8,}|"
        + _TOKEN_CHARS + r"{20,})",
        re.IGNORECASE,
    ),
    # Basic only after an Authorization header name: "Basic authentication" is prose.
    re.compile(
        r"(?<![A-Za-z0-9_-])(Authorization[\"']?\s{0,10}:\s{0,10}[\"']?)Basic\s{1,10}"
        r"[A-Za-z0-9+/=_-]{4,}",
        re.IGNORECASE,
    ),
    re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(_START + r"[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"),  # Stripe
    re.compile(_START + r"AIza[0-9A-Za-z_-]{20,}"),  # Google API key
    re.compile(_START + r"A[SK]IA[0-9A-Z]{16}(?![0-9A-Z])"),  # AWS, long-lived and temporary
    re.compile(_START + r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(_START + r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(_START + r"glpat-[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(_START + r"hf_[A-Za-z0-9]{20,}"),  # Hugging Face
    re.compile(_START + r"npm_[A-Za-z0-9]{20,}"),
    re.compile(_START + r"(?:xox[abpr]|xapp)-[A-Za-z0-9-]{10,}"),  # Slack
]
# The password runs to the last `@` before whitespace, so one holding `/`, `+` or a raw
# `@` is dropped whole. The run is bounded, so each scheme costs at most ~2x256 steps.
_URL_PASSWORD_RE = re.compile(
    _START + r"([a-z][a-z0-9+.-]{0,30}://[^\s:/@]{1,100}:)\S{1,256}(@)", re.IGNORECASE
)
# The key may carry name prefixes/suffixes (GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY). It starts
# at the beginning of an identifier run, and the prefix and suffix are bounded. A name that
# merely ends in `_key`/`-key` (STRIPE_KEY, signing-key) counts too. The value is a whole
# quoted string or a run of non-space characters.
_NAME = r"[A-Za-z0-9_.-]"
_ASSIGNMENT_RE = re.compile(
    r"((?<!" + _NAME + r")" + _NAME + r"{0,64}"
    r"(?:(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credentials?|passphrase)" + _NAME + r"{0,64}|[_-]key)"
    r"[\"']?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"


def strip_memory_context(text: str | None) -> str:
    """Drop every recalled-memory fence; an unclosed one runs to the end of the text."""
    text = text or ""
    kept: list[str] = []
    pos = 0
    while True:
        opening = _FENCE_OPEN_RE.search(text, pos)
        if opening is None:
            kept.append(text[pos:])
            break
        kept.append(text[pos : opening.start()])
        closing = _FENCE_CLOSE_RE.search(text, opening.end())
        if closing is None:
            break
        pos = closing.end()
    return "".join(kept).strip()


def _redact_match(match: re.Match) -> str:
    # A pattern that captures a header name keeps it; the rest go whole.
    return (match.group(1) if match.re.groups else "") + REDACTED


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redact_match, text)
    text = _URL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", text)
    return _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _bounded_cut(text: str, limit: int) -> str:
    """At most 4 x limit characters, so redaction's work is bounded however large the input.

    The cut falls on the last whitespace in [limit, 4 x limit] rather than at a fixed
    index: redaction can shrink the kept text by thousands of characters, which would pull
    a credential cut in half at the boundary into the final window, too short for any
    pattern to recognise. Text with no whitespace there is cut at 4 x limit.
    """
    hard = 4 * limit
    if len(text) <= hard:
        return text
    for i in range(hard, limit - 1, -1):
        if text[i].isspace():
            return text[:i]
    return text[:hard]


def clean_turn_side(text: str | None, limit: int = TRACE_SIDE_LIMIT) -> str:
    return clip(redact(_bounded_cut(strip_memory_context(text), limit)), limit)


def clean_query(text: str | None, limit: int) -> str:
    """A recall query with credentials removed: with a remote embedding provider the query
    leaves the machine."""
    return redact(_bounded_cut(text or "", limit))[:limit]


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
    except (TypeError, ValueError, OverflowError, OSError):
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
