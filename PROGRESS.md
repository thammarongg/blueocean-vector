# Progress (main checkout)

Update at every clock-out (see `AGENTS.md`). Feature work in a worktree is
tracked in that worktree's `.harness/progress.md` (if it has one) and on its
Kanban card; this file only lists where it is.

There is no CI in this repo: `bash scripts/check.sh` is the only definition
of green.

## Current state

- 2026-11-13: feat/mcp-2026-07-28 @ 22f384a (harness onboarding, no code
  change yet). `bash scripts/check.sh` GREEN: uv.lock in sync, ruff clean on
  the whole repo, Qdrant reachable, 27 test scripts, 0 failed. Gates proven
  by negative test: a planted lint error fails ruff, a fake AWS key in a
  tracked file fails R001 (value redacted), uncommitted files make
  clean-state NOT CLEAN.
- Known debt: none in lint.
- Known flake: on the first run in a fresh venv, `tests.auth` (first script
  alphabetically) timed out waiting 20 s for its server; it passed alone in
  2.4 s and on the full rerun. Likely the embedding model's cold load. If it
  recurs, raise `wait_for_server`'s timeout rather than skipping the test.
- R001 scans commits and tracked changes, not untracked files: `git add` a
  new file before relying on it.

## In progress

- `.worktrees/mcp-2026-07-28` (branch feat/mcp-2026-07-28): README comparison
  refresh (#0) and MCP 2026-07-28 compatibility (#1) from
  `docs/market-research-2026-11.md`.

## Next

- Recommendations 2-7 in `docs/market-research-2026-11.md` (Session Trace,
  hybrid search, write hygiene, auto-capture decision, review UI, team mode).

## Blockers

- none
