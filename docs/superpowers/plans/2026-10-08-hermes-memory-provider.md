# Hermes Memory Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hermes recall BlueOcean project memory automatically every turn, and record each turn as a Turn Trace, by shipping BlueOcean as a Hermes `MemoryProvider` plugin.

**Architecture:** The plugin lives in `integrations/hermes/blueocean/` and is symlinked into `$HERMES_HOME/plugins/blueocean/`.
- Four pure modules, testable without Hermes:
  - `config.py`: settings
  - `text.py`: cleaning and formatting
  - `project.py`: project detection
  - `bridge.py`: a sync facade over one long-lived async MCP session
- A thin `__init__.py` wires them into Hermes's ABC.

Server side, entries whose `kind` is in `BLUEOCEAN_TRACE_KINDS` are excluded from every search that does not name a `kind`, and `blueocean-admin prune` gains `--kind`.

**Tech Stack:**
- Python 3.12+ (repo) and Python 3.14 (Hermes runtime)
- `mcp==2.0.0` with `httpx2` (streamable HTTP client)
- Qdrant via the existing server
- Hermes `agent.memory_provider` ABC

**Spec:** `docs/superpowers/specs/2026-10-08-hermes-memory-provider-design.md`

## Global Constraints

- Tests are standalone scripts, not pytest: `uv run python -m tests.<name>`, each with `if __name__ == "__main__":`. They need the local Qdrant running (`docker compose up -d qdrant`).
- `config.py`, `text.py`, `project.py`, `bridge.py` import nothing from Hermes and no sibling plugin module. Only `__init__.py` imports Hermes and the siblings.
- The plugin may depend only on the stdlib and `mcp` (which brings `httpx2`). Both exist in the Hermes runtime.
- Credentials: `agent.secret_scope.get_secret("BLUEOCEAN_AUTH_TOKEN")`, never `os.environ`, inside the plugin.
- Background threads in the plugin start via Hermes's `spawn_context_thread` (injected into `Bridge` as `spawn`).
- Turn Trace literals:
  - `kind="hermes_turn"`
  - `area="hermes"`
  - `module="turns"`
  - `importance=1`
- Defaults:
  - url `http://localhost:8765/mcp`
  - `prefetch_max_tokens` 600, clamped to 100-2000
  - `ignore_projects` `*test*,*-project,probe-*,*-probe`
  - `sync_turns` true
- Limits:
  - query 1000 chars
  - trace side 2000 chars
  - summary side 150 chars
- Timings and sizes:
  - call timeout 5 s (catalog refresh 2 s, so refresh plus search stay under Hermes's 8 s prefetch budget)
  - breaker cooldown 30 s
  - write queue 200
  - project catalog TTL 300 s
  - shutdown drain 5 s
- `BLUEOCEAN_TRACE_KINDS` is comma-separated, default `hermes_turn`. An empty value disables trace exclusion.
- Commit messages: imperative sentence subject in this repo's style (e.g. "Exclude trace kinds from default search"), no `feat:` prefix. End with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Lint before each commit: `uv run ruff check src tests integrations`.

## Review Focus

1. **Thai text glued to a name or secret.** Python's `\b` treats Thai letters as word characters, so `ดูblueocean-vector` and `คีย์คือsk-...` must still match and redact (Tasks 3 and 4).
2. **BlueOcean stopped while Hermes is chatting.** A turn must not wait 5 s every time. After one failure, calls must return immediately until the cooldown ends (Task 5).
3. **Docker rebuild or restart mid-session.** The old MCP session id is dead. The next call must reconnect transparently instead of failing until Hermes restarts (Task 5).
4. **Recalled memory echoed into the trace.** `<memory-context>` blocks in the user/assistant text must never be stored back (Tasks 3 and 6).
5. **The same entries re-injected every turn.** A repeated query in the same session must inject nothing new (Task 6).

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `src/blueocean_mcp/config.py` | add `TRACE_KINDS` |
| `src/blueocean_mcp/vector_store.py` | default-exclude trace kinds in `search` |
| `src/blueocean_mcp/tools.py` | `memory_search` docstring mentions trace kinds |
| `src/blueocean_mcp/admin.py` | `prune --kind` |
| `docker-compose.yml`, `.env.example` | pass/document `BLUEOCEAN_TRACE_KINDS` |
| `integrations/hermes/blueocean/config.py` | plugin settings file, defaults, parsing |
| `integrations/hermes/blueocean/text.py` | fence stripping, redaction, clipping, trace text, recall formatting, dedup |
| `integrations/hermes/blueocean/project.py` | name matching, per-session tracker, cached project catalog |
| `integrations/hermes/blueocean/bridge.py` | breaker, sync `call`/`submit`/`drain` over one async MCP session |
| `integrations/hermes/blueocean/__init__.py` | `BlueOceanMemoryProvider(MemoryProvider)` |
| `integrations/hermes/blueocean/plugin.yaml` | plugin manifest |
| `scripts/install_hermes_plugin.sh` | symlink into `$HERMES_HOME/plugins/` |
| `tests/_hermes.py` | load plugin modules by path; start/stop a real HTTP server |
| `tests/trace_kinds.py` | server: default exclusion, override, `prune --kind` |
| `tests/hermes_logic.py` | pure plugin logic |
| `tests/hermes_bridge.py` | bridge against a real server |
| `tests/hermes_contract.py` | provider loaded through Hermes's own discovery |
| `tests/install_hermes_plugin.py` | installer behavior |
| `CONTEXT.md`, `README.md` | Turn Trace term; config, admin, Hermes section, tests list |

---

### Task 1: Exclude trace kinds from default search

**Files:**
- Modify: `src/blueocean_mcp/config.py` (append after `DEFAULT_TOP_K`)
- Modify: `src/blueocean_mcp/vector_store.py:19-26` (import), `search()` (around line 268)
- Modify: `src/blueocean_mcp/tools.py` (`memory_search` docstring)
- Modify: `docker-compose.yml` (environment block), `.env.example`, `CONTEXT.md`, `README.md` (Configuration)
- Create: `tests/trace_kinds.py`

**Interfaces:**
- Consumes: existing `VectorStore.search(..., kind, exclude_kinds)` and `_scope_filter`.
- Produces: `blueocean_mcp.config.TRACE_KINDS: tuple[str, ...]`. Behavior: `memory_search` without `kind` never returns entries whose `kind` is in `TRACE_KINDS`.

- [ ] **Step 1: Write the failing test**

Create `tests/trace_kinds.py`:

```python
"""Trace kinds stay out of every search that does not ask for them.

A Turn Trace is written every Hermes turn. Claude Code, Codex and Cursor call
memory_search without exclude_kinds, so if the exclusion were left to the
caller, traces would crowd the real entries out of their token budget. The
server therefore excludes the kinds in BLUEOCEAN_TRACE_KINDS whenever the
caller does not name a kind, and still returns them when it does.

Run with:
    uv run python -m tests.trace_kinds
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._helpers import BIN, reset_project

PROJECT = "trace-kinds-project"
QUERY = {"project": PROJECT, "query": "which cache did we pick, redis or memcached"}


def result_json(res) -> dict:
    return json.loads(res.content[0].text)


def ids(result: dict) -> set[str]:
    return {row["id"] for row in result["summary"]}


async def open_session(stack: AsyncExitStack, trace_kinds: str | None) -> ClientSession:
    env = dict(os.environ)
    env.pop("BLUEOCEAN_TRACE_KINDS", None)
    if trace_kinds is not None:
        env["BLUEOCEAN_TRACE_KINDS"] = trace_kinds
    params = StdioServerParameters(command=BIN, args=[], cwd="/", env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = ClientSession(read, write)
    await stack.enter_async_context(session)
    await session.initialize()
    return session


async def store(session: ClientSession, summary: str, kind: str | None) -> str:
    args = {
        "project": PROJECT,
        "area": "backend",
        "module": "cache",
        "content": f"{summary}. We compared redis and memcached for the cache.",
        "summary": summary,
        "importance": 3,
    }
    if kind is not None:
        args["metadata"] = {"kind": kind}
    res = await session.call_tool("memory_store", args)
    return res.content[0].text


async def search(session: ClientSession, **extra) -> set[str]:
    return ids(result_json(await session.call_tool("memory_search", {**QUERY, **extra})))


async def default_kinds() -> dict[str, str]:
    """With the default setting, hermes_turn is a trace kind."""
    async with AsyncExitStack() as stack:
        session = await open_session(stack, None)
        seeded = {
            "plain": await store(session, "Chose redis for the cache", None),
            "summary": await store(session, "Session settled on redis", "session_summary"),
            "trace": await store(session, "User asked about redis -> picked it", "hermes_turn"),
        }

        print("== default search hides the trace, keeps everything else ==")
        found = await search(session)
        assert seeded["trace"] not in found, "a hermes_turn trace leaked into default search"
        assert seeded["plain"] in found, "an entry without a kind was dropped"
        assert seeded["summary"] in found, "a session summary was dropped"
        print(f"  {len(found)} entries, no trace")

        print("== kind='hermes_turn' still reaches the trace ==")
        only = await search(session, kind="hermes_turn")
        assert only == {seeded["trace"]}, f"expected only the trace, got {only}"
        print("  only the trace came back")

        print("== caller exclusions compose with trace exclusion ==")
        found = await search(session, exclude_kinds=["session_summary"])
        assert found == {seeded["plain"]}, f"expected only the plain entry, got {found}"
        print("  plain entry only")
        return seeded


async def overridden_kinds(seeded: dict[str, str]) -> None:
    """BLUEOCEAN_TRACE_KINDS replaces the default list."""
    async with AsyncExitStack() as stack:
        session = await open_session(stack, "custom_trace")
        custom = await store(session, "Custom trace about the redis cache", "custom_trace")

        print("== an overridden list hides its own kinds instead ==")
        found = await search(session)
        assert custom not in found, "custom_trace should be hidden when configured"
        assert seeded["trace"] in found, "hermes_turn is no longer a trace kind here"
        print("  custom_trace hidden, hermes_turn visible")

    async with AsyncExitStack() as stack:
        session = await open_session(stack, "")
        print("== an empty list disables trace exclusion ==")
        found = await search(session)
        assert seeded["trace"] in found and custom in found
        print("  everything visible")


async def main() -> None:
    reset_project(PROJECT)
    seeded = await default_kinds()
    await overridden_kinds(seeded)
    print("\nTRACE KINDS TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m tests.trace_kinds`
Expected: FAIL with `AssertionError: a hermes_turn trace leaked into default search`.

- [ ] **Step 3: Add `TRACE_KINDS` to the server config**

In `src/blueocean_mcp/config.py`, after the `DEFAULT_TOP_K = ...` line, add:

```python

# Kinds of record written mechanically, every turn or every session, rather
# than because someone decided something. They are kept out of every search
# that does not name a kind: most callers (Claude Code, Codex, Cursor) never
# pass exclude_kinds, and left to them a trace would crowd real entries out of
# the token budget. Asking with kind=<one of these> still reaches them. An
# empty value turns the exclusion off.
TRACE_KINDS = tuple(
    k.strip() for k in os.getenv("BLUEOCEAN_TRACE_KINDS", "hermes_turn").split(",") if k.strip()
)
```

- [ ] **Step 4: Apply the exclusion in `VectorStore.search`**

In `src/blueocean_mcp/vector_store.py`, add `TRACE_KINDS` to the config import:

```python
from .config import (
    DEFAULT_COLLECTION_PREFIX,
    DEFAULT_HNSW_EF_CONSTRUCT,
    DEFAULT_HNSW_M,
    DEFAULT_QDRANT_URL,
    MAX_CONTENT_CHARS,
    MAX_TOP_K,
    TRACE_KINDS,
)
```

In `search()`, directly after `self._ensure_collection(project)`, add:

```python
        # Traces are only ever returned to a caller that names their kind.
        if not kind:
            exclude_kinds = [*(exclude_kinds or []), *TRACE_KINDS]
```

- [ ] **Step 5: Document it in the tool docstring**

In `src/blueocean_mcp/tools.py`, extend the `memory_search` docstring paragraph that ends with "``kind`` are never dropped by ``exclude_kinds``." by appending:

```
        Trace kinds (``BLUEOCEAN_TRACE_KINDS``, default ``hermes_turn``) are
        excluded whenever ``kind`` is not given; pass ``kind="hermes_turn"``
        to read them.
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run python -m tests.trace_kinds`
Expected: ends with `TRACE KINDS TEST PASSED`.

Also run the existing kind test to confirm nothing regressed: `uv run python -m tests.memory_kind`. Expected: passes.

- [ ] **Step 7: Wire config and docs**

In `docker-compose.yml`, in the `blueocean-mcp` `environment:` list after `BLUEOCEAN_PRICING_FILE`, add:

```yaml
      # Kinds hidden from searches that don't name them (comma-separated).
      - BLUEOCEAN_TRACE_KINDS=${BLUEOCEAN_TRACE_KINDS:-hermes_turn}
```

In `.env.example`, after the `BLUEOCEAN_TOP_K=20` line, add:

```

# Record kinds hidden from every search that does not ask for them by kind=.
# Hermes writes one hermes_turn entry per turn. Empty = no exclusion.
# BLUEOCEAN_TRACE_KINDS=hermes_turn
```

In `CONTEXT.md`, under `### Records of a session`, after the **Session Trace** entry, add:

```markdown
**Turn Trace**:
A record of one Hermes turn, written by the client after the turn: what the
user said and what the assistant answered, cleaned and clipped. A
client-written relative of the Session Trace. It says what was said, never
what was decided, and is excluded from every search that does not ask for its
kind.
_Avoid_: turn log, transcript, chat history
```

In `README.md` `## Configuration`, after the `BLUEOCEAN_AUTH_TOKEN` bullet, add:

```markdown
- `BLUEOCEAN_TRACE_KINDS` — record kinds kept out of every search that doesn't name them with `kind=` (default `hermes_turn`, comma-separated, empty disables). Traces are written mechanically every turn; without this they would crowd real entries out of other agents' search budget.
```

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src tests
git add src/blueocean_mcp/config.py src/blueocean_mcp/vector_store.py src/blueocean_mcp/tools.py docker-compose.yml .env.example CONTEXT.md README.md tests/trace_kinds.py
git commit -m "Exclude trace kinds from default search

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `blueocean-admin prune --kind`

**Files:**
- Modify: `src/blueocean_mcp/admin.py` (`cmd_prune`, `p_prune` args, module docstring)
- Modify: `tests/trace_kinds.py` (add a prune section)
- Modify: `README.md` (Admin CLI block)

**Interfaces:**
- Consumes: `cmd_prune(args)` with `args.project`, `args.older_days`, `args.max_importance`, `args.dry_run`. Existing callers (`tests/telemetry_cli.py`) build args without `kind`, so read it with `getattr(args, "kind", None)`.
- Produces: `args.kind: str | None`. With `--kind` alone, every entry of that kind is pruned. Combined with other filters, all conditions must hold.

- [ ] **Step 1: Write the failing test**

In `tests/trace_kinds.py`, add these imports at the top with the others:

```python
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from blueocean_mcp import admin
from blueocean_mcp.config import DEFAULT_QDRANT_URL
from blueocean_mcp.vector_store import collection_name
```

Add this function above `main()`:

```python
def count_kind(kind: str | None) -> int:
    """Points of one kind (None = points with no kind key at all)."""
    client = QdrantClient(url=DEFAULT_QDRANT_URL)
    if kind is None:
        flt = qmodels.Filter(must=[qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="kind"))])
    else:
        flt = qmodels.Filter(must=[qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind))])
    return client.count(collection_name(PROJECT), count_filter=flt, exact=True).count


def prune_by_kind() -> None:
    """--kind removes only that kind, and works without any other filter."""
    print("== prune --kind hermes_turn removes traces only ==")
    before_plain = count_kind(None)
    assert count_kind("hermes_turn") == 1 and before_plain >= 1

    class _Args:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # The audit row goes to a telemetry server this test does not run; its
    # failure path is covered in tests/telemetry_cli.py.
    original = admin._report_audit
    admin._report_audit = lambda row: True
    try:
        admin.cmd_prune(_Args(project=PROJECT, older_days=None, max_importance=None,
                              kind="hermes_turn", dry_run=False))
    finally:
        admin._report_audit = original

    assert count_kind("hermes_turn") == 0, "the trace survived prune --kind"
    assert count_kind(None) == before_plain, "prune --kind deleted unkinded entries"
    assert count_kind("session_summary") == 1, "prune --kind deleted another kind"
    assert count_kind("custom_trace") == 1, "prune --kind deleted another kind"
    print("  traces gone, everything else intact")
```

Change `main()` to:

```python
async def main() -> None:
    reset_project(PROJECT)
    seeded = await default_kinds()
    await overridden_kinds(seeded)
    prune_by_kind()
    print("\nTRACE KINDS TEST PASSED")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m tests.trace_kinds`
Expected: FAIL. `cmd_prune` ignores `kind` and prints `Nothing to prune: provide --older-days and/or --max-importance.`, then `AssertionError: the trace survived prune --kind`.

- [ ] **Step 3: Implement `--kind`**

In `src/blueocean_mcp/admin.py` `cmd_prune`, after `max_importance = args.max_importance`, add:

```python
    kind = getattr(args, "kind", None)
```

After the `if max_importance is not None:` block (before `if not must:`), add:

```python
    if kind:
        must.append(
            qmodels.FieldCondition(key="kind", match=qmodels.MatchValue(value=kind))
        )
```

Change the guard message to:

```python
    if not must:
        print("Nothing to prune: provide --older-days, --max-importance and/or --kind.")
        return
```

In the argument parser, after `p_prune.add_argument("--max-importance", ...)`, add:

```python
    p_prune.add_argument("--kind", default=None,
                         help="only entries of this kind (e.g. hermes_turn)")
```

In the module docstring, change the prune line to:

```
    prune <project> [--older-days N] [--max-importance I] [--kind K] [--dry-run]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.trace_kinds`
Expected: `TRACE KINDS TEST PASSED`.

Run: `uv run python -m tests.telemetry_cli`
Expected: passes (an `_Args` without `kind` still works).

- [ ] **Step 5: Document**

In `README.md` `## Admin CLI`, after the existing `prune` line, add:

```bash
uv run blueocean-admin prune <project> --kind hermes_turn --older-days 30 [--dry-run]
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/blueocean_mcp/admin.py tests/trace_kinds.py README.md
git commit -m "Let prune target one kind of record

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Plugin settings and text handling

**Files:**
- Create: `integrations/hermes/blueocean/config.py`
- Create: `integrations/hermes/blueocean/text.py`
- Create: `tests/_hermes.py`
- Create: `tests/hermes_logic.py`

**Interfaces:**
- Produces (`config.py`):
  - `CONFIG_FILE = "blueocean.json"`
  - `DEFAULTS: dict[str, Any]` with keys `url`, `prefetch_max_tokens`, `ignore_projects`, `sync_turns`
  - `load_config(hermes_home: str | Path) -> dict[str, Any]`
  - `save_config(values: dict[str, Any], hermes_home: str | Path) -> None`
  - `ignore_globs(value: str | list[str] | None) -> list[str]`
- Produces (`text.py`):
  - constants `TRACE_SIDE_LIMIT = 2000`, `SUMMARY_SIDE_LIMIT = 150`
  - `strip_memory_context(text: str | None) -> str`
  - `redact(text: str) -> str`
  - `clip(text: str, limit: int) -> str`
  - `clean_turn_side(text: str | None, limit: int = TRACE_SIDE_LIMIT) -> str`
  - `trace_content(user: str, assistant: str) -> str`
  - `trace_summary(user: str, assistant: str) -> str`
  - `unseen(result: dict, seen: set[str]) -> tuple[list[dict], list[dict]]`
  - `format_recall(project: str, summaries: list[dict], fulls: list[dict]) -> str`
- Produces (`tests/_hermes.py`): `PLUGIN_DIR`, `load_plugin_module(name: str) -> ModuleType`. Task 5 adds the server helpers.

- [ ] **Step 1: Write the module loader for tests**

Create `tests/_hermes.py`:

```python
"""Helpers for the Hermes provider tests.

The plugin's package __init__ imports Hermes, which this repo's environment
does not have. Its sibling modules deliberately import neither Hermes nor each
other, so the tests load each one directly by path.
"""

import importlib.util
import sys
from types import ModuleType

from ._helpers import REPO_ROOT

PLUGIN_DIR = REPO_ROOT / "integrations" / "hermes" / "blueocean"


def load_plugin_module(name: str) -> ModuleType:
    """Import integrations/hermes/blueocean/<name>.py without its package."""
    module_name = f"blueocean_hermes_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module
```

- [ ] **Step 2: Write the failing tests**

Create `tests/hermes_logic.py`:

```python
"""The Hermes provider's pure logic: no Hermes install, no server.

Covers what a turn's text goes through before it is stored (fence stripping,
secret redaction, clipping), how recalled entries are de-duplicated and
formatted, and how the plugin's settings file is read and written.

Run with:
    uv run python -m tests.hermes_logic
"""

import calendar
import json
import tempfile
from pathlib import Path

from ._hermes import load_plugin_module

config = load_plugin_module("config")
text = load_plugin_module("text")


def test_config() -> None:
    print("== config: defaults, round trip, clamping, junk ==")
    with tempfile.TemporaryDirectory() as home:
        assert config.load_config(home) == config.DEFAULTS, "missing file must give defaults"

        config.save_config(
            {"url": "http://example:1/mcp", "auth_token": "s3cret", "bogus": 1,
             "prefetch_max_tokens": 5000, "sync_turns": "false"},
            home,
        )
        raw = json.loads((Path(home) / config.CONFIG_FILE).read_text())
        assert "auth_token" not in raw, "a secret was written to the settings file"
        assert "bogus" not in raw, "an unknown key was written"

        loaded = config.load_config(home)
        assert loaded["url"] == "http://example:1/mcp"
        assert loaded["prefetch_max_tokens"] == 2000, "budget must clamp to 2000"
        assert loaded["sync_turns"] is False, "'false' must read as False"

        config.save_config({"prefetch_max_tokens": 10}, home)
        loaded = config.load_config(home)
        assert loaded["prefetch_max_tokens"] == 100, "budget must clamp to 100"
        assert loaded["url"] == "http://example:1/mcp", "save must merge, not replace"

        (Path(home) / config.CONFIG_FILE).write_text("{not json")
        assert config.load_config(home) == config.DEFAULTS, "malformed file must give defaults"

    assert config.ignore_globs("a, b,,") == ["a", "b"]
    assert config.ignore_globs(["x", " y "]) == ["x", "y"]
    assert config.ignore_globs(None) == []
    print("  OK")


def test_fence_and_redaction() -> None:
    print("== text: memory-context fence stripped, secrets redacted ==")
    fenced = "before <memory-context>\nrecalled entry\n</memory-context> after"
    stripped = text.strip_memory_context(fenced)
    assert "recalled entry" not in stripped and stripped.startswith("before")
    assert text.strip_memory_context(None) == ""

    secrets = {
        "Authorization: Bearer abcdefghijklmnop1234": "abcdefghijklmnop1234",
        "key sk-proj-ABCDEFGHIJKLMNOPQRST": "sk-proj-ABCDEFGHIJKLMNOPQRST",
        "aws AKIAIOSFODNN7EXAMPLE here": "AKIAIOSFODNN7EXAMPLE",
        "gh ghp_" + "a" * 36: "ghp_" + "a" * 36,
        "pat github_pat_" + "B" * 30: "github_pat_" + "B" * 30,
        "slack xoxb-123456789012-abcdefghij": "xoxb-123456789012-abcdefghij",
        "password=hunter2!": "hunter2!",
        "api_key: zzz999": "zzz999",
        # Thai letters count as \w, so a \b-anchored pattern misses these.
        "คีย์คือsk-ABCDEFGHIJKLMNOPQRSTUV": "sk-ABCDEFGHIJKLMNOPQRSTUV",
        "โทเคนAKIAIOSFODNN7EXAMPLEนะ": "AKIAIOSFODNN7EXAMPLE",
    }
    for raw, secret in secrets.items():
        out = text.redact(raw)
        assert secret not in out, f"secret survived redaction: {raw!r} -> {out!r}"
        assert "[REDACTED]" in out
    assert text.redact("password=hunter2!") == "password=[REDACTED]"

    harmless = "we chose redis for the token bucket and the skip-list"
    assert text.redact(harmless) == harmless, "ordinary prose was redacted"
    print("  OK")


def test_turn_cleaning() -> None:
    print("== text: turn sides cleaned, clipped, summarised ==")
    long_side = "x" * 5000
    cleaned = text.clean_turn_side(long_side)
    assert len(cleaned) <= text.TRACE_SIDE_LIMIT and cleaned.endswith("…")

    side = text.clean_turn_side("<memory-context>old</memory-context>token=abc123 hi")
    assert "old" not in side and "abc123" not in side and side.endswith("hi")

    assert text.trace_content("u", "a") == "User: u\n\nAssistant: a"
    summary = text.trace_summary("line one\n\n  line two " + "y" * 400, "ok\tdone")
    user_part, assistant_part = summary.split(" -> ")
    assert "\n" not in summary and "  " not in user_part
    assert len(user_part) <= text.SUMMARY_SIDE_LIMIT
    assert assistant_part == "ok done"
    print("  OK")


def test_unseen_and_format() -> None:
    print("== text: dedup by id, recall block format ==")
    ts = calendar.timegm((2026, 10, 8, 12, 0, 0))
    meta = {"area": "architecture", "module": "core", "importance": 5, "timestamp": ts}
    result = {
        "summary": [
            {"id": "a", "summary": "Seen already", "metadata": meta},
            {"id": "b", "summary": "Chose Redis", "metadata": meta},
        ],
        "full": [
            {"id": "a", "content": "old detail", "metadata": meta},
            {"id": "b", "content": "Redis won on latency", "metadata": meta},
        ],
    }
    summaries, fulls = text.unseen(result, {"a"})
    assert [r["id"] for r in summaries] == ["b"] and [r["id"] for r in fulls] == ["b"]
    assert text.unseen({}, set()) == ([], [])

    block = text.format_recall("proj", summaries, fulls)
    assert block.splitlines()[0] == "BlueOcean project memory - proj"
    assert "- [architecture/core · 2026-10-08 · importance 5] Chose Redis" in block
    assert "Details:" in block and "Redis won on latency" in block
    assert "old detail" not in block
    print("  OK")


def main() -> None:
    test_config()
    test_fence_and_redaction()
    test_turn_cleaning()
    test_unseen_and_format()
    print("\nHERMES LOGIC TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m tests.hermes_logic`
Expected: FAIL with `FileNotFoundError` (no `integrations/hermes/blueocean/config.py`).

- [ ] **Step 4: Implement `config.py`**

Create `integrations/hermes/blueocean/config.py`:

```python
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
```

- [ ] **Step 5: Implement `text.py`**

Create `integrations/hermes/blueocean/text.py`:

```python
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run python -m tests.hermes_logic`
Expected: `HERMES LOGIC TEST PASSED`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check tests integrations
git add integrations/hermes/blueocean/config.py integrations/hermes/blueocean/text.py tests/_hermes.py tests/hermes_logic.py
git commit -m "Add the Hermes provider's settings and text handling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Project detection

**Files:**
- Create: `integrations/hermes/blueocean/project.py`
- Modify: `tests/hermes_logic.py` (add project tests)

**Interfaces:**
- Produces:
  - `match_project(message: str | None, known: Iterable[str], ignore: Sequence[str]) -> str | None`
  - `ProjectTracker()` with:
    - `active(key) -> str | None`
    - `set_active(key, project) -> None`
    - `observe(key, message, known, ignore) -> str | None`
    - `seen(key) -> set[str]` (returns a copy)
    - `mark_seen(key, ids) -> None`
    - `switch(old_key, new_key, *, carry_project: bool) -> None`
  - `ProjectCatalog(fetch: Callable[[], list[str]], ttl: float = 300.0, clock: Callable[[], float] = time.monotonic)` with `names() -> list[str]` (sorted)

- [ ] **Step 1: Write the failing tests**

In `tests/hermes_logic.py`, below `text = load_plugin_module("text")`, add:

```python
project = load_plugin_module("project")
```

Add these functions above `main()`:

```python
def test_match_project() -> None:
    print("== project: names found in Thai/English text, not inside tokens ==")
    known = ["blueocean", "blueocean-vector", "kops", "tidepool", "thalassaops",
             "auth-test-project"]
    ignore = ["*test*", "*-project", "probe-*", "*-probe"]
    cases = {
        "ช่วยดู blueocean-vector หน่อย": "blueocean-vector",
        # \b would see no boundary between Thai letters and the name.
        "ดูblueocean-vectorให้หน่อย": "blueocean-vector",
        "BlueOcean-Vector status?": "blueocean-vector",
        "just blueocean please": "blueocean",
        "kopsctl is a different tool": None,
        "open ~/Projects/Repo/kops/main.go": "kops",
        "auth-test-project and tidepool": "tidepool",
        "tidepool and thalassaops": "thalassaops",
        "nothing relevant here": None,
        "": None,
    }
    for message, expected in cases.items():
        got = project.match_project(message, known, ignore)
        assert got == expected, f"{message!r}: expected {expected!r}, got {got!r}"
    assert project.match_project(None, known, ignore) is None
    print("  OK")


def test_tracker() -> None:
    print("== project: latest signal wins, per session, seen ids ==")
    known, ignore = ["tidepool", "thalassaops", "kops"], []
    t = project.ProjectTracker()
    assert t.observe("s1", "work on tidepool", known, ignore) == "tidepool"
    assert t.observe("s1", "now continue", known, ignore) == "tidepool", "must be sticky"
    assert t.observe("s1", "switch to thalassaops", known, ignore) == "thalassaops"
    t.set_active("s1", "kops")
    assert t.active("s1") == "kops"
    assert t.active("s2") is None, "sessions must not share a project"

    t.mark_seen("s1", ["x", "y"])
    snapshot = t.seen("s1")
    snapshot.add("z")
    assert t.seen("s1") == {"x", "y"}, "seen() must return a copy"

    t.switch("s1", "s1", carry_project=True)  # rewound: same id
    assert t.active("s1") == "kops" and t.seen("s1") == set()

    t.mark_seen("s1", ["x"])
    t.switch("s1", "s3", carry_project=True)  # compression / resume
    assert t.active("s3") == "kops" and t.seen("s3") == set()

    t.switch("s3", "s4", carry_project=False)  # /new, /reset
    assert t.active("s4") is None
    print("  OK")


def test_catalog() -> None:
    print("== project: catalog caches for its TTL and survives failures ==")
    now = [1000.0]
    calls = []
    responses = [["b", "a"], RuntimeError("down"), ["c"]]

    def fetch():
        calls.append(1)
        value = responses[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    cat = project.ProjectCatalog(fetch, ttl=300.0, clock=lambda: now[0])
    assert cat.names() == ["a", "b"] and len(calls) == 1
    assert cat.names() == ["a", "b"] and len(calls) == 1, "must serve from cache"
    now[0] += 301
    assert cat.names() == ["a", "b"] and len(calls) == 2, "failed refresh keeps the old list"
    now[0] += 301
    assert cat.names() == ["c"] and len(calls) == 3

    empty = project.ProjectCatalog(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert empty.names() == [], "no cache and a failed fetch must give []"
    print("  OK")
```

Change `main()` to:

```python
def main() -> None:
    test_config()
    test_fence_and_redaction()
    test_turn_cleaning()
    test_unseen_and_format()
    test_match_project()
    test_tracker()
    test_catalog()
    print("\nHERMES LOGIC TEST PASSED")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m tests.hermes_logic`
Expected: FAIL with `FileNotFoundError` for `project.py`.

- [ ] **Step 3: Implement `project.py`**

Create `integrations/hermes/blueocean/project.py`:

```python
"""Which BlueOcean project a Hermes session is talking about.

Hermes runs across many repositories and its gateway has no working
directory, so the project is detected from the conversation. The latest
signal wins: a cwd default, a project named in a message, or an explicit
tool call. Whatever was set last stays active for later turns.

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
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

_NAME_CHAR = r"[A-Za-z0-9_-]"


def match_project(message: str | None, known: Iterable[str], ignore: Sequence[str]) -> str | None:
    """The longest known project named in ``message``, or None."""
    text = message or ""
    best: str | None = None
    for name in sorted(known):
        if not name or any(fnmatch.fnmatchcase(name, g) for g in ignore):
            continue
        pattern = rf"(?<!{_NAME_CHAR}){re.escape(name)}(?!{_NAME_CHAR})"
        if re.search(pattern, text, re.IGNORECASE) and (best is None or len(name) > len(best)):
            best = name
    return best


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
                ignore: Sequence[str]) -> str | None:
        hit = match_project(message, known, ignore)
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
            except Exception:
                if self._names is None:
                    return []
            self._fetched_at = now
            return self._names
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.hermes_logic`
Expected: `HERMES LOGIC TEST PASSED`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check tests integrations
git add integrations/hermes/blueocean/project.py tests/hermes_logic.py
git commit -m "Detect a Hermes session's project from the conversation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Bridge to the BlueOcean MCP server

**Files:**
- Create: `integrations/hermes/blueocean/bridge.py`
- Modify: `tests/_hermes.py` (add server helpers)
- Create: `tests/hermes_bridge.py`

**Interfaces:**
- Produces:
  - `class BridgeUnavailable(Exception)`
  - `class ToolCallError(BridgeUnavailable)`: the tool answered with an error. It never opens the breaker.
  - `class Breaker(cooldown: float = 30.0, clock=time.monotonic)` with `remaining() -> float`, `allow() -> bool`, `failure() -> bool`, `success() -> bool`
  - `class Bridge(url: str, token: str, *, call_timeout: float = 5.0, cooldown: float = 30.0, queue_size: int = 200, spawn: Callable[[Callable[[], None], str], threading.Thread] | None = None)` with:
    - `call(tool, args, *, timeout: float | None = None) -> Any`: the decoded JSON, or the raw text when it isn't JSON. `timeout` defaults to `call_timeout`. Raises `BridgeUnavailable`/`ToolCallError`.
    - `submit(tool, args) -> None`
    - `drain(timeout: float) -> bool`
  - `describe(exc: BaseException, url: str) -> str`
- Produces (`tests/_hermes.py`):
  - `start_server(port: int, token: str) -> subprocess.Popen`
  - `stop_server(proc) -> None`

Verified facts this design rests on (checked on 2026-10-08 against `mcp==2.0.0`):
- `streamable_http_client(url, *, http_client=None, terminate_on_close=True)` takes an `httpx2.AsyncClient`, built with `mcp.shared._httpx_utils.create_mcp_http_client(headers=...)`.
- A tool that fails (e.g. an invalid project name) returns `CallToolResult(is_error=True)` with text `Error executing tool <name>`. It does not raise. The field is `is_error`; `isError` raises `AttributeError` in 2.x.
- A wrong token surfaces as an `ExceptionGroup` around `mcp.shared.exceptions.MCPError("Server returned an error response")`, with no status code.
- A dead port surfaces as an `ExceptionGroup` around `httpx2.ConnectError`.
- The session holds anyio task groups, which must be exited by the task that entered them. So one dedicated task owns the session.

- [ ] **Step 1: Add server helpers for tests**

Append to `tests/_hermes.py`:

```python
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from ._helpers import BIN


def start_server(port: int, token: str) -> subprocess.Popen:
    """A real BlueOcean server over streamable HTTP, requiring ``token``.

    The token goes in through the environment, not --auth-token, which `ps`
    would show to other local users.
    """
    log = tempfile.TemporaryFile(mode="w+")
    env = {**os.environ, "BLUEOCEAN_AUTH_TOKEN": token}
    proc = subprocess.Popen(
        [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=log, env=env, text=True,
    )
    proc.log = log  # type: ignore[attr-defined]
    health = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + 90  # the embedding model loads before the port opens
    while time.time() < deadline:
        if proc.poll() is not None:
            log.seek(0)
            raise RuntimeError(f"server exited early:\n{log.read()}")
        try:
            urllib.request.urlopen(health, timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc  # any HTTP answer means the port is serving
        except OSError:
            time.sleep(0.3)
    stop_server(proc)
    raise TimeoutError(f"server on port {port} did not start")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
```

Move the new `import` lines to the top of the file with the existing imports, so ruff's import-order rule passes.

- [ ] **Step 2: Write the failing tests**

Create `tests/hermes_bridge.py`:

```python
"""The Hermes provider's bridge, against a real server over HTTP.

Hermes calls a memory provider synchronously; the MCP client is async and
keeps a session alive across calls. These tests pin what a person chatting
with Hermes would notice: recall works with the right token, a wrong token or
a stopped server costs one quick failure rather than a five-second wait every
turn, writes survive a short outage, and a server restart (docker rebuild)
does not break the session until Hermes restarts.

Run with:
    uv run python -m tests.hermes_bridge
"""

import time

from ._hermes import load_plugin_module, start_server, stop_server
from ._helpers import reset_project

bridge = load_plugin_module("bridge")

PORT = 8796
URL = f"http://127.0.0.1:{PORT}/mcp"
TOKEN = "bridge-test-token"
PROJECT = "hermes-bridge-project"


def test_breaker_unit() -> None:
    print("== breaker: opens on failure, logs once per outage, closes on success ==")
    now = [0.0]
    b = bridge.Breaker(cooldown=30.0, clock=lambda: now[0])
    assert b.allow() and b.remaining() == 0.0
    assert b.failure() is True, "first failure starts an outage"
    assert not b.allow() and b.remaining() == 30.0
    now[0] = 10.0
    assert b.failure() is False, "a failure inside an outage is not a new outage"
    assert b.remaining() == 30.0, "a new failure restarts the cooldown"
    now[0] = 41.0
    assert b.allow()
    assert b.success() is True and b.success() is False
    print("  OK")


def test_round_trip(proc_url: str) -> None:
    print("== call/submit/drain against a live server ==")
    b = bridge.Bridge(proc_url, TOKEN)
    point_id = b.call("memory_store", {
        "project": PROJECT, "area": "backend", "module": "cache",
        "content": "We chose redis for the session cache.",
        "summary": "Chose redis for the session cache", "importance": 4,
    })
    assert isinstance(point_id, str) and point_id, point_id
    found = b.call("memory_search", {"project": PROJECT, "query": "session cache choice"})
    assert point_id in {r["id"] for r in found["summary"]}

    b.submit("memory_store", {
        "project": PROJECT, "area": "hermes", "module": "turns",
        "content": "User: cache?\n\nAssistant: redis", "summary": "cache? -> redis",
        "importance": 1, "metadata": {"kind": "hermes_turn"},
    })
    assert b.drain(10.0), "queued write did not finish"
    traces = b.call("memory_search", {"project": PROJECT, "query": "cache redis",
                                      "kind": "hermes_turn"})
    assert len(traces["summary"]) == 1, traces
    print("  OK")


def test_tool_error_is_not_an_outage(proc_url: str) -> None:
    print("== a rejected argument raises ToolCallError and keeps the breaker shut ==")
    b = bridge.Bridge(proc_url, TOKEN)
    try:
        b.call("memory_store", {"project": "Not A Valid Name", "area": "a", "module": "m",
                                "content": "c", "summary": "s"})
    except bridge.ToolCallError:
        pass
    else:
        raise AssertionError("an invalid project name must raise ToolCallError")
    assert b.call("memory_list_projects", {})["projects"], "breaker opened on a tool error"
    print("  OK")


def test_wrong_token(proc_url: str) -> None:
    print("== a wrong token fails once, clearly, then fails fast ==")
    b = bridge.Bridge(proc_url, "not-the-token", cooldown=30.0)
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "BLUEOCEAN_AUTH_TOKEN" in str(exc), str(exc)
    else:
        raise AssertionError("a wrong token must raise")
    started = time.monotonic()
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "paused" in str(exc), str(exc)
    assert time.monotonic() - started < 0.1, "an open breaker must not wait"
    print("  OK")


def test_dead_server() -> None:
    print("== a stopped server: quick failure, fast skips, retry after cooldown ==")
    dead = "http://127.0.0.1:8794/mcp"
    b = bridge.Bridge(dead, TOKEN, cooldown=1.0)
    started = time.monotonic()
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "unreachable" in str(exc), str(exc)
    assert time.monotonic() - started < 2.0, "a refused connection should fail fast"
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "paused" in str(exc), str(exc)
    time.sleep(1.1)
    try:
        b.call("memory_list_projects", {})
    except bridge.BridgeUnavailable as exc:
        assert "unreachable" in str(exc), "after the cooldown it must try again"
    print("  OK")


def test_restart_reconnects() -> None:
    print("== a server restart (docker rebuild) is absorbed by one reconnect ==")
    proc = start_server(PORT, TOKEN)
    b = bridge.Bridge(URL, TOKEN)
    try:
        assert b.call("memory_list_projects", {})["projects"] is not None
    finally:
        stop_server(proc)
    proc = start_server(PORT, TOKEN)
    try:
        assert b.call("memory_list_projects", {})["projects"] is not None
    finally:
        stop_server(proc)
    print("  OK")


def test_writes_survive_outage() -> None:
    print("== writes queued while the server is down are delivered when it is back ==")
    b = bridge.Bridge(URL, TOKEN, cooldown=1.0)
    b.submit("memory_store", {
        "project": PROJECT, "area": "hermes", "module": "turns",
        "content": "User: during outage\n\nAssistant: queued", "summary": "outage -> queued",
        "importance": 1, "metadata": {"kind": "hermes_turn"},
    })
    time.sleep(1.5)  # at least one failed attempt
    proc = start_server(PORT, TOKEN)
    try:
        assert b.drain(15.0), "the queued write was never delivered"
        traces = b.call("memory_search", {"project": PROJECT, "query": "outage queued",
                                          "kind": "hermes_turn"})
        assert any("outage" in r["summary"] for r in traces["summary"]), traces
    finally:
        stop_server(proc)
    print("  OK")


def main() -> None:
    reset_project(PROJECT)
    test_breaker_unit()
    proc = start_server(PORT, TOKEN)
    try:
        test_round_trip(URL)
        test_tool_error_is_not_an_outage(URL)
        test_wrong_token(URL)
    finally:
        stop_server(proc)
    test_dead_server()
    test_restart_reconnects()
    test_writes_survive_outage()
    print("\nHERMES BRIDGE TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m tests.hermes_bridge`
Expected: FAIL with `FileNotFoundError` for `bridge.py`.

- [ ] **Step 4: Implement `bridge.py`**

Create `integrations/hermes/blueocean/bridge.py`:

```python
"""Synchronous access to the BlueOcean MCP server.

Hermes calls a memory provider synchronously from its own threads, while the
MCP client is async and holds anyio task groups open for the life of a
session. This module runs one event loop on one background thread, keeps a
single long-lived ClientSession on it, and offers two sync entry points:
``call`` (bounded wait for a result) and ``submit`` (fire and forget,
delivered in order by a writer task).

A circuit breaker stops a stopped server from costing every Hermes turn the
full call timeout. A dead session (server restarted) is replaced by one
reconnect inside the same call.

Imports nothing from Hermes and no sibling module. The thread factory is
injected: the provider passes Hermes's ``spawn_context_thread``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Callable, Iterator

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 5.0
COOLDOWN_S = 30.0
QUEUE_SIZE = 200

SpawnFn = Callable[[Callable[[], None], str], threading.Thread]


class BridgeUnavailable(Exception):
    """The server could not be reached, refused us, or did not answer in time."""


class ToolCallError(BridgeUnavailable):
    """The server answered but the tool reported an error. Not an outage."""


class Breaker:
    """Skip calls for ``cooldown`` seconds after a failure.

    ``failure`` and ``success`` return True on the transition into and out
    of an outage, so the caller logs once per outage rather than per call.
    """

    def __init__(self, cooldown: float = COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self._cooldown - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        return self.remaining() == 0.0

    def failure(self) -> bool:
        with self._lock:
            first = self._opened_at is None
            self._opened_at = self._clock()
            return first

    def success(self) -> bool:
        with self._lock:
            recovered = self._opened_at is not None
            self._opened_at = None
            return recovered


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    nested = getattr(exc, "exceptions", None)
    if nested:
        for inner in nested:
            yield from _leaves(inner)
    else:
        yield exc


def describe(exc: BaseException, url: str) -> str:
    """A log-ready reason. The MCP client reports a 401 only as MCPError."""
    leaves = list(_leaves(exc))
    names = {type(e).__name__ for e in leaves}
    if "ConnectError" in names or any(isinstance(e, OSError) for e in leaves):
        return f"BlueOcean unreachable at {url}"
    if "MCPError" in names:
        return "BlueOcean rejected the request (check BLUEOCEAN_AUTH_TOKEN)"
    return f"BlueOcean call failed: {type(leaves[0]).__name__}"


def _decode(result: Any) -> Any:
    text = "".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:  # mcp 2.x field name; 1.x called it isError
        raise ToolCallError(text or "tool reported an error")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _plain_thread(target: Callable[[], None], name: str) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=True)


class Bridge:
    def __init__(self, url: str, token: str, *, call_timeout: float = CALL_TIMEOUT_S,
                 cooldown: float = COOLDOWN_S, queue_size: int = QUEUE_SIZE,
                 spawn: SpawnFn | None = None) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._call_timeout = call_timeout
        self._cooldown = cooldown
        self._queue_size = queue_size
        self._spawn = spawn or _plain_thread
        self._breaker = Breaker(cooldown)
        self._start_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Touched only on the loop thread:
        self._queue: asyncio.Queue | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._session: ClientSession | None = None
        self._owner: asyncio.Task | None = None
        self._closing: asyncio.Event | None = None
        self._dropping = False

    # -- sync surface ------------------------------------------------------

    def call(self, tool: str, args: dict, *, timeout: float | None = None) -> Any:
        if not self._breaker.allow():
            raise BridgeUnavailable("BlueOcean paused after a recent failure")
        wait = self._call_timeout if timeout is None else timeout
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._call_with_retry(tool, args), loop)
        try:
            return future.result(timeout=wait)
        except concurrent.futures.TimeoutError:
            future.cancel()
            if self._breaker.failure():
                logger.warning("BlueOcean did not answer within %.0fs; pausing calls for %.0fs",
                               wait, self._cooldown)
            raise BridgeUnavailable("BlueOcean timed out") from None

    def submit(self, tool: str, args: dict) -> None:
        loop = self._ensure_loop()
        loop.call_soon_threadsafe(self._enqueue, tool, args)

    def drain(self, timeout: float) -> bool:
        """Wait for queued writes, including the one in flight. False on timeout."""
        if self._loop is None or self._queue is None:
            return True
        future = asyncio.run_coroutine_threadsafe(self._queue.join(), self._loop)
        try:
            future.result(timeout=timeout)
            return True
        except concurrent.futures.TimeoutError:
            future.cancel()
            return False

    # -- loop thread -------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                self._queue = asyncio.Queue(maxsize=self._queue_size)
                self._connect_lock = asyncio.Lock()
                loop.create_task(self._writer())
                loop.call_soon(ready.set)
                loop.run_forever()

            self._spawn(run, "blueocean-bridge").start()
            if not ready.wait(5):
                raise BridgeUnavailable("BlueOcean bridge thread did not start")
            self._loop = loop
            return loop

    async def _call_with_retry(self, tool: str, args: dict) -> Any:
        last: BaseException | None = None
        for _ in range(2):  # the second attempt runs on a fresh session
            try:
                session = await asyncio.wait_for(self._ensure_session(), self._call_timeout)
                result = await asyncio.wait_for(session.call_tool(tool, args), self._call_timeout)
            except Exception as exc:
                last = exc
                await self._reset_session()
                continue
            if self._breaker.success():
                logger.info("BlueOcean reachable again at %s", self._url)
            return _decode(result)
        message = describe(last, self._url)
        if self._breaker.failure():
            logger.warning("%s; pausing BlueOcean calls for %.0fs", message, self._cooldown)
        raise BridgeUnavailable(message) from last

    async def _ensure_session(self) -> ClientSession:
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._closing = asyncio.Event()
            self._owner = asyncio.create_task(self._own_session(ready, self._closing))
            return await ready

    async def _own_session(self, ready: asyncio.Future, closing: asyncio.Event) -> None:
        """Enter and exit the session in this one task: anyio requires a task
        group to be exited by the task that entered it."""
        mine: ClientSession | None = None
        try:
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    create_mcp_http_client(headers=self._headers))
                streams = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=client))
                mine = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await mine.initialize()
                self._session = mine
                if not ready.done():
                    ready.set_result(mine)
                await closing.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.debug("BlueOcean session ended: %s", describe(exc, self._url))
        finally:
            if self._session is mine:
                self._session = None

    async def _reset_session(self) -> None:
        owner, closing = self._owner, self._closing
        self._session = None
        self._owner = None
        self._closing = None
        if closing is not None:
            closing.set()
        if owner is not None:
            await asyncio.wait({owner}, timeout=2)

    def _enqueue(self, tool: str, args: dict) -> None:
        assert self._queue is not None
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            if not self._dropping:
                logger.warning("BlueOcean write queue full (%d); dropping the oldest writes",
                               self._queue_size)
                self._dropping = True
        self._queue.put_nowait((tool, args))

    async def _writer(self) -> None:
        assert self._queue is not None
        while True:
            tool, args = await self._queue.get()
            try:
                await self._deliver(tool, args)
            except Exception:
                logger.warning("BlueOcean write failed", exc_info=True)
            finally:
                self._queue.task_done()
            if self._queue.empty():
                self._dropping = False

    async def _deliver(self, tool: str, args: dict) -> None:
        """Keep one write until it lands: a short outage must not lose it."""
        while True:
            wait = self._breaker.remaining()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            try:
                await self._call_with_retry(tool, args)
                return
            except ToolCallError as exc:
                logger.warning("BlueOcean rejected %s: %s", tool, exc)
                return
            except BridgeUnavailable:
                await asyncio.sleep(0.05)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m tests.hermes_bridge`
Expected: `HERMES BRIDGE TEST PASSED`.

If `test_restart_reconnects` fails, the first call after the restart raised instead of reconnecting. Inspect `describe(last, ...)` in the raised message before changing anything. The retry loop must reset the dead session and succeed on the second attempt.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check tests integrations
git add integrations/hermes/blueocean/bridge.py tests/_hermes.py tests/hermes_bridge.py
git commit -m "Bridge Hermes's sync calls to one long-lived MCP session

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The Hermes memory provider

**Files:**
- Create: `integrations/hermes/blueocean/__init__.py`
- Create: `integrations/hermes/blueocean/plugin.yaml`
- Create: `tests/hermes_contract.py`

**Interfaces:**
- Consumes:
  - `config.DEFAULTS`, `load_config`, `save_config`, `ignore_globs` (Task 3)
  - `text.clean_turn_side`, `trace_content`, `trace_summary`, `unseen`, `format_recall` (Task 3)
  - `project.ProjectTracker`, `ProjectCatalog` (Task 4)
  - `bridge.Bridge`, `BridgeUnavailable` (Task 5)
  - Hermes:
    - `agent.memory_provider.MemoryProvider`, `RecallStatus`, `is_trivial_prompt`, `spawn_context_thread`
    - `agent.secret_scope.get_secret`
    - `hermes_constants.hermes_home_key`
- Produces: `BlueOceanMemoryProvider` with `name == "blueocean"` and exactly one tool, `blueocean_set_project`.

Hermes facts this rests on:
- `handle_tool_call` is called as `handle_tool_call(name, args)` with no session id (`agent/tool_executor.py`).
- Each agent builds its own provider instance, but a `MemoryManager` can be reused across agents, so the current session id is tracked from `initialize` and `on_session_switch`.
- Discovery instantiates the first `MemoryProvider` subclass found in the package (`plugins/memory/__init__.py:_instantiate_subclass`).
- User plugins load under a synthetic package with `submodule_search_locations` set (`plugins/plugin_loader.py:70`), so `from .bridge import ...` resolves. If Step 2's `discovery returned None` persists after Step 4, read the child's stderr for an `ImportError` before suspecting discovery itself.

- [ ] **Step 1: Write the failing contract test**

Create `tests/hermes_contract.py`:

```python
"""The provider, loaded the way Hermes loads it, against a real server.

Runs a child process on Hermes's own Python, with a throwaway HERMES_HOME
whose plugins/blueocean links to this repo. Hermes discovers the provider
through plugins.memory.load_memory_provider, exactly as `memory.provider:
blueocean` would. The child then drives one session: an explicit project
switch, a recall, the same recall again (nothing new to inject), and a turn
whose text carries a memory-context fence and a password.

Skipped when Hermes is not installed (no `hermes` on PATH and no
HERMES_PYTHON).

Run with:
    uv run python -m tests.hermes_contract
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ._hermes import PLUGIN_DIR, load_plugin_module, start_server, stop_server
from ._helpers import reset_project

PORT = 8795
URL = f"http://127.0.0.1:{PORT}/mcp"
TOKEN = "contract-test-token"
PROJECT = "hermes-contract-project"
HERMES_AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))

CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
import hermes_bootstrap  # noqa: F401  (puts Hermes's dependencies on sys.path)
from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider

project = os.environ["CONTRACT_PROJECT"]
p = load_memory_provider("blueocean")
assert isinstance(p, MemoryProvider), f"discovery returned {p!r}"
assert p.name == "blueocean"
assert p.is_available(), p.unavailable_reason()
assert [s["name"] for s in p.get_tool_schemas()] == ["blueocean_set_project"]

p.initialize("contract-session", hermes_home=os.environ["HERMES_HOME"],
             platform="cli", agent_context="primary")
trivial = p.prefetch("ok", session_id="contract-session")
set_out = json.loads(p.handle_tool_call("blueocean_set_project", {"project": project}))
first = p.prefetch("which cache did we choose", session_id="contract-session")
status = p.recall_status()
second = p.prefetch("which cache did we choose", session_id="contract-session")
p.sync_turn("<memory-context>recalled stuff</memory-context> which cache? password=hunter2",
            "We chose Redis.", session_id="contract-session")
p.shutdown()
print("RESULT " + json.dumps({
    "trivial": trivial, "set": set_out, "first": first,
    "count": status.count if status else 0,
    "label": status.provider_label if status else "",
    "second": second,
}))
'''


def hermes_python() -> list[str] | None:
    override = os.environ.get("HERMES_PYTHON")
    if override:
        return [override, "-I"]
    launcher = shutil.which("hermes")
    if not launcher or not HERMES_AGENT_DIR.is_dir():
        return None
    out = subprocess.run([launcher, "--print-runtime-command"], capture_output=True,
                         text=True, check=False)
    try:
        return [json.loads(out.stdout)[0], "-I"]
    except (ValueError, IndexError):
        return None


def main() -> None:
    python = hermes_python()
    if python is None:
        print("SKIP: Hermes not installed (set HERMES_PYTHON / HERMES_AGENT_DIR)")
        return

    reset_project(PROJECT)
    proc = start_server(PORT, TOKEN)
    try:
        bridge = load_plugin_module("bridge").Bridge(URL, TOKEN)
        bridge.call("memory_store", {
            "project": PROJECT, "area": "backend", "module": "cache",
            "content": "Redis won on latency for the session cache.",
            "summary": "Chose Redis for the cache", "importance": 4,
        })

        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "plugins").mkdir()
            (Path(home) / "plugins" / "blueocean").symlink_to(PLUGIN_DIR)
            (Path(home) / "blueocean.json").write_text(json.dumps({"url": URL}))
            env = {**os.environ, "HERMES_HOME": home, "HERMES_AGENT_DIR": str(HERMES_AGENT_DIR),
                   "BLUEOCEAN_AUTH_TOKEN": TOKEN, "CONTRACT_PROJECT": PROJECT}
            # Hermes's launcher drops these; `uv run` sets VIRTUAL_ENV to this repo's venv.
            for var in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
                env.pop(var, None)
            child = subprocess.run([*python, "-c", CHILD], env=env, capture_output=True,
                                   text=True, timeout=180, check=False)
        line = next((ln for ln in child.stdout.splitlines() if ln.startswith("RESULT ")), None)
        assert child.returncode == 0 and line, f"child failed:\n{child.stdout}\n{child.stderr}"
        out = json.loads(line[len("RESULT "):])

        print("== discovery, availability, trivial prompt ==")
        assert out["trivial"] == "", "a trivial prompt must not recall"
        print("  OK")

        print("== blueocean_set_project then recall ==")
        assert out["set"] == {**out["set"], "ok": True, "project": PROJECT, "has_memory": True}
        assert "BlueOcean project memory - " + PROJECT in out["first"], out["first"]
        assert "Chose Redis for the cache" in out["first"]
        assert out["count"] == 1 and out["label"] == f"BlueOcean ({PROJECT})"
        print("  OK")

        print("== the same recall twice injects nothing new ==")
        assert out["second"] == "", out["second"]
        print("  OK")

        print("== the Turn Trace: stored, fence stripped, password redacted ==")
        traces = bridge.call("memory_search", {"project": PROJECT, "query": "which cache",
                                               "kind": "hermes_turn"})
        assert len(traces["summary"]) == 1, traces
        content = traces["full"][0]["content"]
        assert "We chose Redis." in content
        assert "recalled stuff" not in content, "recalled memory was stored back"
        assert "hunter2" not in content, "a password was stored"
        meta = traces["summary"][0]["metadata"]
        assert (meta["area"], meta["module"], meta["importance"]) == ("hermes", "turns", 1)
        print("  OK")

        print("== the trace stays out of default search ==")
        default = bridge.call("memory_search", {"project": PROJECT, "query": "which cache"})
        assert all(r["metadata"].get("kind") != "hermes_turn" for r in default["summary"])
        print("  OK")
    finally:
        stop_server(proc)
    print("\nHERMES CONTRACT TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m tests.hermes_contract`
Expected: FAIL with `child failed:` and `discovery returned None`, because the plugin directory has no `__init__.py` yet.

- [ ] **Step 3: Write the manifest**

Create `integrations/hermes/blueocean/plugin.yaml`:

```yaml
name: blueocean
version: 0.1.0
description: "BlueOcean Vector shared project memory: per-turn recall for the project in conversation, and a Turn Trace of each turn."
requires_env:
  - BLUEOCEAN_AUTH_TOKEN
```

- [ ] **Step 4: Implement the provider**

Create `integrations/hermes/blueocean/__init__.py`:

```python
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

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from agent.memory_provider import (
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from agent.secret_scope import get_secret
from hermes_constants import hermes_home_key

from .bridge import Bridge, BridgeUnavailable
from .config import DEFAULTS, ignore_globs, load_config, save_config
from .project import ProjectCatalog, ProjectTracker
from .text import clean_turn_side, format_recall, trace_content, trace_summary, unseen

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
_BRIDGES: Dict[tuple, Bridge] = {}
_BRIDGES_LOCK = threading.Lock()


def _spawn(target, name: str) -> threading.Thread:
    return spawn_context_thread(target, name=name)


def _bridge_for(url: str, token: str) -> Bridge:
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
        self._config: Dict[str, Any] = dict(DEFAULTS)
        self._bridge: Optional[Bridge] = None
        self._catalog: Optional[ProjectCatalog] = None
        self._tracker = ProjectTracker()
        self._cwd_project: Optional[str] = None
        self._last_status: Optional[RecallStatus] = None

    @property
    def name(self) -> str:
        return "blueocean"

    # -- availability and setup -------------------------------------------

    @staticmethod
    def _token() -> str:
        try:
            return (get_secret(TOKEN_ENV, "") or "").strip()
        except Exception:
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

    def get_config_schema(self) -> List[Dict[str, Any]]:
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
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
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

    def _fetch_projects(self) -> List[str]:
        result = self._bridge.call("memory_list_projects", {}, timeout=CATALOG_TIMEOUT_S)
        return list(result.get("projects", [])) if isinstance(result, dict) else []

    def _resolve_project(self, key: str, query: str) -> Optional[str]:
        known = self._catalog.names()
        active = self._tracker.observe(key, query, known,
                                       ignore_globs(self._config["ignore_projects"]))
        if active is None and self._cwd_project and self._cwd_project in known:
            self._tracker.set_active(key, self._cwd_project)
            active = self._cwd_project
        return active

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_status = None
        if self._bridge is None or is_trivial_prompt(query):
            return ""
        key = self._key(session_id)
        try:
            project = self._resolve_project(key, query)
            if not project:
                return ""
            result = self._bridge.call("memory_search", {
                "project": project,
                "query": query[:QUERY_LIMIT],
                "max_tokens": self._config["prefetch_max_tokens"],
            })
        except BridgeUnavailable as exc:
            logger.debug("BlueOcean prefetch skipped: %s", exc)
            return ""
        except Exception:
            logger.warning("BlueOcean prefetch failed", exc_info=True)
            return ""
        if not isinstance(result, dict):
            return ""
        summaries, fulls = unseen(result, self._tracker.seen(key))
        if not summaries:
            return ""
        self._tracker.mark_seen(key, [str(r["id"]) for r in summaries + fulls])
        self._last_status = RecallStatus(provider_label=f"BlueOcean ({project})",
                                         count=len(summaries))
        return format_recall(project, summaries, fulls)

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_status

    # -- Turn Trace --------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        try:
            if (self._bridge is None or self._agent_context != "primary"
                    or not self._config["sync_turns"]):
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

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SET_PROJECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "blueocean_set_project":
            return json.dumps({"error": f"unknown tool {tool_name}"})
        project = str((args or {}).get("project") or "").strip()
        if not project:
            return json.dumps({"error": "project is required"})
        self._tracker.set_active(self._key(kwargs.get("session_id", "")), project)
        has_memory = project in (self._catalog.names() if self._catalog else [])
        reply: Dict[str, Any] = {"ok": True, "project": project, "has_memory": has_memory}
        if not has_memory:
            reply["note"] = "No memory stored for this project yet; recall stays empty until something is."
        return json.dumps(reply)
```

- [ ] **Step 5: Run the contract test to verify it passes**

Run: `uv run python -m tests.hermes_contract`
Expected: `HERMES CONTRACT TEST PASSED`.

If the child fails while importing `hermes_bootstrap` with a fresh `HERMES_HOME`, read its stderr. Hermes may expect a `config.yaml` in the home: write a minimal `config.yaml` containing `memory: {provider: blueocean}` into the temp home in the driver and rerun. Do not change the provider for it.

- [ ] **Step 6: Re-run the pure and bridge suites**

Run: `uv run python -m tests.hermes_logic && uv run python -m tests.hermes_bridge`
Expected: both PASSED.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check tests integrations
git add integrations/hermes/blueocean/__init__.py integrations/hermes/blueocean/plugin.yaml tests/hermes_contract.py
git commit -m "Add BlueOcean as a Hermes memory provider

Hermes calls prefetch and sync_turn for its active provider, so project
memory is recalled every turn without the agent asking, and each turn is
kept as a Turn Trace outside default search.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Installer and docs

**Files:**
- Create: `scripts/install_hermes_plugin.sh` (mode 755)
- Create: `tests/install_hermes_plugin.py`
- Modify: `README.md` (new "Hermes integration" section after "Teaching agents to actually use it"; Running-the-tests list; Contents)

**Interfaces:**
- Consumes: `integrations/hermes/blueocean/` (Tasks 3-6).
- Produces: `scripts/install_hermes_plugin.sh [--remove]`, honoring `HERMES_HOME` (default `$HOME/.hermes`). Exit 0 on success or already-installed. Exit 1 if the Hermes home is missing or the link path is a real directory. Exit 2 on bad usage.

- [ ] **Step 1: Write the failing test**

Create `tests/install_hermes_plugin.py`:

```python
"""install_hermes_plugin.sh links the provider into a Hermes home, safely.

The link must point back at this repo so `git pull` updates the plugin, a
second run must be a no-op, and a real directory at the link path (a manual
install, or another plugin of the same name) must never be overwritten.

Run with:
    uv run python -m tests.install_hermes_plugin
"""

import subprocess
import tempfile
from pathlib import Path

from ._hermes import PLUGIN_DIR
from ._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "install_hermes_plugin.sh"


def run(hermes_home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={"HOME": str(hermes_home.parent), "HERMES_HOME": str(hermes_home),
             "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True, text=True, check=False,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / ".hermes"
        link = home / "plugins" / "blueocean"

        print("== no Hermes home: refuse ==")
        res = run(home)
        assert res.returncode == 1 and "not found" in res.stdout, res.stdout
        print("  OK")

        print("== install links back to the repo ==")
        home.mkdir()
        res = run(home)
        assert res.returncode == 0, res.stdout + res.stderr
        assert link.is_symlink() and link.resolve() == PLUGIN_DIR.resolve()
        assert "hermes memory setup" in res.stdout, "next steps not printed"
        print("  OK")

        print("== second run is a no-op ==")
        res = run(home)
        assert res.returncode == 0 and "already installed" in res.stdout, res.stdout
        print("  OK")

        print("== remove ==")
        res = run(home, "--remove")
        assert res.returncode == 0 and not link.exists() and not link.is_symlink()
        print("  OK")

        print("== a real directory at the link path is never overwritten ==")
        link.mkdir(parents=True)
        (link / "keep.txt").write_text("mine")
        res = run(home)
        assert res.returncode == 1, res.stdout
        assert (link / "keep.txt").read_text() == "mine"
        print("  OK")

        print("== bad usage ==")
        assert run(home, "--bogus").returncode == 2
        print("  OK")

    print("\nINSTALL HERMES PLUGIN TEST PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m tests.install_hermes_plugin`
Expected: FAIL (`FileNotFoundError`/`PermissionError` running the missing script).

- [ ] **Step 3: Write the installer**

Create `scripts/install_hermes_plugin.sh`:

```bash
#!/usr/bin/env bash
# Link the BlueOcean memory provider into a Hermes home.
#
# Hermes discovers user memory providers in $HERMES_HOME/plugins/<name>/. The
# provider's code stays in this repo; installing is a symlink back to it, so
# `git pull` (or scripts/update.sh) updates the plugin in place. This script
# never edits Hermes's config.yaml: choosing the active memory provider is
# `hermes memory setup`'s job.
#
# Usage:
#   ./scripts/install_hermes_plugin.sh            install (idempotent)
#   ./scripts/install_hermes_plugin.sh --remove   uninstall
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/integrations/hermes/blueocean"
LINK="$HERMES_HOME/plugins/blueocean"

case "${1:-}" in
  --remove)
    if [ -L "$LINK" ]; then
      rm "$LINK"
      echo "  - removed $LINK"
    else
      echo "  = not installed (nothing to remove)"
    fi
    exit 0
    ;;
  "") ;;
  *)
    echo "Usage: $0 [--remove]"
    exit 2
    ;;
esac

if [ ! -d "$HERMES_HOME" ]; then
  echo "!! Hermes home not found: $HERMES_HOME (set HERMES_HOME)"
  exit 1
fi

mkdir -p "$HERMES_HOME/plugins"
if [ -L "$LINK" ]; then
  if [ "$(readlink "$LINK")" = "$SOURCE" ]; then
    echo "  = already installed ($LINK -> $SOURCE)"
  else
    ln -sfn "$SOURCE" "$LINK"
    echo "  ~ relinked $LINK -> $SOURCE"
  fi
elif [ -e "$LINK" ]; then
  echo "!! $LINK exists and isn't a symlink; remove it manually first"
  exit 1
else
  ln -s "$SOURCE" "$LINK"
  echo "  + installed $LINK -> $SOURCE"
fi

cat <<EOF

Next:
  1. hermes memory setup blueocean   (BLUEOCEAN_AUTH_TOKEN must be in $HERMES_HOME/.env)
  2. restart the Hermes gateway      (memory providers are imported once per process)
Roll back: set memory.provider back to empty and restart; the blueocean MCP
server entry keeps working on its own.
EOF
```

Run: `chmod +x scripts/install_hermes_plugin.sh`

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m tests.install_hermes_plugin`
Expected: `INSTALL HERMES PLUGIN TEST PASSED`.

- [ ] **Step 5: Document in the README**

In `README.md`, add to `## Contents` under "Getting started", after the "Teaching agents" line:

```markdown
  - [Hermes: automatic recall](#hermes-automatic-recall)
```

Insert this section directly before `### Alternative: stdio (per-agent local process)`:

````markdown
### Hermes: automatic recall

Every other client only reads memory when the agent decides to call `memory_search`. [Hermes](https://github.com/NousResearch/hermes-agent) can do better: it calls its active *memory provider* itself, before and after every turn. `integrations/hermes/blueocean/` is that provider.

- **Before each turn** it finds which project the conversation is about and injects the relevant entries. It takes the project from a name in your message, or from Hermes's working directory, and you can correct it with the `blueocean_set_project` tool. The default budget is 600 tokens, and an entry already injected in the same session is not injected again.
- **After each turn** it stores a **Turn Trace** (`kind=hermes_turn`, `importance=1`, area `hermes`, module `turns`). Before storing, it strips recalled memory and redacts credentials. Traces are excluded from every search that doesn't ask for `kind="hermes_turn"`, so they never compete with real entries.
- Explicit writes still go through the `blueocean` MCP server entry. Keep it configured.

```bash
./scripts/install_hermes_plugin.sh      # symlink into ~/.hermes/plugins/blueocean
hermes memory setup blueocean           # pick it as the memory provider
# then restart the Hermes gateway
```

Settings (`hermes memory setup`, stored in `~/.hermes/blueocean.json`): `url`, `prefetch_max_tokens`, `ignore_projects` (globs never auto-detected; default skips test and probe projects), `sync_turns`. Old traces can be removed with `blueocean-admin prune <project> --kind hermes_turn --older-days 30`.

If the server is down, recall quietly returns nothing. After one failure the provider pauses for 30 s instead of making every turn wait. To roll back, set `memory.provider` back to empty.
````

In `## Running the tests`, add to the command block:

```bash
uv run python -m tests.trace_kinds            # trace kinds excluded by default; prune --kind
uv run python -m tests.hermes_logic           # Hermes provider: pure logic
uv run python -m tests.hermes_bridge          # Hermes provider: bridge vs a real server
uv run python -m tests.hermes_contract        # Hermes provider loaded by Hermes itself (skips without Hermes)
uv run python -m tests.install_hermes_plugin
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check tests integrations
git add scripts/install_hermes_plugin.sh tests/install_hermes_plugin.py README.md
git commit -m "Add a Hermes plugin installer and document automatic recall

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Full verification and live rollout

This task changes the running system: it rebuilds the shared BlueOcean container that every agent on the machine uses, and it restarts the Hermes gateway (the Discord bot). **Confirm with the user before Steps 2 and 4.** Steps 4-5 need the user at the keyboard and in Discord.

**Files:** none changed. If a step fails, fix the cause in the task that owns the code, with a regression test there.

- [ ] **Step 1: Run every suite**

```bash
uv run ruff check src tests integrations
uv run python -m tests.trace_kinds
uv run python -m tests.memory_kind
uv run python -m tests.telemetry_cli
uv run python -m tests.hermes_logic
uv run python -m tests.hermes_bridge
uv run python -m tests.hermes_contract
uv run python -m tests.install_hermes_plugin
uv run python -m tests.http_transport
uv run python -m tests.mcp_full_coverage
```

Expected: every script ends with its PASSED line, or `SKIP` for the contract test on a machine without Hermes.

- [ ] **Step 2: Rebuild the live server (confirm first)**

```bash
docker compose up -d --build blueocean-mcp
curl -s localhost:8765/health
```

Expected: `{"status":"ok", ...}`. Then confirm the exclusion is live with the `blueocean` MCP tools: `memory_store` a throwaway entry with `metadata={"kind":"hermes_turn"}` in project `telemetry-review-probe`; a default `memory_search` there must not return it, and `kind="hermes_turn"` must. Delete it afterwards with `memory_delete`.

- [ ] **Step 3: Install the plugin**

```bash
./scripts/install_hermes_plugin.sh
ls -l ~/.hermes/plugins/blueocean
```

Expected: a symlink to `integrations/hermes/blueocean` in this repo.

- [ ] **Step 4: Activate in Hermes (user runs; confirm first)**

Ask the user to run `! hermes memory setup blueocean` and accept the defaults (the token is already in `~/.hermes/.env`), then restart the Hermes gateway the way they normally do.

Then check: `grep -n -A3 "^memory:" ~/.hermes/config.yaml` shows `provider: blueocean`.

- [ ] **Step 5: Live check**

Ask the user to send Cholly a non-trivial message naming the project, e.g. "สรุปสถานะ blueocean-vector ล่าสุดให้หน่อย".

Expected:
- the reply shows a `🧠 BlueOcean (blueocean-vector) — recalled N memories` line
- afterwards, `memory_search(project="blueocean-vector", query=<their message>, kind="hermes_turn")` returns that turn's trace, with no `<memory-context>` text in it

- [ ] **Step 6: Record the outcome**

`memory_store` to project `blueocean-vector`, area `architecture`, module `hermes-memory-provider`, importance 5: what shipped (commit range), the live check result, and anything that behaved differently from the spec.
