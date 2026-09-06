# Finding a denominator for silent sessions

Research for issue #4, under the map in issue #1 ("End-of-session memory
reliability"). Planning only: nothing here builds a counter and nothing here
modifies the server.

**Question.** Telemetry can only see sessions that called the server. How
many coding sessions happened in `blueocean-vector` that never called it at
all?

**Short answer.** A denominator exists and is cheap to compute. The best
single source is the coding agents' own on-disk session records — one
JSONL file per session for Claude Code and Codex, one SQLite row per session
for OpenCode. Together they show roughly **68 top-level sessions** in this
repo since it was created, against **32 tool calls** the server recorded for
this project. In the only window where both sides have data (2026-09-04 to
2026-09-06) the ratio is about **33 sessions to 10 calls**, and
`memory_summarize_session` fired in **1** of them.

Everything below is metadata only. No conversation content was read, quoted
or copied; the extractions used `jq` field selectors and `sqlite3` column
selects restricted to ids, paths, timestamps and counts.

---

## 0. Two corrections that change how the numbers read

**The repo is 20 days old, not 90.** First commit is
`2026-08-17 23:11:43 +0700` (`git log main --reverse --format=%ci`). There is
no 90-day history to count. Every "90-day" figure in the map is a
retention-window label, not a data span.

**Telemetry itself only holds 3 days.** `~/.blueocean/telemetry.db` is a test
fixture (agent `mcp`, projects `full-coverage-project`, `auth-test-project`).
The live database is the compose bind mount at
`/Users/thammarongg/Projects/Repo/blueocean-vector/data/telemetry.db`
(`docker-compose.yml:51` sets `BLUEOCEAN_TELEMETRY_DB=/data/telemetry.db`).
Its oldest event is **2026-09-04**; 76 events total. So the numerator the map
quotes covers three days, not ninety. Any denominator has to be cut to the
same three days to mean anything today.

Live telemetry, all projects, whole file:

| agent | calls | distinct `session_id` | span |
|---|---|---|---|
| `codex-mcp-client` | 40 | 13 | 09-04 → 09-06 |
| `claude-code` | 27 | 1 | 09-04 → 09-06 |
| `opencode` | 5 | 2 | 09-04 → 09-04 |
| `zcode` | 4 | 1 | 09-05 → 09-06 |

Scoped to `project = 'blueocean-vector'`: 32 calls —
`memory_search` 17, `memory_manifest` 8, `memory_store` 4,
`memory_summarize_session` 2, `memory_get` 1. `zcode` never called the server
for this project at all.

**The server's `session_id` is not a session unit for HTTP clients.** The id
`9328de9ef3b24dc8a30924aff9c4c9b2` is shared by *both* `claude-code` and
`zcode` and spans three days. On stdio (`codex-mcp-client`) the per-process
id behaves correctly — 13 distinct ids in three days. So the numerator is
per-session only for stdio clients; for the streamable-HTTP clients it
collapses to one row and has to be reconstructed some other way. See §7.

---

## 1. Claude Code transcripts — the strongest candidate

**Path.** `~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl`, one file
per session. This repo:

- `/Users/thammarongg/.claude/projects/-Users-thammarongg-Projects-Repo-blueocean-vector/` — **46** `.jsonl` files
- `/Users/thammarongg/.claude/projects/-Users-thammarongg-Projects-Repo-blueocean-vector--claude-worktrees-agent-afa8faf5c188b3062/` — **2** files

**Format.** Newline-delimited JSON. Every line carries `sessionId`, `cwd`,
`timestamp` (ISO-8601 UTC), `type` and `isSidechain`. A counter needs only
the first and last line of each file. Some sessions also have a sibling
directory `<session-uuid>/tool-results/` holding overflowed tool output.

**Readable?** Yes, `0600`, owned by the user, no elevation needed.

**Attribution.** Exact. The directory name is the slugified working
directory, and every line repeats `cwd` verbatim. Attribution to a point in
time is exact too (per-line ISO timestamps).

**Retention.** Governed by `cleanupPeriodDays` in `~/.claude/settings.json`,
which is **not set** there — so the default (30 days) applies. The oldest
surviving file is `1922ad9f-…` at `2026-08-17T16:11:44Z`, i.e. the day the
repo was created, so nothing has aged out *yet*. From roughly 2026-09-16 the
window starts closing behind us at one day per day.

Two sessions have already vanished early:
`7d6e4110-91de-49f3-a3c1-b5935950a5d4` (2026-08-26) and
`b0e1378d-2aee-40e5-9107-ff885b19e744` (2026-08-18) appear in
`~/.claude/history.jsonl` with `project` = this repo but have no `.jsonl`
anywhere under `~/.claude/projects`. Age does not explain it — older files
from 2026-08-17 survive. So transcript loss is possible and is not purely
age-based.

**Session count.** All 46 files contain a non-sidechain `user` line, contain
exactly one distinct `sessionId`, and carry zero `isSidechain: true` lines —
so each file is one top-level session and there are no subagent transcripts
mixed in. Repo lifetime: **46 + 2 worktree = 48**. Since 2026-09-04: **24**.

**The inflation problem.** Not all 48 are human sessions. Several files are
10–16 lines and cluster within seconds of each other (e.g. `4edc61e7…`,
`ac74d9a0…`, `b2bc1453…` all at `2026-09-06T08:32:0xZ`) — spawned agent
processes, not someone sitting down to work. Each is nonetheless a separate
OS process and therefore a separate MCP client, so for "did this client
open memory" they *do* count; for "did a human's work get remembered" they
do not.

**A cheaper, human-only index.** `~/.claude/history.jsonl` (215 KB, `0600`)
holds one line per *typed* prompt with `project`, `sessionId` and epoch-ms
`timestamp`. It goes back to **2026-07-06** — further than transcripts — and
survives transcript deletion (that is how the two orphans above were found).
For this repo it shows **17** distinct sessions, by day:

```
08-17 ×1  08-18 ×1  08-20 ×1  08-26 ×2  08-31 ×1
09-02 ×2  09-03 ×2  09-04 ×2  09-06 ×5
```

15 of those 17 also have a transcript. It misses `-p`/programmatic and
spawned-agent sessions entirely, which is exactly why it is the better proxy
for *human* sessions.

**Cost to use.** Very low. `ls` the two project directories plus any
`*--claude-worktrees-*` sibling, then `head -1`/`tail -1` each file, or just
`jq` over `history.jsonl`. Seconds, no parsing of message bodies.

---

## 2. Codex rollouts

**Path.** `~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl`, plus
`~/.codex/archived_sessions/` (7 files, flat). 363 files total.

**Format.** JSONL whose first line is `type: "session_meta"` with
`payload.session_id`, `payload.cwd`, `payload.timestamp`,
`payload.originator` (`codex-tui` / `Codex Desktop` / `codex_exec`),
`payload.cli_version` and `payload.source`.

**Readable?** Yes, user-owned, no elevation.

**Attribution.** Exact — `payload.cwd` on line 1, and the date is in the
directory path *and* the filename *and* the payload.

**Subagents are distinguishable, which matters.** `payload.source` is the
string `cli` / `vscode` / `exec` for a top-level session, and an *object*
for a spawned one: `{"subagent":{"thread_spawn":{"parent_thread_id":…}}}` or
`{"subagent":{"other":"guardian"}}`. Across all 363 files: 130
`thread_spawn`, 96 `guardian`, 100 `cli`, 32 `vscode`, 5 `exec`. A counter
must filter on this or it will roughly triple the denominator. (Note for
whoever writes the extractor: `payload.source` breaks `@tsv`/`@csv` in jq
when it is an object — use `tostring`.)

**Retention.** Oldest surviving session is `2026-07-23`; no retention key in
`~/.codex/config.toml`. Files appear to be kept indefinitely, so codex's
window is the longest of the CLI agents (~45 days here).

**Session count for this repo.** 20 rollouts mention this `cwd`; **14** are
top-level (`cli` ×8, `vscode` ×2, `exec` ×2 … see below) and 6 are subagent.
Since 2026-09-04: **8** top-level.

```
08-17 Codex Desktop/vscode     09-04 codex_exec/exec  ×2
08-18 Codex Desktop/vscode     09-04 codex-tui/cli    ×5
08-20 codex-tui/cli            09-04 codex-tui/cli    (07-33 ×2)
08-26 codex-tui/cli
09-02 codex-tui/cli
09-03 codex-tui/cli
```

**Other codex indexes, and why they are worse.** `~/.codex/session_index.jsonl`
(29 KB) has `id`, `thread_name`, `updated_at` but **no cwd** — unusable for
project attribution. `~/.codex/history.jsonl` (956 KB) has `session_id` and
`ts` but also no cwd. `~/.codex/thread_history_1.sqlite` is 163 MB and
`logs_2.sqlite` is 292 MB — large enough that touching them casually is a
bad idea when the rollout headers already answer the question.

**Cost to use.** Low. `head -1` on 363 small files, one `jq` filter. Under a
second.

---

## 3. OpenCode

**Path.** `~/.local/share/opencode/opencode.db` (56 MB SQLite). `~/.opencode`
is only the npm install (`node_modules`, `bin`) and holds nothing useful.

**Format.** Proper relational schema. `session` has `id`, `project_id`,
`parent_id`, **`directory`**, `title`, `agent`, `model`, `time_created`,
`time_updated` (epoch ms), plus token/cost columns. `project` and
`project_directory` map ids to worktree paths.

**Readable?** Yes — opened successfully with
`sqlite3 -readonly 'file:…?immutable=1'`. No lock contention observed.
(`immutable=1` skips the WAL, so a session started in the last minutes may
be missing; copy `db` + `-wal` + `-shm` to a scratch dir if that matters.)

**Attribution.** Exact and already indexed: `WHERE directory LIKE
'%blueocean-vector%' AND parent_id IS NULL`. `parent_id` cleanly separates
subagent sessions.

**Retention.** No pruning evident; 28 sessions total, oldest 2026-08-17
10:02 UTC — which is also about when the DB was created, so the true
retention policy is untested.

**Session count for this repo.** **6** top-level sessions, all with
`agent = 'build'`, none with a `parent_id`:

```
2026-08-17 10:13   2026-09-03 17:45
2026-08-17 18:19   2026-09-03 17:52
2026-08-20 17:07   2026-09-04 06:56   (UTC)
```

Since 2026-09-04: **1**.

**Cost to use.** Lowest of all candidates — a single SQL query, exact
answer, no file walking.

---

## 4. zcode — exists, but keeps almost nothing

**Paths.** `~/.zcode/cli/db/db.sqlite` (8.3 MB, plus a 4 MB WAL);
`~/.zcode/cli/rollout/model-io-sess_*.jsonl`; `~/.zcode/cli/exec/sess_*/`;
`~/.zcode/v2/` (a separate app layer with `tasks-index.sqlite`, which holds
automations and task groups, not sessions).

**Format.** `cli/db/db.sqlite` has an OpenCode-shaped `session` table —
`id`, `project_id`, `parent_id`, `directory`, `time_created`, plus
`task_type` (`interactive` / …) and `trace_id`.

**Readable?** Yes. Reading it properly needs the WAL: copy
`db.sqlite`, `db.sqlite-wal` and `db.sqlite-shm` together, because
`immutable=1` alone would miss recent rows.

**The problem: it prunes hard.** With the WAL included the whole table holds
**6 sessions**, all `directory = …/tidepool`, and **zero** for
`blueocean-vector`. Meanwhile `~/.zcode/cli/exec/` still has per-session
scratch directories from 2026-08-25 whose rows and rollouts are gone, and
`cli/rollout/` retains only 3 `model-io-*.jsonl` files. A `grep -rl` for
`blueocean-vector` across all of `~/.zcode` matches exactly one file — a
model-io rollout for a session whose `directory` is elsewhere.

This is consistent with telemetry: `zcode` made 4 calls, none of them scoped
to `project = 'blueocean-vector'`.

**Verdict.** Not usable as a denominator. Its retention is short and
apparently opportunistic, and it has no record of ever having worked in this
repo.

---

## 5. Gemini CLI / Antigravity, and Cursor — "not used here", not "silent"

The map lists `cursor`, `gemini` and `kiro` as install targets with zero
traffic. Their on-disk state says why.

**Gemini CLI.** `~/.gemini/tmp/blueocean-vector/` and
`~/.gemini/history/blueocean-vector/` both exist, created
`2026-08-17 19:04:06`, and each contains exactly one file: `.project_root`.
No chat, no session index, no timestamps beyond the directory mtime. So
gemini was launched in this repo once, around the day it was created, and
kept no per-session record. It yields a boolean plus one mtime — enough to
say "used here at least once", not enough to count.
`~/.gemini/antigravity/conversations/` holds 6 `.db` files, none of which
mention this repo.

**Cursor.** `~/Library/Application Support/Cursor/User/workspaceStorage/`
has 10 workspaces; `8166d7e8650ba492c4fac22c43e81303/workspace.json` maps to
`file:///Users/thammarongg/Projects/Repo/blueocean-vector`. Its
`state.vscdb` was last written `2026-08-21 23:05:02` and holds editor state
(open editors, view layout, MCP/skill snapshots) — not a session log.
Cursor's chat index lives separately in
`…/globalStorage/conversation-search.db`, whose `conversations` table has 42
rows keyed by `root_fingerprint` (an opaque hash) with `updated_at` — the
fingerprint is not the same hash as the workspaceStorage directory name, so
attributing a conversation to a project directory would need reverse
engineering. Not worth it: nothing points to this repo after 2026-08-21.

**Kiro.** No trace on this machine at all.

**Verdict.** These three are not silent sessions being missed. They are
tools that were not used on this project. That is worth writing into the map
under "Which clients the install path should support" — the absence is
explained, not mysterious.

---

## 6. Weak signals, checked and discounted

**Shell history.** `~/.zsh_history`, 1141 lines, `0600`, last written
2026-09-04. Three disqualifying problems: (a) `EXTENDED_HISTORY` is off —
`grep -cE '^: [0-9]+:[0-9]+;'` returns 0, so **there are no timestamps at
all**, only command text in order; (b) there is no working directory on any
entry; (c) the coding agents run their `Bash` tool through non-interactive
shells, so agent activity never lands here — it records only what the human
typed in Terminal. It mentions `blueocean` 13 times, which cannot be dated or
attributed to a session. Unusable.

`~/.zsh_sessions/` holds one stale session from 2026-07-03. Also unusable.

**Git commits.** `main` has 43 commits since the repo was created, on **5
distinct days**: 08-17, 08-18, 09-02, 09-04, 09-06. Against 17 human Claude
sessions plus 14 codex plus 6 opencode, commits under-count heavily —
research, review and planning sessions leave no commit. Useful as a floor
("at least this many working days"), useless as a denominator.

**Git reflog.** `/Users/thammarongg/Projects/Repo/blueocean-vector/.git/logs/HEAD`,
44 lines, plain text, epoch-stamped, readable with `cat`. Same information
as the commit log for this repo (no rebases or resets to speak of), and
`gc.reflogExpire` defaults to 90 days so it would age out anyway. No
advantage over commit timestamps.

**Commit trailers — the one real join key, and it is coarse.** 20 of 43
commit messages carry `Claude-Session: https://claude.ai/code/session_…`.
But only **two distinct values** appear (19 commits share
`session_01CxhEdjuw4yc9Cq36TfhELf`). That id is the *conversation* id, stable
across `--resume` and across many transcript files, so it identifies a
stretch of work rather than a process. It is genuinely useful for joining
git → telemetry (see next section), but it is not a session counter.

---

## 7. Joining the numerator to the denominator

The telemetry column `agent_session_label` already carries client-side
session identity when the caller supplies it, and it uses two different
namespaces:

| agent | label values seen |
|---|---|
| `claude-code` | `session_01QWtXevjj8Vh2F16Vej7sjX`, `session_01VNVMEmcVs8vmpzAsRwe8RL`, `561913b5-43ab-4575-a34d-9d634edcf492` |
| `codex-mcp-client` | `task_faaad2ede2f4`, `sprint17-…-task14/15/16`, `sprint17-…-codex` |
| `opencode` | `task_44b0ece16fca` |

Two of these are joinable today, and it is worth being precise about which:

- `561913b5-43ab-4575-a34d-9d634edcf492` **is** the filename of a transcript
  in `~/.claude/projects/-Users-…-blueocean-vector/`. That is an exact,
  already-working join from a telemetry row to a session on disk. It arrived
  as the `session_id` argument of a `memory_summarize_session` call.
- `session_01QWtXevjj8…` is the same namespace as the `Claude-Session:`
  commit trailer, so telemetry rows and commits can be joined — but at
  conversation granularity, not session granularity.

So the join key exists but is populated opportunistically: 10 of the 32
`blueocean-vector` calls carry a label; the rest collapse into the single
useless HTTP `session_id`. Anyone building the counter later should note that
the *sessions that close correctly are exactly the ones that identify
themselves*, which biases any label-based numerator upward.

---

## 8. The counts, side by side

**Repo lifetime, 2026-08-17 → 2026-09-06 (20 days).** Denominator only —
telemetry does not reach back this far.

| source | top-level sessions | subagent/child | window |
|---|---|---|---|
| Claude Code transcripts | 48 (46 + 2 worktree) | 0 | full |
| Claude Code, human-typed only (`history.jsonl`) | 17 | — | full |
| Codex rollouts | 14 | 6 | full |
| OpenCode `session` table | 6 | 0 | full |
| zcode | 0 | 0 | no record |
| Gemini CLI | ≥1 (mtime only) | — | not countable |
| Cursor | ≥1 (mtime only) | — | not countable |
| **total, top-level** | **≈68** | | |
| **total, human-driven** | **≈37** (17 + 14 + 6) | | |

**Telemetry's actual window, 2026-09-04 → 2026-09-06 (3 days).** The only
apples-to-apples comparison available today.

| agent | sessions on disk | sessions that called the server | calls |
|---|---|---|---|
| `claude-code` | 24 processes / **7** human-typed | 3–4 (1 `session_id` + 2 labels + one unlabelled cluster) | 10 |
| `codex-mcp-client` | 8 | 6 distinct stdio `session_id`s | 19 |
| `opencode` | 1 | 1 | 3 |
| `zcode` | 0 | 0 | 0 |
| **total** | **33 processes / 16 human-driven** | **≈10** | **32** |

And the number the map is actually chasing: in that window
`memory_summarize_session` was called **twice** for this project, **once** by
`claude-code` (labelled `561913b5…`) against 7 human-driven Claude sessions.
Codex closed the other one.

Read carefully, that is a close rate of roughly **1 in 8** human sessions,
not the 9-in-19 that comparing `memory_summarize_session` to `memory_manifest`
suggests. Comparing the two tool counts measures "sessions that opened and
then also closed"; it silently conditions on having opened at all.

---

## What this means for the map

**The best available denominator is the union of the agents' own session
records, counted per client:** Claude Code `.jsonl` files under
`~/.claude/projects/-Users-thammarongg-Projects-Repo-blueocean-vector/` and
its `*--claude-worktrees-*` siblings; Codex `rollout-*.jsonl` filtered to
`payload.cwd == <repo>` and `payload.source` a string; and the OpenCode
`session` table filtered on `directory` and `parent_id IS NULL`. All three
are user-readable, project-attributable, timestamped to the second, and cost
under a second to scan. Nothing needs to be built into the server to obtain
it, and nothing needs to be built to keep obtaining it.

It has five known distortions, in rough order of how much they matter.

1. **The windows do not line up, and neither is 90 days.** The repo is 20
   days old and live telemetry holds 3 days. Any rate quoted "over 90 days"
   is quoting a retention setting, not a measurement. Until telemetry
   accumulates, the denominator has to be cut to telemetry's span, which
   makes today's sample about 16 human sessions — enough to see a gross
   failure, not enough to detect a 20% improvement.

2. **The denominator's unit is a process; the interesting unit is a piece of
   work.** 24 Claude transcript files in three days correspond to about 7
   times a human sat down. Spawned agents, `-p` runs and worktree agents each
   get their own file and their own MCP connection. Both numbers are
   defensible and they differ by 3×, so the map must state which one it means
   before it states a rate. `history.jsonl` is the cheap filter for the
   human-driven number, at the cost of missing genuinely headless sessions.

3. **The numerator is broken for HTTP clients in a way the denominator is
   not.** All 27 `claude-code` calls across three days share one server
   `session_id`, shared with `zcode` besides. So "sessions that called"
   cannot currently be counted from telemetry for anything but stdio
   `codex-mcp-client`. Today it has to be reconstructed from
   `agent_session_label` or from call-time clustering — and labels are
   populated exactly by the well-behaved calls, which biases the numerator
   up. This is a separate defect worth its own note in the map; it also means
   the verification loop cannot be built on `COUNT(DISTINCT session_id)` as
   the schema stands.

4. **The denominator has a moving back edge.** Claude Code's
   `cleanupPeriodDays` is unset, so the 30-day default applies and transcripts
   begin ageing out around 2026-09-16 — while `~/.claude/history.jsonl`
   reaches back to 2026-07-06 and survives it. Two transcripts have already
   disappeared ahead of schedule for reasons age does not explain, so a
   snapshot taken today is worth more than one taken later. If the map wants
   a baseline it should be captured now.

5. **Three of the five advertised clients are not silent — they are
   absent.** `cursor` last touched this repo on 2026-08-21 and keeps no
   attributable session log; `gemini` left a single empty project marker on
   2026-08-17; `kiro` has no presence on the machine. Counting them as
   "sessions that skipped memory" would be wrong. `zcode` is a fourth case
   again: it is installed and does call the server, but for other projects,
   and it prunes its own session records so aggressively that it can never
   contribute a denominator.

**What this does not settle.** It gives a denominator, not a mechanism. It
does say something about the shape of the failure, though: the sessions that
call `memory_summarize_session` are also the ones that pass a session label,
i.e. the well-behaved ones are well-behaved throughout, which is weak
evidence that the gap is structural (a session ends without the agent
getting a turn to act) rather than a matter of the instruction's wording.
That is the question ticket #2 and ticket #3 are for; this ticket only
supplies the number they will be measured against.
