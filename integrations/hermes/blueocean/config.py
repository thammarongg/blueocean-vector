"""The provider's settings: defaults, the on-disk file, and parsing.

Imports nothing from Hermes, so its tests run without a Hermes install. The
auth token is never stored here: it is a secret, and Hermes keeps secrets in
its .env, read through agent.secret_scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_FILE = "blueocean.json"

DEFAULTS: dict[str, Any] = {
    "url": "http://localhost:8765/mcp",
    "prefetch_max_tokens": 600,
    "ignore_projects": "*test*,*-project,probe-*,*-probe",
    "sync_turns": True,
    # Projects that may be auto-detected, each with its aliases. Empty = any project in
    # the store except ignore_projects; once set, only these (and their aliases).
    "projects": {},
    # Platforms whose turns are stored as Turn Traces: Hermes's local sessions (terminal,
    # TUI, desktop chat panel, editor ACP). Chat apps such as discord and telegram are
    # left out: casual and personal talk does not belong in the store every agent reads.
    "sync_turns_platforms": ["cli", "tui", "desktop", "acp"],
}

_MIN_TOKENS, _MAX_TOKENS = 100, 2000
_TRUE = {"1", "true", "yes", "on"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _clamp_tokens(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULTS["prefetch_max_tokens"]
    return max(_MIN_TOKENS, min(_MAX_TOKENS, n))


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_config(hermes_home: str | Path) -> dict[str, Any]:
    """Settings for one Hermes home: the file's known keys over the defaults."""
    cfg = dict(DEFAULTS)
    raw = _read(Path(hermes_home) / CONFIG_FILE)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS and v is not None})
    cfg["prefetch_max_tokens"] = _clamp_tokens(cfg["prefetch_max_tokens"])
    cfg["sync_turns"] = _as_bool(cfg["sync_turns"])
    cfg["projects"] = parse_projects(cfg["projects"])
    cfg["sync_turns_platforms"] = parse_platforms(cfg["sync_turns_platforms"])
    return cfg


def save_config(values: dict[str, Any], hermes_home: str | Path) -> None:
    """Merge the known, non-secret keys of ``values`` into the settings file."""
    path = Path(hermes_home) / CONFIG_FILE
    current = _read(path)
    current.update({k: v for k, v in (values or {}).items() if k in DEFAULTS})
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".json.tmp")
    staged.write_text(json.dumps(current, indent=2), encoding="utf-8")
    staged.replace(path)


def ignore_globs(value: str | list[str] | None) -> list[str]:
    """``ignore_projects`` as a list, from either a comma string or a list."""
    parts = value.split(",") if isinstance(value, str) else list(value or [])
    return [str(p).strip() for p in parts if str(p).strip()]


def parse_platforms(value: Any) -> list[str]:
    """``sync_turns_platforms`` as lowercase names, from a comma string or a list.

    Anything else falls back to the default, so a typo such as ``true`` cannot take the
    provider down (this runs in ``initialize``).
    """
    if not isinstance(value, (str, list)):
        return list(DEFAULTS["sync_turns_platforms"])
    return [p.lower() for p in ignore_globs(value)]


def _names(value: Any) -> list[str]:
    parts = value.split("|") if isinstance(value, str) else value
    if not isinstance(parts, list):
        return []
    return [p.strip().lower() for p in parts if isinstance(p, str) and p.strip()]


def parse_projects(value: Any) -> dict[str, list[str]]:
    """``projects`` as {project: [aliases]}, lowercased.

    JSON holds a dict (aliases as a list or an ``a|b`` string) or a plain list of
    projects. ``hermes memory setup`` can only collect text, so
    ``"blueocean-vector:blueocean|bo, tidepool"`` works too. A project listed twice
    keeps the aliases of both entries. Anything else means no allowlist.
    """
    if isinstance(value, str):
        pairs = [item.partition(":")[::2] for item in value.split(",")]
    elif isinstance(value, list):
        pairs = [(item, []) for item in value if isinstance(item, str)]
    elif isinstance(value, dict):
        pairs = list(value.items())
    else:
        return {}
    projects: dict[str, list[str]] = {}
    for name, aliases in pairs:
        key = str(name).strip().lower()
        if not key:
            continue
        merged = projects.setdefault(key, [])
        merged.extend(a for a in _names(aliases) if a not in merged)
    return projects
