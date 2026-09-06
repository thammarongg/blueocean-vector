---
name: blueocean-memory
description: Use at the start of any coding session to recall project context stored by other agents/tools, and every time you reach a decision a future session would otherwise have to work out again. Applies when starting work on a project, resuming after a break, switching from another tool (Codex, Cursor, Gemini/Antigravity, Kiro, opencode, zcode), or when the user asks "do you remember", "what's the status", "continue where we left off", or similar.
---

# BlueOcean Memory

A shared, persistent memory reachable via the `blueocean` MCP server
(`http://localhost:8765/mcp`) -- the same server every registered coding
tool on this machine connects to. Memory written by one tool is readable by
any other; it survives switching agents entirely or running out of tokens
in one tool.

## At the start of a session

1. Determine `project` -- use the current working directory's folder name
   unless the user says otherwise.
2. Call `memory_manifest(project)` to see what areas/modules already have
   recorded knowledge. If the project has no memory yet, skip to working
   normally.
3. Call `memory_search(project, query="current state / recent decisions", area=<relevant area>)`
   scoped to whichever area(s) look relevant to the task at hand. Read the
   `summary` layer first; only fetch `memory_get` for a specific entry's
   full content when the summary isn't enough.

Do this BEFORE assuming a project is unfamiliar or asking the user to
re-explain context that may already be recorded.

## Every time you decide something

Call `memory_store(project, area, module, content, summary, importance)`
**at the moment a decision is reached**, while the reasoning is still in
front of you -- not later, and never saved up for the end of the session.

The trigger is a decision a future session would otherwise have to work out
again: why an approach was chosen over the alternatives, what an
investigation concluded, what turned out not to work.

- **importance=5**: architecture decisions, why something was chosen over
  alternatives, anything a future agent would otherwise have to re-derive.
- **importance=3**: routine facts, current status, in-progress notes.
- **importance=1-2**: transient/low-value notes (safe to prune later).

There is no quota. A session that decided nothing writes nothing, and that
is correct rather than a miss.

Keep `area`/`module` consistent with what `memory_manifest` already showed
for this project rather than inventing new names for the same concept.

**Why at the decision, and not at the end:** sessions end without warning,
and an agent is never present at its own ending -- measured across 21 real
sessions on this project, one wrote a closing summary and it did so 76% of
the way through and then kept working. Anything you are waiting to write
does not get written.

## Recording a whole session at once

`memory_summarize_session(project, area, session_id, observations=[...],
conclusion="...")` records a session as a single entry. Worth calling when
the user asks you to wrap up, or when handing over to another tool. It is
**not** a substitute for storing decisions as you reach them.

Pass `module` when you know which module the session worked on. Left
unset it lands in a shared `sessions` module -- it used to default to the
session id, which minted a throwaway module per session and filled
manifests with one-off ids that say nothing about their contents.

## Reading past session records

`memory_search` takes `kind` and `exclude_kinds`:

- `kind="session_summary"` returns only session-level records.
- `exclude_kinds=["session_summary"]` keeps them out of an ordinary search.

Entries stored without a `kind` are never dropped by `exclude_kinds`.

## Notes

- If the `blueocean` MCP server isn't connected/reachable, just proceed
  normally -- don't block work on it.
- Token budget is handled server-side (`memory_search` returns a
  budgeted `summary` + `full` split, default ~2000 tokens); no need to
  manually trim what you read back.
