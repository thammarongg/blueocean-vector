# Hermes Memory Provider - Design

Status: design approved section by section in chat, awaiting review of this
written spec before planning.
Date: 2026-10-08

## 1. Why

Hermes (the "Cholly" assistant, `~/.hermes`) reaches BlueOcean today only as
an MCP server (`mcp_servers.blueocean`). Hermes never calls an MCP server on
its own, so recall happens only when the agent remembers to call
`memory_search`. The `blueocean-memory` skill and step 0 of the
`agent-delegation` skill ask it to, which is a compliance-dependent mechanism
(see the 2026-09-06 "End-of-session memory reliability" map: a tier that
depends on agent compliance cannot carry a mechanism alone).

Hermes has a native hook for exactly this: the `MemoryProvider` ABC
(`~/.hermes/hermes-agent/agent/memory_provider.py`). For the active provider,
Hermes itself calls `prefetch(query)` before every turn and injects the result
(fenced in `<memory-context>`), and calls `sync_turn(user, assistant)` after
every turn. Writing BlueOcean as a provider turns recall into a Floor: it
happens every turn and asks nothing of the agent.

This revises the earlier decision on 2026-10-08 (BlueOcean memory
`positioning/competitive-landscape`) to connect Hermes "as an MCP server, not
as a memory provider". That decision accepted "no automatic recall" as its
trade-off and named a native provider as future work.

## 2. Scope

In scope:

- A Hermes `MemoryProvider` plugin, versioned in this repo and symlinked
  into `$HERMES_HOME/plugins/blueocean/`.
- Automatic per-turn recall (`prefetch`) scoped to an automatically detected
  project.
- A per-turn **Turn Trace** written by `sync_turn`.
- A server change: trace kinds are excluded from default search.
- `blueocean-admin prune --kind`.
- An install script and docs (`README.md`, `CONTEXT.md`).

Out of scope (YAGNI, revisit on evidence):

- Automatic pruning of traces.
- LLM-based extraction of decisions from turns.
- `on_session_end`, `on_pre_compress`, `on_delegation`, `on_memory_write`.
- Fan-out search across all projects.
- Changes to Claude Code / Codex / Cursor integration.

## 3. Constraints imposed by Hermes (verified in source)

| Constraint | Source | Consequence |
| --- | --- | --- |
| Only one external provider is active (`memory.provider`) | `agent/memory_provider.py` docstring | The slot is currently free (no provider set) |
| No new in-tree providers; out-of-tree goes to `$HERMES_HOME/plugins/<name>/` | `plugins/AGENTS.md` | Plugin lives in this repo, symlinked |
| Discovery is a text heuristic: `MemoryProvider` must appear in the first 8 KB of `__init__.py`, then the first instantiable subclass is used | `plugins/memory/__init__.py` | Keep the provider class in `__init__.py` |
| External `prefetch` is bounded by an 8 s timeout; a stuck call is skipped on later turns until it returns | `agent/memory_manager.py` (`_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0`) | Our own call timeout must be well below 8 s |
| Prefetch output is stamped into the user turn and replayed every later turn | `agent/memory_manager.py` (`_prefetch_provider`) | Keep the budget small; do not reinject the same entries |
| All ABC methods are synchronous | `agent/memory_provider.py` | A sync-to-async bridge is needed for the MCP client |
| Background work must start via `spawn_context_thread` | `plugins/AGENTS.md` | Bridge thread uses it |
| Credentials via `agent.secret_scope.get_secret`, never `os.environ` | `plugins/AGENTS.md` | Token read via `get_secret("BLUEOCEAN_AUTH_TOKEN")` |
| One process serves several profiles and sessions | `plugins/AGENTS.md` | State keyed by `(hermes_home_key(), session_id)` |
| `agent_workspace` is hardcoded to `"hermes"`; `cwd` is passed only when the agent has a `session_cwd` | `agent/agent_init.py` | Project cannot be derived from the workspace |
| `initialize()` gets `agent_context` (`primary`/`subagent`/`cron`/`flush`); non-primary contexts should skip writes | ABC docstring | `sync_turn` writes only for `primary` |
| Hermes ships `mcp==2.0.0`; `streamable_http_client(url, *, http_client=...)` | Hermes runtime, inspected | Bearer header goes on the supplied HTTP client |

## 4. Decisions

Each was chosen by the user from the listed options.

1. **Project scoping: auto-detect.** Rejected: fan-out search over every
   collection (about 43 today, latency grows with each one, cross-project
   noise); a fixed project in config (needs a config edit on every repo
   switch).
2. **`sync_turn`: write a Turn Trace** (a distinct `kind`, excluded from
   default search). Rejected: no-op (gives up the record of what happened);
   LLM extraction (costs money every turn, risks wrong or duplicate entries,
   and contradicts the README's "explicit write" positioning).
3. **Transport: an MCP client over streamable HTTP** to the existing
   server. Rejected: a new REST API, which would duplicate auth, limits and
   telemetry on a second surface.

## 5. Components

```
blueocean-vector/
├── integrations/hermes/blueocean/
│   ├── __init__.py   BlueOceanMemoryProvider(MemoryProvider), thin
│   ├── bridge.py     sync -> async MCP client on one long-lived loop
│   ├── project.py    per-session project detection (no Hermes import)
│   ├── text.py       formatting, redaction, fence stripping (no Hermes import)
│   └── plugin.yaml
├── src/blueocean_mcp/  trace-kind default exclusion, prune --kind
└── scripts/install_hermes_plugin.sh
```

`project.py` and `text.py` must not import anything from Hermes, so their
tests run without a Hermes install. `__init__.py` imports the Hermes ABC and
wires the pieces together.

### 5.1 Tool surface

`mcp_servers.blueocean` stays configured. The agent keeps using
`memory_store` / `memory_search` / `memory_summarize_session` through it for
explicit writes and deeper reads.

The provider exposes exactly one tool, `blueocean_set_project(project)`, to
correct auto-detection. It does not re-expose any `memory_*` name, so there is
no collision with the MCP server's tools.

## 6. Project detection

State per `(hermes_home_key(), session_id)`: `active_project`, `seen_ids`,
`last_count`.

The rule is that **the latest signal wins**:

1. **Initial default.** If `initialize()` receives `cwd` and its basename is a
   known project, use it.
2. **Mention in the user message.** If the message names a known project,
   switch to it. The project stays sticky for later turns.
   - Boundary is a custom lookaround, `(?<![A-Za-z0-9_-])name(?![A-Za-z0-9_-])`,
     not `\b`. Python treats Thai letters as `\w`, so `\b` fails when a name
     sits directly against Thai text.
   - Case-insensitive.
   - If several projects match, the longest name wins.
   - Names matching `ignore_projects` globs are never candidates.
3. **Explicit tool call.** `blueocean_set_project(name)` switches
   immediately. An unknown name is accepted, but the tool's result says the
   project has no memory yet.
4. **No signal at all.** No active project, so no injection and no trace.

Known projects come from `memory_list_projects`, cached for 5 minutes, so a
project created elsewhere becomes detectable without a restart.

Accepted trade-off: a project whose name is a common word (for example
`kubernetes`, `kops`) can be matched by accident. The remedies are the
`blueocean_set_project` tool and the `ignore_projects` list. A confirmation
step was rejected because it would defeat "automatic".

## 7. Prefetch

`prefetch(query, session_id)`:

1. Return `""` and record `last_count = 0` when `is_trivial_prompt(query)` is
   true (Hermes's helper: greetings, acknowledgements, slash commands), or
   when the session has no active project after detection.
2. Call `memory_search(project, query[:1000], max_tokens=prefetch_max_tokens)`
   with a default of 600. Trace kinds are excluded server-side (section 9).
3. Drop entries whose id is in `seen_ids`. If none remain, return `""`.
   Otherwise add the new ids to `seen_ids`.
4. Format:
   ```
   BlueOcean project memory - <project>
   - [<area>/<module> · <YYYY-MM-DD> · importance <n>] <summary>
   ...
   Details:
   <content of the full-layer hits that survived dedup>
   ```
5. `recall_status()` returns `RecallStatus("BlueOcean (<project>)", n)` for the
   last prefetch only, or `None` when nothing was injected.

`queue_prefetch` is a no-op. A local search takes a few hundred ms, well under
the 8 s budget. Precomputing would search with the previous turn's message,
which gives less relevant results.

`on_session_switch` clears `seen_ids` for the new session (compression,
`/new`, `/reset`, `/resume`), because the earlier injections are no longer
in context. `active_project` carries over unless `reset=True`.

## 8. Turn Trace (`sync_turn`)

Skip when any of these hold:

- `agent_context != "primary"`.
- The session has no active project.
- `sync_turns` is false.
- Both sides are empty after cleaning.

Otherwise enqueue:

```
memory_store(
  project    = <active project>,
  area       = "hermes",
  module     = "turns",
  content    = "User: <=2000 chars>\n\nAssistant: <=2000 chars>",
  summary    = "<first 150 chars of user> -> <first 150 chars of assistant>",
  importance = 1,
  metadata   = {"kind": "hermes_turn", "session_id": ..., "platform": ...},
)
```

- `area`/`module` are fixed. A trace never mints a Module (CONTEXT.md).
- Cleaning happens before truncation:
  1. Strip `<memory-context>...</memory-context>` blocks, so recalled memory
     is never fed back into the store.
  2. Redact secrets with regex, replacing each match with `[REDACTED]`.
     Patterns: `Bearer <tok>`, `sk-...`, `AKIA[0-9A-Z]{16}`,
     `gh[pousr]_...`, `github_pat_...`, `xox[abpr]-...`, and the value in
     `(password|token|secret|api_key)\s*[:=]\s*\S+`.
- `turn_author` is not recorded, to keep personal data such as Discord
  names out of a store every agent reads.
- The write is non-blocking. It goes onto a bounded queue (200 items) drained
  by the bridge loop. On overflow the oldest item is dropped, with one warning
  per outage. `shutdown()` drains for at most 5 s.

## 9. Server changes

### 9.1 Trace kinds excluded by default

New config `BLUEOCEAN_TRACE_KINDS`: a comma-separated env var, default
`hermes_turn`. In `VectorStore.search`:

- If `kind` is not given, the effective `exclude_kinds` is the caller's
  `exclude_kinds` plus the trace kinds.
- If `kind` is given, behavior is unchanged: only entries of that kind are
  returned, so `kind="hermes_turn"` still reaches traces.
- Entries without a `kind` and `session_summary` entries behave exactly as
  before. No data migration is needed.

This must be server-side: Claude Code, Codex and Cursor call `memory_search`
without `exclude_kinds`, and they cannot each be changed. It implements the
half of the 2026-09-06 #7 decision that was never built ("a Trace is worth
writing only if excluded from default search").

`memory_manifest` still lists `hermes/turns`. That is intended: it shows the
traces exist without spending search budget on them.

### 9.2 `blueocean-admin prune --kind K`

Restricts `prune` to entries of one kind, combinable with `--older-days` and
`--dry-run`. Example:

```
blueocean-admin prune blueocean-vector --kind hermes_turn --older-days 30
```

This avoids deleting real low-importance entries the way
`--max-importance 1` would. It is not scheduled automatically in this round.

## 10. Bridge and error handling

- One asyncio loop on a daemon thread started via `spawn_context_thread`.
  The thread holds one `ClientSession` over
  `streamable_http_client(url, http_client=<client with Authorization: Bearer>)`.
- Sync callers submit with `run_coroutine_threadsafe(...).result(timeout=5)`.
- The connection is lazy: Hermes startup never waits on BlueOcean.

| Situation | Behavior |
| --- | --- |
| Connection refused / server down | Circuit breaker opens for 30 s; prefetch returns `""` immediately; one warning per outage |
| HTTP 401 | Clear log message ("BLUEOCEAN_AUTH_TOKEN rejected"); breaker opens |
| Session expired / server restarted | Reconnect once and retry the call |
| Call exceeds 5 s | Return `""` (prefetch) or drop (sync); below Hermes's 8 s ceiling |
| Any other exception | Caught at the provider boundary, logged, never raised into the turn |

Rollback: unset `memory.provider`. The MCP-server path keeps working as it
does today.

## 11. Configuration

`get_config_schema()` fields, set through `hermes memory setup`:

| key | default | notes |
| --- | --- | --- |
| `url` | `http://localhost:8765/mcp` | |
| `auth_token` | none | `secret: true`, `env_var: BLUEOCEAN_AUTH_TOKEN` (already in `~/.hermes/.env`) |
| `prefetch_max_tokens` | `600` | integer, 100-2000 |
| `ignore_projects` | `*test*,*-project,probe-*,*-probe` | comma-separated globs |
| `sync_turns` | `true` | boolean |

`save_config()` writes the non-secret values to
`$HERMES_HOME/blueocean.json`, read per call from the home it is handed.

`is_available()` checks that `mcp` imports and the token resolves, with no
network call. `unavailable_reason()` names whichever is missing.

## 12. Install

`scripts/install_hermes_plugin.sh`:

- Symlinks `integrations/hermes/blueocean` to
  `$HERMES_HOME/plugins/blueocean`, idempotently, in the style of
  `install_skill.sh`.
- Refuses to overwrite a non-symlink directory.
- Does not edit `config.yaml`. It prints the next steps:
  `hermes memory setup`, choose `blueocean`, then restart the gateway (Hermes
  imports providers once per process).

Because it is a symlink, `git pull` / `scripts/update.sh` update the plugin.

## 13. Testing

Tests follow the repo convention: standalone scripts run with
`uv run python -m tests.<name>`.

1. **`tests/hermes_logic.py`.** Pure logic, no Hermes import:
   - project detection (Thai boundary, longest match, ignore globs, latest
     signal wins, sticky across turns, no-signal case)
   - redaction
   - fence stripping
   - truncation
   - formatting
   - `seen_ids` dedup and reset on session switch
   - circuit breaker timing
2. **`tests/trace_kinds.py`.** Server, over stdio:
   - a `hermes_turn` entry is absent from default search
   - it is present with `kind="hermes_turn"`
   - kind-less entries are unaffected
   - `BLUEOCEAN_TRACE_KINDS` override
   - `prune --kind` deletes only that kind
3. **`tests/hermes_bridge.py`.** Against a real server over streamable HTTP
   with auth (pattern from `tests/http_transport.py`): store, prefetch,
   `sync_turn`, then read the trace back with `kind="hermes_turn"`; plus
   breaker behavior with the server stopped.
4. **`tests/hermes_contract.py`.** Loads the provider through Hermes's own
   discovery (`plugins.memory` loader) with Hermes's Python. Skipped when
   `~/.hermes/hermes-agent` is absent.
5. **Manual live check.** Ask Cholly about `blueocean-vector`. Expect the
   `🧠 BlueOcean (blueocean-vector) - recalled N memories` line, then find a
   `hermes_turn` entry via `memory_search(kind="hermes_turn")`.

## 14. Docs

- `CONTEXT.md`: add **Turn Trace**, defined as a record of one Hermes turn
  written by the client. It is a client-written relative of the Session
  Trace, says what was said and never what was decided, and is excluded from
  default search. Avoid: turn log, transcript, chat history.
- `README.md`: a "Hermes integration" section covering install, setup, what
  is recalled and written, and rollback. Mention `BLUEOCEAN_TRACE_KINDS` and
  `prune --kind` under Configuration and Admin CLI.
