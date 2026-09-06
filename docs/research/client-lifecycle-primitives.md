# Lifecycle primitives the real clients expose

Research note for [issue #2](https://github.com/thammarongg/blueocean-vector/issues/2), a
ticket on the map in [issue #1](https://github.com/thammarongg/blueocean-vector/issues/1)
("End-of-session memory reliability"). Planning only — nothing here was implemented.

**Question.** For each client that actually calls this server — `claude-code`,
`codex-mcp-client`, `opencode`, `zcode` — what session-lifecycle mechanism does it expose
as of September 2026, and can any of them be made to run something when a session ends?

**Sources, in order of trust.** Every one of these four clients connected to this server
from *this machine*, so the shipped binaries, config schemas and on-disk state are
available locally and are version-exact. That is the primary source used throughout;
published documentation is used only where the local artefact cannot answer the question,
and is marked as such. Where the installed version differs from the version that actually
connected, the gap is stated.

**Date of research:** 2026-09-06.

---

## 0. The distinction that decides what a positive finding is worth

A lifecycle hook can do two different things, and they feed different tickets:

- **Run a shell command when a session ends.** The agent is already gone, so such a command
  can never *write* a summary — but it can tell the server "session X ended". That is a
  real input to [#6](https://github.com/thammarongg/blueocean-vector/issues/6) (how the
  server knows a session ended).
- **Make the agent act at the end.** Dead regardless of what any client's hook list says:
  [#3](https://github.com/thammarongg/blueocean-vector/issues/3) established the protocol
  cannot carry it and [#5](https://github.com/thammarongg/blueocean-vector/issues/5)
  measured that the agent is never present at an ending.

So a positive result below is always a result of the first kind. It also always arrives
as a *per-tool* mechanism, which the map's standing preferences rank below a
protocol-level one ("breaks every time a new client appears, and two already have"). This
note reports what exists; it does not argue for adopting it.

## Versions under test

| Client (`clientInfo.name`) | Version that actually connected | Version installed now | Source |
| --- | --- | --- | --- |
| `claude-code` | 2.1.258, 2.1.260, 2.1.261, 2.1.263 | 2.1.263 | telemetry `events` table; `claude --version` |
| `codex-mcp-client` | 0.153.0, 0.153.2 | 0.153.4 | telemetry `events` table; `codex --version` |
| `opencode` | 1.18.27 | see §3 | telemetry `events` table |
| `zcode` | 0.16.5 | see §4 | telemetry `events` table |

Read from `data/telemetry.db` (the live database; note that `~/.blueocean/telemetry.db`
is a test fixture, per [#4](https://github.com/thammarongg/blueocean-vector/issues/4)).
The window is narrow — the table holds 2026-09-04 onward — so absence of a client from a
row is not evidence of anything.

---

## 1. `claude-code` 2.1.263

### 1.1 Does it have a session-end hook? **Yes.**

`SessionEnd` is a recognized hook event in the shipped binary. Evidence, all from
`strings` over `~/.local/share/claude/versions/2.1.263` (a 190 MB Mach-O arm64
executable, the resolved target of `~/.local/bin/claude`):

| Symbol / string found in the binary | What it establishes |
| --- | --- |
| `executeSessionEndHooks` | a dedicated execution path for the event |
| `getSessionEndHookTimeoutMs` | the event has its own configurable timeout |
| `markSessionEndedByModel` | the client itself records an end-of-session transition |
| `SessionEnd hook [` | a log/diagnostic line naming the event |
| `Not a recognized hook event. Common events: PreToolUse, PostToolUse, UserPromptSubmit, SessionStart, SessionEnd, Stop. Check spelling and capitalization.` | the client's own validation error enumerates `SessionEnd` as valid |

The last row is the strongest single piece of evidence: it is the client telling the user,
in its own words, that `SessionEnd` is a legal event name at this version.

Compaction is covered by a separate pair, `PreCompact` and `PostCompact`, both present as
exact strings in the same binary. So "context ran out" and "session ended" are
distinguishable events to this client, which matters for the map's open question about
abnormal endings.

The end reasons the binary carries are `clear`, `logout`, `prompt_input_exit` and `other`
(all four present as string literals; `prompt_input_exit` and `other` are both observed at
call sites of the same exit function). Note what that set means: **`clear` is in it.**
`/clear` was classified in [#9](https://github.com/thammarongg/blueocean-vector/issues/9)
as a zero-cost signal the design may read freely, and this client already emits it as a
first-class end reason.

### 1.2 Central or per-project? **Central, and in use on this machine.**

`~/.claude/settings.json` — the user-level file, not a project one — currently configures
twelve hook events: `SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`,
`SubagentStart`, `SubagentStop`, `TeammateIdle`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionRequest`, `PostCompact`. Project-level
`.claude/settings.json` layers on top. So a hook can be installed once for every project
this user opens, which is the shape `install_skill.sh` would need.

`SessionEnd` is *not* among the twelve currently configured. Nothing was found suggesting
it is unavailable — it is simply not wired up here yet.

### 1.3 Does it surface an MCP server's `instructions`? **Yes, once, at the start.**

Directly observed rather than inferred: the `blueocean` server's `instructions` text
appears verbatim in this client's system prompt under a heading `# MCP Server
Instructions`, introduced by "The following MCP servers have provided instructions for how
to use their tools and resources". It is delivered once, at session construction.

This confirms from the client side what
[#3](https://github.com/thammarongg/blueocean-vector/issues/3) established from the
protocol side: `instructions` is a start-of-session channel with no re-send. It is also
the exact text that [#5](https://github.com/thammarongg/blueocean-vector/issues/5)
measured being ignored, so delivery is ruled out as the cause for this client — the text
arrives, in full, every session.

### 1.4 Does it terminate MCP sessions cleanly? **No — see §5.**
