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
