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

---

## 2. `codex-mcp-client` 0.153.2

The connecting versions were 0.153.0 and 0.153.2; 0.153.4 is installed now. All three are
on disk under `~/.codex/packages/standalone/releases/`, so the version gap costs nothing —
the findings below were read from **0.153.2, the version that actually connected**, and
confirmed identical in 0.153.4.

### 2.1 Does it have a session-end hook? **Yes.**

The binary carries an interned enum of hook event names as one contiguous string. From
`strings` over `.../0.153.2-aarch64-apple-darwin/bin/codex`:

```
PreToolUse PermissionRequest PostToolUse PreCompact PostCompact
SessionStart SessionEnd UserPromptSubmit SubagentStart SubagentStop Stop Interrupt
```

(run together in the binary; spaced here for reading). `SessionEnd` is present, as are
`PreCompact`/`PostCompact` and an `Interrupt` event the other clients do not have.

This is the finding that most directly overturns the 2026-08-18 deferral. That deferral
rested on "each tool would need its own separate mechanism (if it even has one) … unproven
feasibility per-tool". Two of the four clients turn out to expose the *same* event under
the *same* name, in the same `hooks.json`-shaped config format.

### 2.2 Central or per-project? **Central.**

`~/.codex/hooks.json` is a user-level file with the same `{event: [{hooks: [{type,
command, timeout}]}]}` shape Claude Code uses. It currently wires eight events —
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`,
`SubagentStart`, `SubagentStop`, `Stop` — all to one dispatcher script. `SessionEnd` is
available but not wired, exactly as in Claude Code.

### 2.3 A second, older primitive: `notify`

`~/.codex/config.toml` line 1 sets `notify = [<command>, "turn-ended"]`. The binary
identifies this path as `legacy_notify` and carries the payload keys
`agent-turn-complete`, `thread-id`, `turn-id`, `cwd`, `client`, `input-messages`,
`last-assistant-message`.

Worth recording because it is *turn*-scoped, not session-scoped, and it hands the caller
`last-assistant-message`. It fires at the end of every turn — which is to say, repeatedly,
including at the last turn before a session goes quiet. For a design whose floor is
save-as-you-go, a per-turn signal is arguably a better fit than a per-session one. But it
is marked legacy in the binary, superseded by the hook system above.

### 2.4 Does it surface an MCP server's `instructions`? **Not established.**

No `server_instructions`, `mcp_instructions` or equivalent string was found in the binary,
against 435 occurrences of `instructions` overall — the ones that exist are
`base_instructions`, `developer-instructions`, `plugins_instructions`,
`subagent_developer_instructions`, `classifier_instructions`, none MCP-server-scoped.

Stated as a negative result with its limit: absence from the string table is weaker
evidence than the positive matches above, because the field could be handled without a
distinctly-named literal. What can be said is that no primary source on this machine shows
codex surfacing the field, and this server's `instructions` text has never been observed in
a codex transcript.

### 2.5 Does it terminate MCP sessions cleanly? **Yes — and it is the only one that does. See §5.**

---

## 3. `opencode` 1.18.27

Version note: 1.18.27 connected, the binary at `~/.opencode/bin/opencode` is now 1.18.29,
and the typed plugin/SDK packages vendored under `~/.config/opencode/node_modules/` are
1.18.13. The findings below come from the vendored **type declarations**, which are the
oldest of the three — so anything found there is, if anything, understated for the version
that connected.

### 3.1 Does it have session lifecycle events? **Yes — the richest of the four.**

`@opencode-ai/sdk/dist/gen/types.gen.d.ts` declares a typed event union. The
session-scoped and server-scoped members:

| Event `type` | Payload |
| --- | --- |
| `session.idle` | `{ sessionID }` |
| `session.created` | — |
| `session.deleted` | — |
| `session.compacted` | — |
| `session.error` | — |
| `session.status` | `{ sessionID, status }` |
| `session.updated`, `session.diff` | — |
| `server.connected` | — |
| `server.instance.disposed` | — |

`EventSessionIdle` is declared exactly as:

```ts
export type EventSessionIdle = {
    type: "session.idle";
    properties: {
        sessionID: string;
    };
};
```

This is the closest thing any of the four clients has to the primitive
[#6](https://github.com/thammarongg/blueocean-vector/issues/6) is looking for. It is not
"the session ended" — it is "the session went quiet", which is precisely the
idle-timeout-shaped signal #6 provisionally settled on, except computed by the client
instead of guessed at by the server. `session.deleted` and `server.instance.disposed`
cover the harder endings.

### 3.2 How is it consumed? **A plugin, installed as an npm dependency.**

`@opencode-ai/plugin/dist/index.d.ts` declares the `Hooks` interface a plugin returns.
Relevant members: a generic `event?: (input: { event: Event }) => Promise<void>` — a
firehose over the union above — plus `dispose?: () => Promise<void>`,
`experimental.session.compacting`, `chat.message`, `chat.params`, `chat.headers`,
`permission.ask`, `command.execute.before`, `tool.execute.before`, `config`, `auth`,
`provider` and `tool`.

Two of these matter here. `event` means a plugin can subscribe to `session.idle` without
polling. `dispose` gives the plugin a shutdown callback of its own.

### 3.3 Central or per-project? **Central.**

`~/.config/opencode/opencode.jsonc` is the user-level config (it is where this machine's
`mcp.blueocean-vector` remote entry lives), and `~/.config/opencode/package.json` carries
the plugin dependency — currently `@opencode-ai/plugin@1.18.13`. So a plugin installs once
per user, not once per project.

### 3.4 Does it surface an MCP server's `instructions`? **Not established.**

Not answerable from the vendored type declarations, which describe the plugin and SDK
surface rather than prompt assembly. No opencode transcript on this machine was available
to check by observation, as was possible for `claude-code` (§1.3). Recorded as unknown, not
as a negative.

### 3.5 Does it terminate MCP sessions cleanly? **Yes. See §5.**

---

## 4. `zcode` 0.16.5 — the negative result

Version note, stated because it is unresolved: the client identifies itself to this server
as `zcode` version **0.16.5**, but the application on disk (`/Applications/ZCode.app`) is
version **3.11.2** (`CFBundleVersion 3.11.2.6792`). These are different numbering schemes —
most likely the app version and an embedded agent/CLI version — and no artefact was found
that maps one to the other. The findings below are from the shipped 3.11.2 bundle. If the
`0.16.5` component is versioned independently, they may not hold for it.

### 4.1 Does it have hooks? **Yes — modelled on Claude Code's.**

`/Applications/ZCode.app/Contents/Resources/app.asar` (a 307 MB Electron archive) carries a
Zod schema whose `hookEventName` is a closed enum:

```js
hookEventName: t.enum(["SessionStart","UserPromptSubmit","PreToolUse",
                       "PermissionRequest","PostToolUse","PostToolUseFailure","Stop"])
```

The same seven-member array appears at three independent sites in the bundle. Supporting
evidence that this is a Claude Code lineage rather than a coincidence:
`~/.zcode/cli/config.json` enables plugins named `code-review@claude-plugins-official`,
`superpowers@claude-plugins-official`, `commit-commands@claude-plugins-official` and
`feature-dev@claude-plugins-official` — the same marketplace this machine's Claude Code
uses.

### 4.2 Does it have a session-end hook? **No.**

`SessionEnd` is absent from that enum, and the enum is closed. The 18 `SessionEnd`
substring hits in the bundle are a **false positive** and are recorded here so the negative
is not later re-litigated: they are all framer-motion drag-gesture code
(`onSessionEnd`, `handlePointerUp`, `dragSnapToOrigin`), a UI animation library, unrelated
to agent sessions.

`PreCompact` is absent outright; the 18 `PostCompact` hits are likewise false positives —
`postCompactTokenCount` and `truePostCompactTokenCount` telemetry fields, not an event.

The closest thing zcode has is **`Stop`**, which fires at the end of an assistant response,
not at the end of a session. Turn-scoped, like codex's `notify` (§2.3), and for the same
reason it is a plausible carrier for a save-as-you-go floor and no use at all for detecting
an ending.

### 4.3 Central or per-project? **Central.**

`~/.zcode/cli/config.json` is user-level and already holds this machine's `mcp.servers`
entry for the server, so hooks configured there would apply across projects. Hook
configuration was not present in that file to confirm the key name by observation.

### 4.4 Does it surface an MCP server's `instructions`? **Not established.**

Not determined from the bundle, and no zcode transcript was available on this machine to
check by observation.

### 4.5 Does it terminate MCP sessions cleanly? **No. See §5.**
