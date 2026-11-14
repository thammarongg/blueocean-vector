# Progress (main checkout)

Update at every clock-out (see `AGENTS.md`). Feature work in a worktree is
tracked in that worktree's `.harness/progress.md` (if it has one) and on its
Kanban card; this file only lists where it is.

There is no CI in this repo: `bash scripts/check.sh` is the only definition
of green.

## Current state

- 2026-11-13: feat/mcp-2026-07-28 @ f02b9fd. `bash scripts/check.sh` GREEN:
  uv.lock in sync, ruff clean on the whole repo, Qdrant reachable, 28 test
  scripts, 0 failed. Harness gates proven by negative test: a planted lint
  error fails ruff, a fake AWS key in a tracked file fails R001 (value
  redacted), uncommitted files make clean-state NOT CLEAN.
- Known debt: none in lint.
- Known flake: on the first run in a fresh venv, `tests.auth` (first script
  alphabetically) once timed out waiting 20 s for its server; it passed alone
  in 2.4 s and on every full run since. Likely the embedding model's cold
  load. If it recurs, raise `wait_for_server`'s timeout rather than skipping.
- R001 scans commits and tracked changes, not untracked files: `git add` a
  new file before relying on it.
- The live container (port 8765) has NOT been rebuilt with this branch. It
  already runs mcp 2.3.0; derived sessions, `session_source`, the Hermes
  client name and the new tool descriptions reach it only after merge and
  `scripts/update.sh`.

## In progress

- `.worktrees/mcp-2026-07-28` (branch feat/mcp-2026-07-28): done and
  reviewed, waiting for the merge decision. Contains the harness onboarding,
  the 2026-11 market research, the README comparison refresh (#0) and
  MCP 2026-07-28 compatibility (#1). Design:
  `docs/superpowers/specs/2026-11-13-mcp-2026-07-28-compat-design.md`.

## Next

- Deferred minors from the final review of feat/mcp-2026-07-28:
  `time.monotonic` may not advance during host sleep, so a laptop closed
  overnight can resume yesterday's derived session; the comment above
  `_session_identity`'s request lookup overstates the fallback (a ctx with no
  request_context fails at `ctx.headers` first, unreachable from a real call);
  any `mcp-session-id` header is trusted as `header` even on a 2026-07-28
  request; `BLUEOCEAN_SESSION_IDLE_SECONDS` is not validated (0/negative
  makes every call a new session); docker-compose does not pass it into the
  container; rejected calls with invalid project names still occupy tracker
  slots (bounded); `check.sh` keeps the template's unused `BASE`/`touched`;
  R001's root-commit fallback range excludes the root commit.
- Optional explicit session handle (SEP-2567 pattern), deferred in the spec.
- Recommendations 2-7 in `docs/market-research-2026-11.md` (Session Trace,
  hybrid search, write hygiene, auto-capture decision, review UI, team mode).

## Blockers

- none
