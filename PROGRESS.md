# Progress (main checkout)

Update at every clock-out (see `AGENTS.md`). Feature work in a worktree is
tracked in that worktree's `.harness/progress.md` (if it has one) and on its
Kanban card; this file only lists where it is.

There is no CI in this repo: `bash scripts/check.sh` is the only definition
of green.

## Current state

- 2026-11-19: main @ ee5b772, merging origin/main after PR #12
  (feat/search-budget-4000, merged on GitHub as ad04121) into the 13
  previously unpushed main commits. `bash scripts/check.sh` GREEN: 29 test
  scripts, 0 failed, R001 pass. PR #12: default search budget 2000 -> 4000
  and passed to the container by docker-compose; the skill and the server
  instructions now say how to turn a folder name into a valid project name.
  Pushed: origin/main = main.
- 2026-11-19: live on this machine. `scripts/update.sh` rebuilt the 8765
  container from main @ f9cef1f (Qdrant untouched) and synced
  `~/.agents/skills/blueocean-memory/SKILL.md` (old copy backed up beside
  it). The local `.env` still said `BLUEOCEAN_MAX_TOKENS=2000`, which compose
  now passes through, so it was set to 4000 and the container recreated. A
  real `memory_search` without `max_tokens` returns `"budget": 4000`.
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
- The live container (port 8765) runs main @ f9cef1f since 2026-11-19, so
  it includes feat/mcp-2026-07-28 (mcp 2.3.0, derived sessions,
  `session_source`, the Hermes client name, the new tool descriptions).

## In progress

- `.worktrees/mcp-2026-07-28` (branch feat/mcp-2026-07-28): merged into
  main (2df100a) and pushed 2026-11-19. The worktree still exists and is
  locked by its owner (claude-code 5a1cb669); removing it is that owner's
  call. Contains the harness onboarding,
  the 2026-11 market research, the README comparison refresh (#0) and
  MCP 2026-07-28 compatibility (#1). Design:
  `docs/superpowers/specs/2026-11-13-mcp-2026-07-28-compat-design.md`.

## Next

- `.env.example` variables docker-compose never passes to blueocean-mcp:
  `BLUEOCEAN_TOP_K`, `BLUEOCEAN_QDRANT_API_KEY`, `BLUEOCEAN_HNSW_M`,
  `BLUEOCEAN_HNSW_EF_CONSTRUCT`, `BLUEOCEAN_HEALTH_EMBED_TTL`,
  `BLUEOCEAN_SESSION_IDLE_SECONDS`, `BLUEOCEAN_EMBEDDING` (an
  `--embedding` flag exists but compose doesn't pass it), and the provider
  settings `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `AWS_REGION`,
  `BLUEOCEAN_BEDROCK_MODEL_ID`, so openai/bedrock can't work under compose.
  `BLUEOCEAN_EMBED_MODEL`/`_EMBED_CACHE_DIR` are fixed at image build;
  `BLUEOCEAN_QDRANT_URL` is covered by `--qdrant-url`.
- The project-name rule can still yield invalid names (a leading `_`, more
  than 100 characters) and doesn't mention `_suggest_slug`'s empty-result
  fallback `project`. The memory_* tool descriptions, which 2026-07-28
  clients see when they skip `instructions`, don't carry the rule yet.
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
