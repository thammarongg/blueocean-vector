"""BlueOcean Vector as a Hermes MemoryProvider.

For the active provider, Hermes itself calls prefetch() before every turn and
sync_turn() after it. This turns recall of shared project memory into
something that happens every turn instead of something the agent has to
remember to do. Explicit writes stay with the `blueocean` MCP server, so the
only tool this provider adds is blueocean_set_project, to correct detection.

Wiring only. The logic lives in sibling modules that import nothing from
Hermes: config (settings), text (cleaning and formatting), project
(detection), bridge (the MCP session).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from agent.memory_provider import (
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from agent.secret_scope import get_secret
from hermes_constants import hermes_home_key

from .config import DEFAULTS, ignore_globs, load_config, save_config
from .project import (
    ProjectCatalog,
    ProjectTracker,
    alias_map,
    suggest_project_name,
    valid_project_name,
)
from .text import (
    clean_query,
    clean_turn_side,
    format_recall,
    trace_content,
    trace_summary,
    unseen,
)

# .bridge imports mcp, so it loads lazily: without mcp this package must still
# import, so Hermes finds the provider and reports it unavailable instead of
# "not found".
if TYPE_CHECKING:
    from .bridge import Bridge

logger = logging.getLogger(__name__)

TOKEN_ENV = "BLUEOCEAN_AUTH_TOKEN"
TRACE_KIND = "hermes_turn"
QUERY_LIMIT = 1000
SHUTDOWN_DRAIN_S = 5.0
# A prefetch can make two calls (catalog refresh, then search) inside Hermes's
# 8 s prefetch budget; the refresh gets the smaller share.
CATALOG_TIMEOUT_S = 2.0

SET_PROJECT_SCHEMA = {
    "name": "blueocean_set_project",
    "description": (
        "Set which BlueOcean project this conversation is about. Project memory is "
        "recalled automatically for the project named in the conversation; call this "
        "only when it picked the wrong one or none."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Project name, usually the repository folder name.",
            },
        },
        "required": ["project"],
    },
}

# One bridge per (url, token) for the whole process: the gateway runs many
# agents, and each would otherwise hold its own thread and connection.
_BRIDGES: dict[tuple, Bridge] = {}
_BRIDGES_LOCK = threading.Lock()


def _spawn(target, name: str) -> threading.Thread:
    return spawn_context_thread(target, name=name)


def _bridge_for(url: str, token: str) -> Bridge:
    from .bridge import Bridge

    with _BRIDGES_LOCK:
        bridge = _BRIDGES.get((url, token))
        if bridge is None:
            bridge = Bridge(url, token, spawn=_spawn)
            _BRIDGES[(url, token)] = bridge
        return bridge


class BlueOceanMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._home_key = ""
        self._session_id = ""
        self._agent_context = "primary"
        self._platform = ""
        self._config: dict[str, Any] = dict(DEFAULTS)
        self._bridge: Bridge | None = None
        self._catalog: ProjectCatalog | None = None
        self._tracker = ProjectTracker()
        self._cwd_project: str | None = None
        self._last_status: RecallStatus | None = None

    @property
    def name(self) -> str:
        return "blueocean"

    # -- availability and setup -------------------------------------------

    @staticmethod
    def _token() -> str:
        try:
            return (get_secret(TOKEN_ENV, "") or "").strip()
        except Exception:  # noqa: BLE001 - any secret-lookup failure means "no token"
            return ""

    @staticmethod
    def _mcp_importable() -> bool:
        try:
            import mcp.client.streamable_http  # noqa: F401
        except ImportError:
            return False
        return True

    def is_available(self) -> bool:
        return self._mcp_importable() and bool(self._token())

    def unavailable_reason(self) -> str:
        if not self._mcp_importable():
            return "the 'mcp' package is not installed in Hermes's environment"
        if not self._token():
            return f"{TOKEN_ENV} is not set (add it to Hermes's .env)"
        return ""

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "url", "description": "BlueOcean MCP endpoint", "default": DEFAULTS["url"]},
            {"key": "auth_token", "description": "Bearer token for the BlueOcean server",
             "secret": True, "required": True, "env_var": TOKEN_ENV},
            {"key": "prefetch_max_tokens", "description": "Token budget for per-turn recall",
             "type": "integer", "default": DEFAULTS["prefetch_max_tokens"],
             "minimum": 100, "maximum": 2000, "step": 50},
            {"key": "ignore_projects",
             "description": "Comma-separated globs never auto-detected as the project",
             "default": DEFAULTS["ignore_projects"]},
            {"key": "sync_turns", "description": "Store each turn as a Turn Trace",
             "type": "boolean", "default": DEFAULTS["sync_turns"]},
            {"key": "sync_turns_platforms",
             "description": "Comma-separated platforms whose turns are stored (e.g. cli,telegram)",
             "default": ",".join(DEFAULTS["sync_turns_platforms"])},
            {"key": "projects",
             "description": ("Projects that may be auto-detected, with aliases, e.g. "
                             "'blueocean-vector:blueocean|bo, tidepool' (empty = any project)"),
             "default": ""},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        save_config(values, hermes_home)

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        home = str(kwargs.get("hermes_home") or "")
        self._home_key = hermes_home_key(home or None)
        self._session_id = session_id or ""
        self._agent_context = kwargs.get("agent_context") or "primary"
        self._platform = str(kwargs.get("platform") or "")
        self._config = load_config(home) if home else dict(DEFAULTS)
        # No network here: Hermes startup must never wait on BlueOcean.
        self._bridge = _bridge_for(self._config["url"], self._token())
        self._catalog = ProjectCatalog(self._fetch_projects)
        cwd = kwargs.get("cwd")
        self._cwd_project = os.path.basename(os.path.normpath(cwd)) if cwd else None

    def system_prompt_block(self) -> str:
        return ("BlueOcean project memory is recalled automatically for the project named "
                "in the conversation. If it recalls the wrong project, call "
                "blueocean_set_project.")

    def shutdown(self) -> None:
        if self._bridge is not None and not self._bridge.drain(SHUTDOWN_DRAIN_S):
            logger.warning("BlueOcean: unsent Turn Traces dropped at shutdown")

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        old_key = self._key("")
        self._session_id = new_session_id or self._session_id
        self._tracker.switch(old_key, self._key(""), carry_project=not reset)

    # -- recall ----------------------------------------------------------------

    def _key(self, session_id: str) -> str:
        return f"{self._home_key}\0{session_id or self._session_id}"

    def _fetch_projects(self) -> list[str]:
        result = self._bridge.call("memory_list_projects", {}, timeout=CATALOG_TIMEOUT_S)
        return list(result.get("projects", [])) if isinstance(result, dict) else []

    def _resolve_project(self, key: str, query: str) -> str | None:
        cwd = self._cwd_project
        if self._config["projects"]:
            # Allowlist: only listed projects and their aliases; the store is not consulted.
            allowed = alias_map(self._config["projects"])
            active = self._tracker.observe(key, query, (), (), allowed=allowed)
            default = allowed.get(cwd.lower()) if cwd else None
        else:
            known = self._catalog.names()
            ignore = ignore_globs(self._config["ignore_projects"])
            active = self._tracker.observe(key, query, known, ignore)
            default = (cwd if cwd and cwd in known
                       and not any(fnmatch.fnmatchcase(cwd, glob) for glob in ignore) else None)
        if active is None and default:
            self._tracker.set_active(key, default)
            active = default
        return active

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_status = None
        if self._bridge is None or is_trivial_prompt(query):
            return ""
        from .bridge import BridgeUnavailable  # already loaded: a bridge exists

        key = self._key(session_id)
        try:
            project = self._resolve_project(key, query)
            if not project:
                return ""
            result = self._bridge.call("memory_search", {
                "project": project,
                # Redacted: with a remote embedding provider the query leaves the machine.
                "query": clean_query(query, QUERY_LIMIT),
                "max_tokens": self._config["prefetch_max_tokens"],
            })
            if not isinstance(result, dict):
                return ""
            summaries, fulls = unseen(result, self._tracker.seen(key))
            if not summaries:
                return ""
            text = format_recall(project, summaries, fulls)
            self._tracker.mark_seen(key, [str(rid) for r in summaries + fulls
                                          if (rid := r.get("id")) is not None])
            status = RecallStatus(provider_label=f"BlueOcean ({project})", count=len(summaries))
        except BridgeUnavailable as exc:
            logger.debug("BlueOcean prefetch skipped: %s", exc)
            return ""
        except Exception:
            logger.warning("BlueOcean prefetch failed", exc_info=True)
            return ""
        self._last_status = status
        return text

    def recall_status(self) -> RecallStatus | None:
        return self._last_status

    # -- Turn Trace --------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list[dict[str, Any]] | None = None) -> None:
        try:
            if (self._bridge is None or self._agent_context != "primary"
                    or not self._config["sync_turns"]
                    or (self._platform or "cli").lower() not in self._config["sync_turns_platforms"]):
                return
            project = self._tracker.active(self._key(session_id))
            if not project:
                return
            user = clean_turn_side(user_content)
            assistant = clean_turn_side(assistant_content)
            if not user and not assistant:
                return
            self._bridge.submit("memory_store", {
                "project": project,
                "area": "hermes",
                "module": "turns",
                "content": trace_content(user, assistant),
                "summary": trace_summary(user, assistant),
                "importance": 1,
                "metadata": {"kind": TRACE_KIND, "session_id": session_id or self._session_id,
                             "platform": self._platform},
            })
        except Exception:
            logger.warning("BlueOcean sync_turn failed", exc_info=True)

    # -- tools -------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [SET_PROJECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name != "blueocean_set_project":
            return json.dumps({"error": f"unknown tool {tool_name}"})
        project = str((args or {}).get("project") or "").strip()
        if not project:
            return json.dumps({"error": "project is required"})
        # Server project names are lowercase; take the catalog's spelling when it has one.
        known = self._catalog.names() if self._catalog else []
        match = next((name for name in known if name.lower() == project.lower()), None)
        project = match or project.lower()
        if not valid_project_name(project):
            # Setting it would make every later recall and Turn Trace fail quietly.
            error: dict[str, Any] = {
                "error": (f"invalid project name {project!r}: use lowercase letters, digits, "
                          "'-' and '_', starting with a letter or digit, at most 100 chars"),
            }
            suggestion = suggest_project_name(project)
            if suggestion:
                error["suggestion"] = suggestion
            return json.dumps(error)
        has_memory = match is not None
        self._tracker.set_active(self._key(kwargs.get("session_id", "")), project)
        reply: dict[str, Any] = {"ok": True, "project": project, "has_memory": has_memory}
        if not has_memory:
            reply["note"] = "No memory stored for this project yet; recall stays empty until something is."
        return json.dumps(reply)
