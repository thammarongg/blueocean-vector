"""Server-derived session identity.

The 2026-07-28 MCP spec removed protocol sessions and the Mcp-Session-Id
header, so a modern client no longer tells the server which session a call
belongs to. Asking the agent to carry a handle works only when the agent
complies, and issue #8 measured that it mostly does not. So the server derives
a Session itself from what every HTTP request already carries:

    (agent name, agent version, caller fingerprint, project)

and starts a new one when that key has been idle longer than the threshold.
CONTEXT.md defines a Session as one agent on one Project, which is why the
project is part of the key.

The honest ceiling: two windows of the same agent version, from the same
caller, on the same project, are indistinguishable here and merge into one
session.

Everything is in memory. A restart forgets every key, which is correct: a
restart ends every session anyway.
"""

import hashlib
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping

from ..config import SESSION_IDLE_SECONDS

SessionKey = tuple[str, str, str, str]

DEFAULT_MAX_KEYS = 10_000


class SessionTracker:
    """Maps a session key to its current session id, renewed after an idle gap.

    Bounded: past ``max_keys`` the least recently seen key is dropped, so a
    flood of distinct callers costs a fixed amount of memory. A dropped key
    that comes back simply starts a new session.
    """

    def __init__(
        self,
        idle_seconds: float,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle = idle_seconds
        self._max = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (session id, last seen)
        self._seen: OrderedDict[SessionKey, tuple[str, float]] = OrderedDict()

    def session_for(self, key: SessionKey) -> str:
        now = self._clock()
        with self._lock:
            current = self._seen.get(key)
            if current is not None and now - current[1] <= self._idle:
                session_id = current[0]
            else:
                session_id = uuid.uuid4().hex
            self._seen[key] = (session_id, now)
            self._seen.move_to_end(key)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return session_id

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


def caller_fingerprint(
    headers: Mapping[str, str] | None, query_token: str | None, remote: str | None
) -> str:
    """Who is calling, without ever keeping the credential.

    A bearer token (the Authorization header, or the ``?token=`` query param
    that Kiro needs) is reduced to a short SHA-256 prefix; the same token by
    either route gives the same fingerprint. The address is always part of
    it: the server has one shared token, so with auth on the token alone is
    the same for every caller and would merge two machines into one session.
    A client whose address changes mid-session is split instead, which is
    the cheap direction to be wrong (issue #6).
    """
    token = query_token
    auth = (headers or {}).get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip() or token
    addr = f"addr:{remote}" if remote else ""
    if token:
        digest = "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
        return f"{digest}@{addr}" if addr else digest
    return addr or "unknown"


_tracker: SessionTracker | None = None
_tracker_lock = threading.Lock()


def get_tracker() -> SessionTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = SessionTracker(idle_seconds=SESSION_IDLE_SECONDS)
        return _tracker
