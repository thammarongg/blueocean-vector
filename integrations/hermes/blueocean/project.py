"""Which BlueOcean project a Hermes session is talking about.

Hermes runs across many repositories and its gateway has no working
directory, so the project is detected from the conversation. The latest
signal wins: a cwd default, a project named in a message, or an explicit
tool call. Whatever was set last stays active for later turns. A configured
allowlist (with aliases) narrows what a message can select: on a chat app,
a project named like an ordinary word ("kubernetes") would otherwise stick.

Names match on a lookaround boundary rather than ``\\b``: Python counts Thai
letters as word characters, so ``\\b`` finds no boundary in ``ดูkops``.
Hyphens and underscores count as part of a name, so ``kops`` does not match
inside ``kopsctl`` and ``blueocean`` does not match inside
``blueocean-vector``.

Imports nothing from Hermes.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

_NAME_CHAR = r"[A-Za-z0-9_-]"

# The server's project-name rule (src/blueocean_mcp/vector_store.py, _PROJECT_NAME_RE),
# copied because this module must not import the server; tests/hermes_logic.py fails if
# the two drift apart.
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


def valid_project_name(name: str) -> bool:
    return bool(PROJECT_NAME_RE.match(name))


def suggest_project_name(name: str) -> str | None:
    """A valid name close to ``name`` (the server's own suggestion), or None."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:100]
    return slug if valid_project_name(slug) else None


def alias_map(projects: dict[str, list[str]]) -> dict[str, str]:
    """Every name that detects a project (the project itself and its aliases) -> project.

    A project the server would reject is skipped: selecting it would make every search
    and Turn Trace for the session fail quietly.
    """
    names: dict[str, str] = {}
    for project, aliases in projects.items():
        if not valid_project_name(project):
            continue
        names[project] = project
        for alias in aliases:
            names.setdefault(alias, project)
    return names


def match_alias(message: str | None, names: dict[str, str]) -> str | None:
    """The project whose longest name or alias appears in ``message``, or None."""
    text = message or ""
    best: str | None = None
    for name in sorted(names):
        if not name:
            continue
        pattern = rf"(?<!{_NAME_CHAR}){re.escape(name)}(?!{_NAME_CHAR})"
        if re.search(pattern, text, re.IGNORECASE) and (best is None or len(name) > len(best)):
            best = name
    return names[best] if best is not None else None


def match_project(message: str | None, known: Iterable[str], ignore: Sequence[str]) -> str | None:
    """The longest known project named in ``message``, or None."""
    names = {n: n for n in known if n and not any(fnmatch.fnmatchcase(n, g) for g in ignore)}
    return match_alias(message, names)


@dataclass
class _SessionState:
    project: str | None = None
    seen: set[str] = field(default_factory=set)


class ProjectTracker:
    """Active project and already-injected entry ids, per session key."""

    def __init__(self) -> None:
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

    def _state(self, key: str) -> _SessionState:
        return self._states.setdefault(key, _SessionState())

    def active(self, key: str) -> str | None:
        with self._lock:
            state = self._states.get(key)
            return state.project if state else None

    def set_active(self, key: str, project: str) -> None:
        with self._lock:
            self._state(key).project = project

    def observe(self, key: str, message: str | None, known: Iterable[str],
                ignore: Sequence[str], *, allowed: dict[str, str] | None = None) -> str | None:
        """Switch to a project named in ``message``. With ``allowed`` (an
        :func:`alias_map`), only those names and aliases count; ``known`` and
        ``ignore`` apply otherwise."""
        hit = (match_alias(message, allowed) if allowed is not None
               else match_project(message, known, ignore))
        with self._lock:
            state = self._state(key)
            if hit:
                state.project = hit
            return state.project

    def seen(self, key: str) -> set[str]:
        with self._lock:
            state = self._states.get(key)
            return set(state.seen) if state else set()

    def mark_seen(self, key: str, ids: Iterable[str]) -> None:
        with self._lock:
            self._state(key).seen.update(ids)

    def switch(self, old_key: str, new_key: str, *, carry_project: bool) -> None:
        """Rebind after Hermes reassigns the session id, or rewinds it.

        Earlier injections are gone from context either way, so ``seen`` is
        cleared. The project carries over unless this is a new conversation.
        """
        with self._lock:
            old = self._states.get(old_key)
            new = self._states.get(new_key) or _SessionState()
            if not carry_project:
                new.project = None
            elif old is not None and old.project:
                new.project = old.project
            new.seen = set()
            if old_key != new_key:
                self._states.pop(old_key, None)
            self._states[new_key] = new


class ProjectCatalog:
    """Known project names, fetched at most once per ``ttl`` seconds.

    A failed refresh keeps the previous list: one unreachable call must not
    make every project unknown for the next five minutes.
    """

    def __init__(self, fetch: Callable[[], list[str]], ttl: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._names: list[str] | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        with self._lock:
            now = self._clock()
            if self._names is not None and now - self._fetched_at < self._ttl:
                return self._names
            try:
                self._names = sorted(self._fetch())
            except Exception:  # noqa: BLE001 - any fetch failure keeps the old list
                if self._names is None:
                    return []
            self._fetched_at = now
            return self._names
