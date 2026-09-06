# Telemetry and Usage - Design

Status: design settled across grilling rounds 1-6, frontier empty, awaiting
review before planning.
Date: 2026-09-02
Author: design session started 2026-08-31 (transcript `eacc32ad-9ae0-4b39-8e11-0e8825a79ee5`)

## 1. Why

BlueOcean Vector has no usage instrumentation at all. `/health` answers
"is it alive and can it reach Qdrant", and `memory_stats` counts points in a
collection. Nothing records *who* called *what*, how long it took, whether a
search found anything, or what it cost.

That gap is felt in three concrete ways:

1. **No attribution.** On 2026-08-17 a concurrent agent session cleared this
   project's memory entries (likely via `prune`). There was no way to tell
   which agent did it, or when.
2. **No feedback loop on memory quality.** Nobody knows which stored entries
   are ever retrieved. Memory grows monotonically and pruning is guesswork.
3. **No cost visibility.** With `openai` or `bedrock` embedders, every store
   and search is a billed API call, and the token counts the providers return
   are currently discarded.

The motivating reference is Oh My Pi IDE's "AI Usage Statistics" dashboard
(`http://127.0.0.1:3847/`). Reverse-engineering it showed it is an **event log
persisted and aggregated later** (`/api/stats/{overview,tools,behavior,costs,
errors,folders,gain,providers,recent}`), not a set of live counters. That
distinction is the core architectural decision below.

## 2. Scope

In scope, four dimensions:

| Dimension | Answers |
| --- | --- |
| Operational | how many calls, how slow, how many failed |
| Usage | which projects/areas/agents use memory, and how |
| Quality | do searches find anything, is the summary layer enough |
| Cost | embedding tokens and money |

Non-goals:

- **No Prometheus, no `/metrics`.** Rejected by the user. This removes the
  label-cardinality problem (project names are user-chosen, up to 100 chars)
  and the separate `/metrics` auth question.
- **No OpenTelemetry.** Single localhost server, no distributed traces to
  correlate. Revisit if the project moves to cloud, as an exporter.
- **No telemetry in Qdrant.** Poor time-bucketed group-by, I/O contention with
  real memory operations, and decisively: when Qdrant is down is exactly when
  telemetry matters most and would be unwritable.
- **No automatic memory extraction.** Unrelated to this feature.

## 3. Architecture

```
MCP tool call ──▶ instrument() ──▶ bounded queue ──▶ writer thread ──▶ SQLite
                       │                                                  │
                       └── ContextVar accumulator                         │
                           (embed_tokens, embed_ms)                       │
                                                                          ▼
                                              ┌────────────┬──────────────┴──────────┐
                                              │            │                         │
                                        memory_usage   GET /api/stats          GET /dashboard
                                        (MCP tool)     (JSON, authenticated)   (single HTML page)
                                                              ▲
                                                              │
                                              blueocean-admin usage (HTTP client)
```

Event log is the source of truth; every reader is an aggregation over it.
Telemetry is **on by default** and **never leaves the machine**.

## 4. Data model

SQLite, WAL mode, `PRAGMA busy_timeout`. Schema version tracked in
`PRAGMA user_version`; migrations are **additive only** (`ALTER TABLE ADD
COLUMN`). A version mismatch never drops data: 90 days of history is the only
thing this feature is worth. The one sanctioned exception is a data migration
that removes values that should never have been stored (v2 nulls
caller-supplied `error_msg` on `cli-reported` rows; see 8.1). If a breaking
change is ever unavoidable, rename the old file aside rather than deleting it.

### 4.1 `events`

One wide table, NULL where a column does not apply to that event kind. SQLite
stores NULLs cheaply, and every dashboard query becomes a single-table scan
plus index with no joins.

| Column | Notes |
| --- | --- |
| `id` | INTEGER PRIMARY KEY |
| `ts` | INTEGER, UTC epoch seconds (matches the convention used everywhere else in the codebase) |
| `kind` | `tool` or `admin` |
| `tool` | tool or admin subcommand name |
| `project`, `area`, `module` | scope, when the call carried one |
| `agent_name`, `agent_version` | from MCP `clientInfo` |
| `session_id` | transport session (see 4.3) |
| `agent_session_label` | agent-declared session, nullable (see 4.3) |
| `ok` | 0/1 |
| `error_class` | exception class name |
| `error_msg` | server-observed rows only, sanitized and truncated to 200 chars; always NULL on `cli-reported` rows (see 8.1) |
| `total_ms` | whole tool call |
| `embed_ms` | embedding portion only |
| `result_count`, `top_score`, `tokens_returned` | search quality |
| `embed_tokens`, `tokens_exact` | see 5.2 |
| `est_cost_usd`, `unit_price_per_1m`, `price_source` | see 6 |
| `deleted_count` | destructive ops |
| `origin` | `observed` (the server saw the call) or `cli-reported` (see 8.1) |

Indexes: `ts`, `(tool, ts)`, `(project, ts)`, `(agent_name, ts)`.

`total_ms` and `embed_ms` are separate columns because the first question when
something is slow is "the model or Qdrant?", and the answer is a subtraction.
Verified: `VectorStore.store` calls `embed_documents([content])` exactly once
(`vector_store.py:203`) and `VectorStore.search` calls `embed_query` exactly
once (`vector_store.py:264`). No tool embeds more than once per call.

Rough size: about 150-200 bytes per row, so a heavy day of 1,000 calls is
around 180 KB and the full 90-day window lands near 16 MB. Nothing here needs
designing around.

### 4.2 `entry_hits`

`(point_id, project, hits, full_hits, last_seen_at)`, primary key
`(project, point_id)`.

- `hits` increments when an entry appears in a search's `summary` layer.
- `full_hits` increments when it is expanded into the `full` layer, and when
  `memory_get` fetches it (same intent).

`memory_get` increments **both** counters, not just `full_hits`. Counting it as
a full hit alone would leave an entry that is fetched by id ten times sitting
at `hits = 0`, which puts it at the top of the "never retrieved" list and gets
it pruned - the opposite of the truth.

The difference between the two is the signal worth having: high `hits` with
low `full_hits` means the summary was good enough and the entry earns its
keep; `hits = 0` over a long window means it is safe to prune. Collapsing
these into one counter makes the two cases indistinguishable forever.

Writes go through the same background queue as events, batched:
one `INSERT ... ON CONFLICT DO UPDATE` `executemany` per search, not one write
per result.

**`entry_hits` is never purged by retention.** It holds one row per memory
entry, not one per event, so it does not grow with traffic, and "last retrieved
eight months ago" is precisely the signal that makes an entry safe to prune.
Deleting those rows at 90 days would erase the only evidence the panel exists
to show. The cost of keeping it is that this panel measures **since first
observed**, not within the selected 24h/7d/30d window, while every other panel
on the page is windowed. The dashboard must label it that way; two differently
scoped numbers side by side with no label will be misread.

**Orphans are handled in two places**, because one is not enough.
`memory_delete`, `prune` and `restore` delete the matching `entry_hits` rows on
the paths we control. Deletions that bypass our code (a snapshot restore, or
someone operating Qdrant directly) will still leave orphans, so reads also
tolerate them: a `point_id` that no longer exists is simply not shown. Joining
against Qdrant on every dashboard load was rejected for the same reason
telemetry does not live in Qdrant: it couples the two systems on the read
path.

### 4.3 Two notions of session

Three things could be called a "session" and only two are recordable:

1. **Transport session.** `stateless_http` defaults to `False`
   (`mcp/server/mcpserver/server.py:1062`), so streamable-http issues an
   `mcp-session-id` and the client echoes it back; readable via `ctx.headers`.
   On stdio, `ctx.headers` is `None`, so a per-process UUID generated at
   startup stands in.
2. **Agent-declared session.** `memory_summarize_session` already takes a
   free-form `session_id` string (used as `module`, e.g.
   `codex-2026-08-18-project-summary`). Recorded as `agent_session_label`,
   nullable, only when a call carries one. This is the only link from the
   event log back to a real entry in Qdrant.
3. The coding agent's own internal session. Not knowable. Ignored.

Both recorded values are opaque identifiers, not content, so neither violates
the privacy rule in section 9. `mcp-session-id` is client-supplied: it is a
grouping key, never an identity assertion (the library's own docstring warns
about this).

## 5. Instrumentation

### 5.1 One wrapper, applied centrally

`tools.py` defines eight nested plain functions and registers them all at the
bottom of `register_tools()`. The wrapper is applied there, in a loop over the
registration list, **not** by editing each function. A future ninth tool
cannot silently vanish from the stats.

`memory_usage` (section 7.1) is on a deliberate denylist: reading the meter is
not using the memory. See Q4.4 in the decision log. The test asserts
"8 instrumented, 1 deliberately excluded", not "all 9 instrumented".

### 5.2 The ctx-injection hazard (write the test first)

The wrapper needs a `ctx: Context` parameter to reach `clientInfo` and headers.
Two different parts of the library read that parameter from two different
places:

- `find_context_parameter(fn)` uses `typing.get_type_hints(fn)`, i.e.
  `__annotations__` (`utilities/context_injection.py:13-27`).
- `func_metadata(fn, skip_names=[context_kwarg])` reads the **signature** to
  build the agent-visible JSON schema (`tools/base.py`).

A wrapper that sets only `__signature__` or only `__annotations__` fails
**with no error**: either ctx is never injected, or `ctx` leaks into the schema
the agent sees. The wrapper must set both. The test that asserts ctx is
injected *and* absent from the published schema is written before the wrapper.

### 5.3 Embedding accounting: `ContextVar`, not `threading.local`

The providers already receive the numbers and throw them away:
`OpenAIEmbedder.embed()` keeps `resp.data` and discards `resp.usage`;
`BedrockEmbedder.embed()` reads `body["embedding"]` and discards
`inputTextTokenCount`. The `Embedder` ABC returns `list[list[float]]`, with no
place for usage to ride along.

Changing the ABC is a breaking change to a public interface for the sake of a
secondary feature, so usage travels in a side channel instead: providers write
into a `contextvars.ContextVar` accumulator, and the wrapper resets it at call
start and reads it at call end.

`ContextVar`, not `threading.local`: verified that sync tool functions are
dispatched through `anyio.to_thread.run_sync`
(`utilities/func_metadata.py:108`), which runs them on a **reused worker
thread pool**. A thread-local would leak the previous call's token count into
the next call on the same worker thread. anyio copies the context into the
worker thread, so a `ContextVar` is correct on both the threaded and the
direct-await path. The accumulator carries `embed_tokens` and `embed_ms`.

`BedrockEmbedder.embed()` loops `invoke_model` **once per text**, so usage
must accumulate across the loop, not overwrite. Covered by a test.

Providers that cannot report real counts (fastembed, which is local and free)
fall back to `estimate_tokens()` from `token_budget.py` (4 chars/token) and set
`tokens_exact = 0`. The dashboard reports measured and estimated separately
rather than summing them and implying equal precision.

## 6. Cost

Prices are stored **per row at the time of the call** (`unit_price_per_1m`),
so historical rows stay truthful after the price table is updated.
`est_cost_usd` is nullable: NULL means "price unknown", which is a different
fact from fastembed's genuine $0.00, and the dashboard shows
`unpriced calls: N` rather than folding unknowns into zero.

### 6.1 Where a price comes from

Resolution order, first hit wins, recorded per row in `price_source`:

| Order | Source | `price_source` |
| --- | --- | --- |
| 1 | `BLUEOCEAN_PRICE_<PROVIDER>_<MODEL>` (USD per 1M tokens) | `env` |
| 2 | the pricing file (`BLUEOCEAN_PRICING_FILE`), refreshed from OpenRouter on demand | `openrouter` |
| 3 | built-in table (6.2) | `builtin` |
| 4 | nothing matched: `est_cost_usd` stays NULL | `NULL` |

`price_source` costs one small column and is decisive the day someone
questions a cost number: it separates "the user overrode this", "the feed is
wrong", and "our table is stale" without guesswork.

### 6.2 Built-in table

Fetched from primary sources on 2026-09-02:

| Provider | Model | USD per 1M input tokens | Source |
| --- | --- | --- | --- |
| openai | text-embedding-3-small | 0.02 | developers.openai.com/api/docs/pricing |
| openai | text-embedding-3-large | 0.13 | developers.openai.com/api/docs/pricing |
| openai | text-embedding-ada-002 | 0.10 | developers.openai.com/api/docs/pricing |
| bedrock | amazon.titan-embed-text-v2:0 | 0.02 | AWS Price List API, `titanModel=TitanEmbeddingsV2-Text-input`, `regionCode=us-east-1` ($0.00002 per 1K tokens on demand) |
| fastembed | any | 0.00 | local, no API call |

Bedrock is priced per region, so the table key includes the region and an
unlisted region yields NULL rather than a guess. Bedrock batch pricing
($0.01 per 1M) does not apply, since `BedrockEmbedder` calls `invoke_model`
synchronously.

### 6.3 OpenRouter as a price feed

`GET https://openrouter.ai/api/v1/embeddings/models` returns 33 embedding
models with `pricing.prompt` in USD **per token**, and needs no API key. This
is a different endpoint from `/api/v1/models`, which lists 421 chat models and
contains no embedding models at all.

The feed independently confirms the built-in OpenAI numbers, which is the
first reason to trust it:

| OpenRouter id | USD/token | per 1M | matches |
| --- | --- | --- | --- |
| `openai/text-embedding-3-small` | 0.00000002 | $0.02 | yes |
| `openai/text-embedding-3-large` | 0.00000013 | $0.13 | yes |
| `openai/text-embedding-ada-002` | 0.0000001 | $0.10 | yes |

**Refresh is explicit, never automatic.** `blueocean-admin usage
--refresh-prices` fetches the feed, and the pricing file is updated from it
(each entry keeping the model id, USD per 1M, and the fetch date; the writer is
named below). The server never calls out on its own. An automatic daily refresh would mean the server makes an
outbound request the user never asked for, which contradicts "data never
leaves the machine" and quietly adds a network dependency to a dashboard that
is meant to work air-gapped.

**The server owns the pricing file, the CLI never writes it remotely.**
`BLUEOCEAN_PRICING_FILE` sits next to the telemetry database
(`/data/pricing.json` under compose), and the same rule as section 8 applies:
the process that owns the directory is the one that writes it. So
`--refresh-prices` fetches from OpenRouter on the host and then `POST`s the
result to `/api/prices` (authenticated with the same token); the server writes
the file, atomically, write-temp then `rename`, so a reader never sees a
half-written file.

This keeps every earlier rule intact: the CLI still makes the only outbound
call, the server still never calls out on its own, and the host never has to
know what path the file has inside the container. Defaulting the CLI to
`~/.blueocean/pricing.json` would have reintroduced the split-file bug exactly
as section 8 describes it, since the container's home is `/home/appuser`.

Under `--db` (stdio-only, no server) the CLI writes the file directly, which is
correct there because nothing else is running.

Two limits worth stating plainly:

- **The feed does not cover Bedrock.** Titan v2 is not among the 33 models, so
  OpenRouter cannot replace the built-in table, only supplement it.
- **`:free` models retain data.** Entries such as
  `liquid/lfm-2.5-embedding-350m:free` state that requests and embeddings may
  be retained and used for training. If a price display ever lists these, it
  must carry that label rather than showing an attractive "$0". This project's
  entire premise is that memory content stays local.

Noted for a separate design, not built here: `intfloat/multilingual-e5-large`,
this project's own default model, is on OpenRouter at $0.01 per 1M. Using it
hosted would remove the 2 GB local model download while keeping vectors
compatible. See section 13.

## 7. The three readers

### 7.1 `memory_usage` MCP tool

Budget about 500 tokens, roughly 25-30 lines, default window 7 days, and
relative ("last 7 days") so timezone never enters an agent-facing answer.

Default view: totals, top 5 tools, top 3 agents, zero-result search rate,
cost, and count of entries never retrieved.

Optional `view` parameter for one drill-down at a time (`unused`, `tools`,
`errors`), each with its own budget. Without that escape hatch an agent that
wants the detail will fall back to guessing with `memory_search`, which costs
far more. Truncating the dashboard payload to fit a budget is not an option:
it produces structurally broken JSON.

### 7.2 `blueocean-admin usage`

```
blueocean-admin usage [--project X] [--days 7] [--by tool|agent|project|day]
                      [--audit] [--unused] [--json]
blueocean-admin usage --refresh-prices
```

`--refresh-prices` is the only command that sends anything off this machine.
It fetches the OpenRouter embedding price feed and hands the result to the
server via `POST /api/prices`, which writes the pricing file (section 6.3).
Everything else the CLI does over the network goes to localhost.

`project` is an optional flag, not a positional, unlike `stats`/`prune`: the
common question is "what did this machine use", not "what did one project
use". Default output is a human-readable table (the rest of the CLI prints raw
JSON payloads because those *are* the payload); `--json` for piping.

**The CLI does not open the SQLite file** in the normal case. It reads through
`GET /api/stats` (section 8), finding the server via `BLUEOCEAN_SERVER_URL`
(default `http://localhost:8765`) and authenticating with the existing
`BLUEOCEAN_AUTH_TOKEN`. If the server is unreachable, the command fails with a
clear error.

`--db <path>` opts explicitly into reading the file directly. That is the
stdio-only development case, where no HTTP server exists at all and the file is
on the same machine with no VM boundary in between. There is deliberately **no
automatic fallback** from HTTP to file: a transient connection failure while the
container is running would silently open a bind-mounted SQLite file underneath
an active writer, which is exactly the failure section 8 exists to prevent.

### 7.3 Dashboard

`GET /dashboard?token=...`, reusing the existing `?token=` mechanism that
`auth.py` already supports. Not auth-exempt: `--host 0.0.0.0` is really used by
docker-compose, so the dashboard is reachable off-host.

Single HTML file, vanilla JS, inline SVG charts, no build step and no CDN, so
it works air-gapped.

One page with a time-range selector (24h / 7d / 30d), in this order:

1. Four tiles: calls, p95 latency, error rate, estimated cost
2. Per tool: calls, p50, p95, errors
3. Per agent, from `clientInfo`
4. Search quality: zero-result rate, mean top score, mean tokens returned
5. Per project
6. Unused memories from `entry_hits`, `hits = 0` first, labelled
   "since first observed" because it is not scoped to the selected window
   (section 4.2)
7. Audit log, last 50 destructive operations
8. Recent errors

Loads once with a manual refresh button. No polling: the data barely changes,
this is a look-at-it-occasionally tool, and auto-refresh would re-send an
authenticated request carrying a URL token every few seconds for the life of
an open tab.

Token hygiene: the page reads the token from the URL, immediately strips it
with `history.replaceState`, and holds it in memory for the `/api/stats`
fetch. Served with `Referrer-Policy: no-referrer`. This is the same concern
that put `access_log=False` in `__main__.py`: clients that cannot set an
`Authorization` header send the token in the query string, and it must not
propagate into logs or referrers.

### 7.4 `GET /api/stats`

One endpoint returning every panel in one JSON object; `?days=` and
`?tz_offset_minutes=`. The response is aggregates plus two small row lists
(50 audit rows, recent errors), so it is a few KB. One round trip, one auth
check, and the URL token is sent once rather than nine times. Splitting per
panel remains possible later without touching the dashboard, which reads from
a single object either way.

Route wiring follows `/health`: insert into `mcp_app.router.routes` at the
front, then `wrap_with_auth(...)` **without** adding these paths to
`exempt_paths`.

**When `BLUEOCEAN_TELEMETRY=0`**, `/dashboard`, `/api/stats` and `/api/audit`
return **503** with a one-line body saying telemetry is disabled and which
variable turns it on. Not 404: that makes a deliberate configuration look like
a broken deployment, and costs someone an hour. Not an empty dashboard either,
which is worse - it looks like a working system that nobody uses, and leads to
a confidently wrong conclusion.

**Timezone.** Events are stored as UTC epoch; day bucketing happens in SQL
using the caller's offset (`strftime('%Y-%m-%d', ts, 'unixepoch', '+7 hours')`
style). "Yesterday" is always a local-time concept; bucketing by UTC would
make every daily number silently wrong by seven hours for a UTC+7 user. The
CLI passes the machine's offset, the dashboard passes the browser's.

## 8. Storage and the single-writer rule

`BLUEOCEAN_TELEMETRY_DB` selects the file.

- In docker-compose: bind mount `./data:/data` (gitignored) with
  `BLUEOCEAN_TELEMETRY_DB=/data/telemetry.db`. The Dockerfile creates `/data`
  owned by `appuser` (uid 10001). The `blueocean-mcp` service currently has no
  `volumes:` section at all, so without this the DB dies on every rebuild.
- Outside a container (stdio, dev only): `~/.blueocean/telemetry.db`.

The pricing file (section 6.3) lives in the same directory, for the same
reason: `/data/pricing.json` under compose, `~/.blueocean/pricing.json`
otherwise.

**Amended decision (see decision log, Q2.4).** The round-2 answer had the host
CLI and the containerized server writing the same SQLite file across the
bind mount. That is unsafe. WAL coordinates writers through an mmap'd `-shm`
file whose shared memory is not coherent across the macOS host / Docker VM
boundary, and even rollback-journal mode depends on `fcntl` locks being
forwarded faithfully through virtiofs. `busy_timeout` does not help, because
the failure is not contention but two processes that cannot see each other's
locks.

Therefore: **the server process is the only writer and the only reader of the
file.**

- `blueocean-admin usage` reads over HTTP (`GET /api/stats`). This also
  eliminates the "two disjoint datasets" failure mode entirely.
- CLI destructive operations record their audit row by POSTing to an
  authenticated `/api/audit` (8.1). If the server is unreachable, the CLI does
  **not** write the file: it warns loudly, prints the audit row as JSON on
  stderr so the record is not lost, and exits non-zero on the audit step while
  leaving the destructive operation's own result untouched. A silent
  file-write fallback would violate the same rule as 6.2, and would write to
  the host's default path while the real database sits at the container's -
  an audit row nobody would ever read. Under `--db` the CLI writes directly,
  which is correct because nothing else is running.
- The bind mount stays, for durability across rebuilds and host-side backup.

WAL and `busy_timeout` are still enabled: they cover multiple stdio dev
processes sharing `~/.blueocean/telemetry.db`, and any `--db` invocation.

### 8.1 What `/api/audit` is allowed to assert

Every client shares one `BLUEOCEAN_AUTH_TOKEN`. There is no per-agent identity,
so anyone who can call an MCP tool can also POST an audit row, including a row
that blames someone else for a deletion. Pretending otherwise would be worse
than having no audit trail.

The server therefore stamps what it can verify - the timestamp, and
`origin = 'cli-reported'` - and refuses to let a posted row claim
`origin = 'observed'`. Rows the server generated from calls it actually handled
carry `origin = 'observed'`.

A posted `error_msg` is **discarded, not stored**. The sanitized messages in
section 9 are safe because this server produced them from exceptions it
observed itself; a posted `error_msg` is arbitrary caller text with no such
provenance, and the privacy hard rule says caller text never reaches the
database. The row is still accepted (202) with `error_class` intact: the
classification survives, only the unverifiable text goes. The v2 migration
nulls `error_msg` on existing `cli-reported` rows for the same reason; rows
with `origin = 'observed'` are untouched by that sweep.

`error_class` is kept only while it looks like one: a dotted identifier of at
most 64 characters. The argument above is that a classification is
server-checkable in a way free text is not, and that only holds while
something actually checks it - otherwise the field is a second door for
exactly the caller text `error_msg` was closed against. A value that fails
the check is dropped rather than truncated, because 64 characters of
someone's memory is still their memory.

The other posted columns are typed too: `tool`, `project`, `area`, `module`
and `agent_name` must be strings and are capped at 120 characters,
`deleted_count` and `ok` must be integers, and anything else is dropped
before the row is queued. That is a binding requirement as much as a privacy
one - a value SQLite cannot bind raises inside the writer thread, and the
self-disable rule in section 9 would then take telemetry down for the life of
the process over one malformed request.

Stated plainly in the docs and on the dashboard panel: **this audit trail is
built to explain accidents, not to withstand a liar.** It answers "which agent
pruned this project on 2026-08-17", which is the question that motivated it. It
does not answer "prove nobody forged this row", and it cannot until identity
stops being a single shared token.

## 9. Privacy and failure isolation

**Hard rule.** Telemetry never stores query text, memory content, summaries,
entry metadata, or bearer tokens. Enforced by a sentinel round-trip test:
unique strings are injected into every input, then every column of every table
is dumped and asserted not to contain them.

`error_msg` is truncated to 200 chars and comes from exception messages, but
it is **never the raw message**. Before recording, the wrapper drops the
message entirely if it contains any string the caller passed in, replacing it
with the fixed marker `<redacted: contained caller text>`. This describes
**server-observed rows only** (`origin = 'observed'`): the wrapper saw the
exception, so it can sanitize it. Rows posted to `/api/audit` never carry an
`error_msg` at all (8.1) - nobody on the server side saw that exception, so
nothing can vouch for its text.

The original argument for storing raw messages was that neither `raise` site in
`vector_store.py` embeds memory content: an invalid project name (line 54) and
content over the size cap (line 193). That holds for our own code and does not
hold for the libraries we call. A `qdrant-client` or embedding-provider
exception can quote the payload it choked on, and that payload is the user's
memory. The privacy sentinel test caught exactly this during implementation.

Two filters, in order.

**Origin.** The message is kept only when the exception was raised inside
`blueocean_mcp` itself, judged by the deepest frame of its traceback. Anything
raised by a library we call - `qdrant-client`, an embedding provider - stores
`<redacted: third-party exception>` and its class, nothing more. This is the
filter that matters: those libraries quote the payload they choked on, and
that payload is the user's memory. The cost is real and accepted: a
`ConnectionError` from Qdrant now reaches the dashboard as a class name
without its "Connection refused" text.

**Caller text.** For our own exceptions, the message is dropped anyway if it
contains any string the caller passed in, replaced by
`<redacted: contained caller text>`. `project`, `area` and `module` are
skipped (already their own columns, and not content), as are strings shorter
than 8 characters, which would match ordinary words in an ordinary message.

The origin filter alone would have been enough for the leak the sentinel
found; the caller-text filter stays because our own `raise` sites take
user-supplied arguments and could start echoing them at any time.

Failure isolation:

- `BLUEOCEAN_TELEMETRY=0` disables everything.
- A telemetry failure never breaks a memory operation: log once, self-disable.
- Bounded in-memory queue (about 10,000 events), dropping oldest, counting
  drops so the dashboard can show that it dropped.

Retention: 90 days, env-overridable, purged at process start and hourly. No
rollup tables.

## 10. Test plan

Written in this order; the first two are TDD gates before their code exists.

1. **ctx injection** - ctx is injected into the wrapped tool, and `ctx` does
   not appear in the JSON schema the agent sees. Guards the
   `__signature__`/`__annotations__` hazard (5.2).
2. **Privacy sentinel** - unique strings into every input, dump all columns,
   assert absence.
3. **Coverage** - 8 tools instrumented, `memory_usage` deliberately excluded.
4. **`clientInfo` capture** - agent name and version recorded.
5. **Concurrent writers** - multiple processes against one file, WAL and
   `busy_timeout` hold.
6. **Telemetry off** - `BLUEOCEAN_TELEMETRY=0` touches no DB file.
7. **Failure isolation** - a broken writer does not break `memory_store` or
   `memory_search`; drops are counted.
8. **Bedrock usage accumulation** - N texts produce N calls and a summed token
   count, not the last one.
9. **Dashboard auth** - `/dashboard`, `/api/stats`, `/api/audit` and
   `/api/prices` without a token return 401.
10. **Day bucketing** - a UTC+7 offset puts an event at 23:30 local into the
    right local day.
11. **Migration** - a v1 database opened by v2 code gains columns and keeps
    its rows; the v2 sweep nulls `error_msg` only where
    `origin = 'cli-reported'`, observed messages survive, and reopening an
    already-migrated database is harmless.
12. **Price resolution** - env beats `pricing.json` beats the built-in table;
    an unknown model yields NULL cost with `price_source` NULL, not 0; the
    resolved price and source are snapshotted on the row.
13. **Audit origin** - a row posted to `/api/audit` is stored with a
    server-stamped timestamp and `origin = 'cli-reported'`, cannot claim
    `origin = 'observed'`, and has its posted `error_msg` discarded while
    `error_class` survives.
14. **`entry_hits` lifecycle** - `memory_delete` removes the matching row; an
    orphaned `point_id` left by an out-of-band deletion is skipped at read time
    rather than crashing the panel.
15. **Disabled state** - with `BLUEOCEAN_TELEMETRY=0`, `/dashboard`,
    `/api/stats` and `/api/audit` return 503 with an explanatory body.
16. **No silent fallback** - with the server unreachable and no `--db`,
    `usage` fails loudly and opens no local file; a destructive op's audit row
    is printed on stderr instead of written.
17. **`--refresh-prices`** - without `--db` it posts to `/api/prices` and never
    opens a local file; with `--db` it writes the file directly.

## 11. Implementation order

1. `telemetry.py`: schema, migrations, bounded queue, writer thread, retention
   purge. Unit tests 5, 6, 11.
2. ctx-injection test (test 1), then `instrument()` in `register_tools`.
   Tests 3, 4, 7.
3. Provider accounting: `ContextVar` accumulator, openai and bedrock usage
   capture, fastembed estimate. Test 8. Price resolution chain (env ->
   `pricing.json` -> built-in -> NULL), `est_cost_usd`, `unit_price_per_1m`,
   `price_source`. Test 12.
4. `entry_hits`, its deletion hooks in `memory_delete`/`prune`/`restore` (test
   14), and search-quality extraction from
   `TokenAllocation.to_dict()` (`total_tokens`, `len(summary)`,
   `summary[0]["score"]`).
5. Privacy sentinel test (test 2) against everything written so far.
6. `memory_usage` tool.
7. `GET /api/stats` and `/api/audit`, including the disabled-state response.
   Tests 9, 10, 13, 15.
8. `blueocean-admin usage` as an HTTP client, plus `--db` and
   `--refresh-prices`. Tests 16, 17.
9. `/dashboard` single-file page.
10. docker-compose bind mount, Dockerfile `/data`, `.gitignore`, README.

## 12. Decision log

Round 1 (all accepted):

| # | Decision |
| --- | --- |
| Approach | B: event log as source of truth, fanned out to readers. Prometheus rejected. |
| Scope | Operational, Usage, Quality, Cost |
| Identity | MCP `clientInfo`, not per-agent tokens |
| Default | on by default, data never leaves the machine |
| Q1 | log MCP tool calls plus destructive CLI/MCP ops as an audit trail |
| Q2 | `entry_hits` table for "which memories are never used" |
| Q3 | exception class name plus message truncated to 200 chars |
| Q4 | `BLUEOCEAN_TELEMETRY_DB`, default `~/.blueocean/telemetry.db`, plus a compose bind mount |
| Q5 | 90-day retention, env-overridable, purge at start and hourly, no rollups |
| Q6 | `BLUEOCEAN_TELEMETRY=0`; silent self-disabling failure; bounded queue with drop counting |
| Q7 | `memory_usage` returns a tight summary, 7 days, about 500 tokens |
| Q8 | single-file HTML dashboard, vanilla JS, inline SVG, no build step, no CDN |
| Q9 | `GET /dashboard?token=...` reusing the existing `?token=` mechanism |

Round 2: 2.1 one scrolling page with a fixed panel order; 2.2 single `usage`
command with flags, `--project` optional; 2.3 record both transport session and
agent-declared label; 2.4 bind mount `./data:/data`; 2.5 telemetry module
first, with the ctx test before the wrapper.

Round 3: 3.1 one wide `events` table plus `entry_hits`, `total_ms` and
`embed_ms` split; 3.2 side-channel accounting with a `tokens_exact` flag;
3.3 built-in price table, nullable cost, per-row price snapshot; 3.4 two
counters (`hits`, `full_hits`); 3.5 `user_version` with additive migrations.

Round 4: 4.1 one `/api/stats` endpoint; 4.2 fixed default view plus `view`
drill-down; 4.3 UTC storage with local-offset bucketing; 4.4 dashboard, CLI
and `memory_usage` are not themselves recorded; 4.5 manual refresh, no polling.

Round 5 (added after the user asked whether OpenRouter prices could be
included): 5.1 OpenRouter is a price *source*, refreshed only by an explicit
`--refresh-prices`, never by the server on its own; 5.2 resolution order env ->
`pricing.json` -> built-in -> NULL, with a `price_source` column recorded per
row; 5.3 supporting OpenRouter as an actual embedding *provider* is out of
scope here and gets its own issue.

Round 6 (raised because the Q2.4 amendment moved the CLI onto HTTP after
grilling had ended, which grew new branches): 6.1 `/api/audit` rows are stamped
`cli-reported` and cannot claim `observed`, with the limits of a single shared
token stated openly; 6.2 CLI finds the server via `BLUEOCEAN_SERVER_URL` and
fails loudly, with `--db` as an explicit opt-in and no silent fallback;
6.3 `entry_hits` is never purged and the panel is labelled "since first
observed"; 6.4 delete `entry_hits` on the paths we control and tolerate orphans
at read time; 6.5 a disabled telemetry system answers 503, not 404 and not an
empty page.

Amendments made while writing this spec:

- **Q2.4 amended.** Bind mount retained, but the host CLI no longer opens the
  SQLite file. SQLite locking is not reliable across a Docker Desktop bind
  mount. The CLI reads via `GET /api/stats` and writes audit rows via
  `POST /api/audit`. Section 8.
- **Q3.2 refined.** The side channel is a `contextvars.ContextVar`, not
  `threading.local`, because sync tools run on a reused anyio worker thread
  pool. Section 5.3.
- **Offline audit fallback dropped.** An earlier draft let the CLI write the
  audit row straight to the file when the server was down. Removed: it
  contradicted 6.2's no-silent-fallback rule and would have written to the
  host's default path rather than the container's. The CLI now prints the row
  on stderr and exits non-zero on that step. Section 8.
- **`--refresh-prices` posts to the server.** Same reason: only the server
  writes files in its own directory. Section 6.3.
- **Posted `error_msg` discarded (closeout amendment).** The first
  implementation copied a caller-supplied `error_msg` into `cli-reported`
  rows, truncating but not redacting it - exactly the caller-text leak
  section 9 exists to prevent, since a CLI-caught exception can quote memory
  or query text and nobody server-side saw it to sanitize it. Rows posted to
  `/api/audit` now never store `error_msg` (still 202, `error_class`
  survives), and the v2 migration nulls the field on existing
  `cli-reported` rows. Server-observed messages keep their section 9
  sanitization path unchanged. Sections 8.1 and 9.

## 13. Open risks

1. **Linux bind-mount ownership.** On macOS, Docker Desktop maps bind-mount
   ownership to the host user, so uid 10001 is invisible. On Linux the files
   are owned by 10001 and the docs must say so.
2. **`mcp-session-id` is client-supplied.** Fine as a grouping key, never as
   an identity assertion.
3. **Price drift.** The built-in table will go stale. Mitigated by the
   per-row price snapshot and the env override, not eliminated.
4. **OpenRouter feed coverage.** It prices only models OpenRouter routes, so
   Bedrock (and any future provider it does not carry) still depends on the
   built-in table. The feed supplements, never replaces.
5. **`:free` OpenRouter models retain data.** Not a risk to telemetry itself,
   but if the project ever adds OpenRouter as an embedding provider, routing
   memory content through a `:free` model would send it somewhere it can be
   retained for training. Any such feature must exclude or loudly label them.
6. **Follow-up, not a risk.** `OpenAIEmbedder` already accepts `base_url` via
   `OPENAI_BASE_URL`, so OpenRouter would work today except `_KNOWN_DIMENSIONS`
   raises `ValueError` for any model id outside OpenAI's three, and no env var
   exists to pass a dimension. A separate issue covers `BLUEOCEAN_EMBED_DIM`
   plus hosted `intfloat/multilingual-e5-large` at $0.01 per 1M.
7. **The audit trail is not tamper-proof.** One shared token means any client
   can post a `cli-reported` row. Mitigated by the `origin` flag and by saying
   so out loud (8.1); genuinely fixed only by per-agent identity.
8. **stdio dev path stays split.** A developer running stdio locally writes to
   a different file than the server. Accepted: every registered agent connects
   over `http://localhost:8765/mcp`, so stdio is not the production path.
