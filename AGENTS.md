# blueocean-vector — agent guide

The startup path and the working rules for any coding agent (Claude Code,
Codex, OpenCode, ZCode, Orca, Hermes workers). Layout and local dev:
`README.md`. Vocabulary: `CONTEXT.md` (read it before naming anything new).
Workspace-wide rules: `~/Projects/AGENTS.md`.

## Clock-in (before touching code)

1. `git status -sb`, `git log --oneline -10` and `git worktree list -v`:
   which checkout are you in? Work only where your task says.
2. Ownership: a `locked: owner: ...` line under a worktree means another
   tool or session owns it. Treat it as read-only and create your own
   worktree instead (see "Working across tools").
3. Read the state, latest entry first: `PROGRESS.md`, or
   `.harness/progress.md` in a worktree that has a `.harness/` (then follow
   `.harness/README.md` too).
4. If the `blueocean` MCP is available, search project memory
   (project = `blueocean-vector`) for decisions and dead ends on your
   task. Memory explains why; git shows what is true now. When they
   disagree, trust git.
5. Run `bash scripts/check.sh`. It must end with `check.sh: GREEN`. If it's
   red, fix it first when the failure is in your scope, otherwise stop and
   report it. Never start new work on a red base. It needs the local Qdrant
   running (`docker compose up -d qdrant`).

## Definition of done

- `bash scripts/check.sh` exits 0 after your last edit, plus the runtime
  check your task names (a real MCP call against a started server, a real
  Qdrant round trip). Code written or "looks right" is not done.
- The tests are standalone integration scripts against a real Qdrant, not
  mocks. Behaviour such as transport handling, auth refusal, concurrency
  and TLS is proven against a started process on a test port (879x), never
  against the live server on 8765.
- Report the exact commands and their real output. Never carry results over
  from an earlier run.

## Scope

- One task at a time. Finish it or revert it before starting another.
- Change only the files your task names. Ideas outside scope go under
  "Next" in `PROGRESS.md`; don't act on them.
- Project pointers: design specs and plans in `docs/superpowers/`, research
  notes in `docs/`, decisions on the session-end map in GitHub issues #1-#10.
- Never push, deploy, or touch production, real external services, real
  mail or real credentials unless the task explicitly allows it. The live
  server's memory (other projects' collections in the shared Qdrant) is
  production: never prune, reset or delete a collection a test didn't make.

## Stop conditions

Stop, write the state, commit a clean checkpoint and report (with the
command, the error and what you tried) when ANY of these is true:

- The same check fails with the same error 3 runs in a row and your last
  change didn't alter it.
- You've used your task's budget (default: 3 fix attempts per failing
  check).
- The blocker is outside your scope: environment, dependency, credentials,
  a decision, or a file you don't own.
- Your context is running low. Don't rush to finish; go to Clock-out.

## Throwaway resources

- Start test databases with `docker run --rm`, bound to `127.0.0.1` only,
  and stop them before you finish.
- Scope dummy env vars to one command (`env VAR=... cmd`). Never `export`
  them: other packages may read the same names.

## Working across tools

Hermes, Claude Code, Codex, OpenCode, ZCode and Orca each have their own
orchestration, and none of them sees the others' task lists. This file,
git and project memory are what they share.

- One worktree, one owner. Before writing in a linked worktree, claim it:
  `git worktree lock --reason "owner: <tool> <card/session id> (<date>)" <path>`.
  At clock-out, if you're done with it: `git worktree unlock <path>`. The
  main checkout can't be locked: use it only for short tasks and do longer
  writer work in a worktree (this repo keeps them in `.worktrees/`, which
  is excluded locally via `.git/info/exclude`).
- State lives in git: commits, `PROGRESS.md`, `.harness/progress.md`.
  Another tool must be able to resume from those alone, without your chat
  history.
- If the `blueocean` MCP is available, store a decision, a dead end or a
  gotcha in project memory when it happens, not at the end of the session.
  Don't store what git already shows (code structure, file lists, what
  changed). Never store secrets.

## Clock-out (also when out of time or blocked)

1. Leave no half-done change: finish it, or revert it with a WIP commit
   and say so.
2. `bash scripts/check.sh` → GREEN, then small Conventional Commits whose
   message says why.
3. `bash scripts/clean-state.sh` → `CLEAN` (no uncommitted changes, no
   leftover test containers).
4. Update the state file: `PROGRESS.md` (main) or `.harness/progress.md`
   (worktree): current state (commit, check result, date), next, blockers.

## Promoted rules

A mistake that reviews catch twice becomes a check, not more prose here:
one script per rule in `scripts/rules/R<NNN>-<name>.sh` with a
WHAT / WHY / FIX header. `scripts/check.sh` runs them all.
