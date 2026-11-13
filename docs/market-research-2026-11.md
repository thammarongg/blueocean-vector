# Market Research for BlueOcean Vector (2026-11)

Researched on 2026-11-13. This note checks the market claims behind the README's [How this compares](../README.md#how-this-compares) section, along with the protocol and dependency facts that positioning depends on. Sources are primary only: official spec and docs pages, the projects' own GitHub repos and READMEs, and `gh api repos/<owner>/<repo>` for stats. Facts are linked inline. Opinions are marked **Inference**. Each claim has a confidence note:

- **High:** read verbatim from the primary source.
- **Medium:** taken from a summarized read of the primary page, or only partly covered by it.
- **Low:** no primary source found.

## Verified external findings

### 1. MCP spec 2026-07-28 and the Python SDK

| Claim | Finding | Confidence |
|---|---|---|
| Is 2026-07-28 final or still an RC? | **Final.** The RC was "locked as of May 21, 2026", and the final spec was due July 28, 2026 ([RC post](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)). The release post says: "Today, we're officially pushing the release button on the next version of the MCP specification, 2026-07-28" ([release post](https://blog.modelcontextprotocol.io/posts/2026-07-28/)). | High |
| Are protocol sessions and the `Mcp-Session-Id` header removed? | **Yes.** Changelog major change 1: "Remove protocol-level sessions and the `Mcp-Session-Id` header from the Streamable HTTP transport. List endpoints … no longer vary per-connection" ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). The RC post adds: "any MCP request can land on any server instance" ([RC post](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)). | High |
| Is the `initialize` handshake removed? | **Yes.** Changelog major change 2: "remove the `initialize`/`notifications/initialized` handshake. Every request now carries its protocol version and client capabilities in `_meta`." Protocol-version mismatches return `UnsupportedProtocolVersionError` (SEP-2575) ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). | High |
| How is the server `instructions` field delivered? | **In the `server/discover` result.** The [Discovery page](https://modelcontextprotocol.io/specification/2026-07-28/server/discover) lists `instructions` as "Optional natural-language guidance for LLMs on how to use this server effectively". In [`schema.ts`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts), `instructions?: string` appears only on `DiscoverResult`. Servers **MUST** implement `server/discover`, but clients only **MAY** call it ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). So a 2026-07-28 client that never calls `server/discover` never sees the instructions. | High (spec); **Inference** for the consequence |
| How are the client's name and version conveyed? | **Per request, in `_meta`.** "Clients SHOULD identify themselves on each request (`io.modelcontextprotocol/clientInfo`), and servers SHOULD identify themselves in each result's `_meta` (`io.modelcontextprotocol/serverInfo`)" ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). This is a SHOULD, so client info is optional. | High |
| What does SEP-2567 say about server-issued handles? | Status **Final**. The SEP removes sessions and recommends "explicit, server-minted state handles that the model carries and threads through subsequent calls", for example `create_basket()` returning `basket_id`. The handle is explicitly "not a new protocol construct — there is no schema or wire format for them. They are a tool-design pattern". The non-normative guidance: handles should be opaque; for authenticated servers, validate `(handle, auth_context)` on every call; for unauthenticated servers, use 128 bits or more of CSPRNG entropy and a bounded lifetime; document durability in the `create_*` tool description; return useful expiry errors. The rollout "is a clean break … with no deprecation window" ([SEP-2567](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2567-sessionless-mcp.md), [PR #2567](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2567)). | High |
| Other changes that matter here | `subscriptions/listen` replaces the HTTP GET stream and `resources/subscribe`. SSE resumability (`Last-Event-ID`) is removed. Logging is deprecated, along with Roots and Sampling (SEP-2577). `Mcp-Method` and `Mcp-Name` headers are now required on POST. List results now require `ttlMs` and `cacheScope` ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). | High |
| Tasks | Tasks have moved out of core into the official extension `io.modelcontextprotocol/tasks` (SEP-2663). The extension uses polling via `tasks/get` and a new `tasks/update`, and drops `tasks/list` ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). | High |
| Server-initiated events (2026 roadmap) | **Not a priority in the March roadmap.** The [2026 MCP Roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/) (Mar 9, 2026) has four priority areas: transport evolution and scalability, agent communication (refining Tasks), governance, and enterprise readiness. "Triggers and event-driven updates" appear only under "On the Horizon", which "would benefit from a new Working Group". **The August roadmap promotes them.** [The New MCP Roadmap](https://blog.modelcontextprotocol.io/posts/mcp-roadmap/) (Aug 22, 2026) lists "server-initiated events (webhooks and channels, so clients aren't left polling for results)", together with "maturing the Tasks extension (SEP-2663) so it can move into the specification". | High (quotes taken from the raw page text) |
| Which Python SDK version implements 2026-07-28, and is it 2.x or 3.x? | **v2.0.0, a 2.x release.** It was published 2026-07-28: "It supports the 2026-07-28 revision … and serves every earlier revision from the same server." "v1.x is in maintenance mode" ([v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)). The latest release is v2.3.0, published 2026-10-02 ([releases](https://github.com/modelcontextprotocol/python-sdk/releases)). There is no 3.x. A known gap in v2.0.0: "The tasks extension (SEP-2663) is not part of this release." | High |
| What does the repo pin and resolve? | `pyproject.toml` pins `mcp>=2.0.0,<3.0.0`, and `uv.lock` resolves **mcp 2.0.0**. The pin is on the right major version, but the lock is three minor versions behind 2.3.0. | High |

**Repo observation (not a recommendation):** `src/blueocean_mcp/telemetry/instrument.py` (`_agent_identity`) reads the agent's identity from `ctx.session.client_params` and the session from the `mcp-session-id` request header, falling back to a per-process id. The installed SDK (`mcp/server/connection.py`, `Connection.from_envelope`) builds `client_params` from the per-request `_meta` envelope, but only when both the client info and the client capabilities parse. So agent name and version still work for 2026-07-28 clients that send both. However, 2026-07-28 clients send no `Mcp-Session-Id`. **Inference:** on the shared HTTP server, the telemetry `session_id` for every modern client collapses to the single server process id. `memory_summarize_session` is unaffected because it already takes `session_id` as an ordinary tool argument, which is exactly the handle pattern SEP-2567 recommends.

### 2. Memorix

The canonical upstream is [AVIDS2/memorix](https://github.com/AVIDS2/memorix). It is not a fork, was created 2026-02-14, and its latest release is v1.10.3, published 2026-11-05 ([releases](https://github.com/AVIDS2/memorix/releases)). All facts below come from its [README](https://github.com/AVIDS2/memorix/blob/main/README.md) and [docs/ARCHITECTURE.md](https://github.com/AVIDS2/memorix/blob/main/docs/ARCHITECTURE.md).

| Claim (README column) | Finding | Confidence |
|---|---|---|
| Retrieval: "Fallback only (~1.8s), keyword search is primary" | **Partly accurate, but too narrow.** The storage model is "SQLite is the canonical store. Small projects use the in-process Orama path; larger projects use a persistent SQLite FTS5 candidate index and, when available, an optional local LanceDB semantic shadow index." The 1.8-second fallback is real but limited to the `memorix context` / `resume` Workset path: "Keyword matches stay primary; when no reviewed durable item matches and an embedding provider is configured, Memorix makes one 1.8-second, no-retry semantic fallback." The `memorix_search` tool itself runs "BM25 or hybrid retrieval -> source-aware ranking" ([ARCHITECTURE.md](https://github.com/AVIDS2/memorix/blob/main/docs/ARCHITECTURE.md)). The accurate summary is keyword-primary with optional hybrid semantic search when an embedding provider is configured. | High |
| "MCP-native, no server to run" | **Contradicted.** Stdio is still the default, but `memorix background start` / `memorix serve-http` "run the HTTP service for a shared endpoint, dashboard, VPS Docker deployment, or multiple clients" ([README](https://github.com/AVIDS2/memorix/blob/main/README.md)). [docs/DOCKER.md](https://github.com/AVIDS2/memorix/blob/main/docs/DOCKER.md) documents a Docker `serve-http` service at `http://host:3211/mcp` with bearer-token auth (`MEMORIX_HTTP_AUTH_TOKEN`) and an unauthenticated `/health` endpoint. | High |
| "Explicit — the calling agent writes" | **Incomplete.** Memorix offers "Optional capture of prompts, tool events, file edits, session lifecycle events, and native compaction checkpoints" through hooks, enabled by default for Claude Code unless you pass `--noHooks`. It also has Git Memory (commit ingestion via a git hook) and optional LLM "formation" (fact extraction and evaluation). Hook-captured candidates "stay pending until qualified" for long-term memory ([README](https://github.com/AVIDS2/memorix/blob/main/README.md)). | High |
| Team sync backends | There are two separate features. **Multi-device store sync** is opt-in and, per the README, "separate from HTTP sharing". It moves "Privacy-filtered observation events with local SQLite kept canonical" over a "GitHub JSONL, filesystem, S3, or Postgres relay" (`memorix sync store`). **Team coordination** (`memorix team`, `memorix orchestrate`, `memorix lock`) covers task handoffs, file locks, messages, and review loops ([README](https://github.com/AVIDS2/memorix/blob/main/README.md)). | High |
| Client list | The repo description lists Claude Code, Codex, Cursor, Windsurf, Gemini CLI, Antigravity, OpenClaw, Hermes Agent, Oh-my-Pi, Pi, Copilot, Kiro, OpenCode, and Trae. The README adds DeepSeek Harness, CodeBuddy Code, WorkBuddy, Grok Build, memcode (its bundled terminal agent), and "Any MCP Client" (stdio or HTTP). That is about 20 named targets, most with plugin, MCP, hooks, and skills integrations. Hermes is supported through a plugin with hooks. | High |
| Tool count | It depends on the profile: "`memorix serve` defaults to `--mode micro` (9 tools) … `memorix setup` writes `--mode lite` (21 tools) … `--mode team` (29 tools) … `--mode full` (48 tools)". | High |
| License | Apache-2.0 ([gh api](https://github.com/AVIDS2/memorix)). | High |

### 3. GitHub stats (gh api, 2026-11-13)

| Repo | Stars | Last push (UTC) | License | Notes |
|---|---:|---|---|---|
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 116,759 | 2026-11-13 | Apache-2.0 | Description: "Works with Claude Code, OpenClaw, Codex, Gemini, Hermes, Copilot, OpenCode + More" |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | 68,913 | 2026-11-06 | Apache-2.0 | |
| [getzep/graphiti](https://github.com/getzep/graphiti) | 32,789 | 2026-11-11 | Apache-2.0 | |
| [letta-ai/letta](https://github.com/letta-ai/letta) | 25,429 | 2026-10-18 | Apache-2.0 | |
| [basicmachines-co/basic-memory](https://github.com/basicmachines-co/basic-memory) | 4,566 | 2026-11-13 | AGPL-3.0 | |
| [AVIDS2/memorix](https://github.com/AVIDS2/memorix) | 931 | 2026-11-11 | Apache-2.0 | |
| [thammarongg/blueocean-vector](https://github.com/thammarongg/blueocean-vector) | 0 | 2026-11-02 | MIT | `origin/main` matches local `main` (`22f384a`) |

Confidence: High, read straight from `gh api repos/<owner>/<repo>`. Stars are an attention signal, not a quality metric.

**claude-mem retrieval and capture** ([README](https://github.com/thedotmack/claude-mem/blob/main/README.md)):

- **Retrieval is hybrid FTS5 + vector.** "SQLite at `~/.claude-mem/claude-mem.db` holds sessions, observations and summaries. Chroma adds semantic search on top of SQLite's keyword search." The docs link reads "Database - SQLite schema & FTS5 search" and "Search Architecture - Hybrid search with Chroma vector database". Search uses a three-step MCP workflow (`search` index → timeline → details). High.
- **Capture is automatic, through hooks and an LLM observer.** "Hooks in your agent hand each tool call … to a small local worker." For Claude Code the hooks are SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStop, and SessionEnd. "The observer … turns tool activity into observations and summaries." Explicit writes are also possible (`save_memory`). High.
- **Sharing.** Memory is local by default ("Your memory lives in `~/.claude-mem` on your machine. Cloud sync is opt-in"). There is an optional hosted team workspace ("TeamBrain"), and anonymous usage analytics are on by default. High.

**Inference:** claude-mem has about 125x Memorix's stars. Its own description now claims "Every Agent" and lists Codex and Hermes, so the "cross-agent coding memory" niche is contested by the field's largest project, not only by Memorix. The README's comparison table does not list claude-mem at all.

### 4. Built-in memory in the agents themselves

| Agent | Finding | Machine-local / per-tool? | Confidence |
|---|---|---|---|
| Claude Code (auto memory) | "Auto memory is on by default in local sessions." Its scope is "Per repository, shared across worktrees", stored at `~/.claude/projects/<project>/memory/` with `MEMORY.md` as the index. "The first 200 lines of `MEMORY.md`, or the first 25KB, whichever comes first, are loaded at the start of every conversation." "Auto memory is machine-local. … Files are not shared across machines or cloud environments." The location can be changed with `autoMemoryDirectory` ([Claude Code memory docs](https://code.claude.com/docs/en/memory)). | **Machine-local: yes**, stated explicitly. **Claude-only: Inference.** The page never mentions other tools reading the store. | High (machine-local); Medium (cross-tool) |
| Codex (memories) | The given URL `developers.openai.com/codex/memories` now 308-redirects to [learn.chatgpt.com/docs/customization/memories](https://learn.chatgpt.com/docs/customization/memories?surface=app). "Local Codex memories are off by default." Turned on with `[features] memories = true`, they are stored under `$CODEX_HOME/memories_v2/` (default `~/.codex`). "After you enable memories, Codex can turn useful context from eligible prior chats into local memory files." The toggle applies "for the selected machine". "ChatGPT web uses ChatGPT memory, while local Codex clients use a separate local memory store." The page also advises: "Keep required team guidance in AGENTS.md or checked-in documentation. Treat memories as a helpful recall layer, not as the only source for rules that must always apply." | **Machine-local: yes**, stated. **Not shared with other tools: Inference.** The page never mentions other agents, and the store is a Codex-home directory. | High (quotes); Medium (cross-tool) |
| Cursor (Memories) | **Removal in 2.1 is not confirmed by primary sources.** The official [2.1 changelog](https://cursor.com/changelog/2-1) (Nov 21, 2025) says "Custom modes have been removed, with the ability to export as custom commands" and never mentions Memories (the 2.1 and 2.2 changelog pages both loaded, and a grep of each for "emor" finds nothing). What is verifiable: the old docs URL `cursor.com/docs/context/memories` now redirects to [Rules](https://cursor.com/docs/rules), the [docs index](https://cursor.com/llms.txt) has no Memories page, and Rules (including dashboard-managed Team Rules) is the documented persistent-context mechanism. Community forum threads report Memories disappearing in 2.1.x, but I could not confirm any of those replies came from Cursor staff, so they are not treated as fact here. | Cursor's documented persistence today is Rules, which are project files or team-dashboard rules. Nothing in the official docs describes a store shared with other agents. | Medium (redirect and index verified); Low (the "removed in 2.1" claim itself) |

**Inference:** all three built-in memories are per-tool. Claude Code and Codex document theirs as machine-local. None of the three describes sharing with a different agent. That still supports BlueOcean's "one store across tools and machines" pitch. But the real cross-tool competitors are third-party layers (claude-mem, Memorix, mem0's plugins; see item 6), not the built-ins. Both vendors point to checked-in `AGENTS.md` as the channel for guidance shared across tools. Claude Code can read a repo's `AGENTS.md` in place of CLAUDE.md ([memory docs](https://code.claude.com/docs/en/memory)), and Codex says to "Keep required team guidance in AGENTS.md or checked-in documentation". That is static, hand-maintained text, not a searchable memory.

### 5. Qdrant hybrid search

| Claim | Finding | Confidence |
|---|---|---|
| Server-side fusion via `query_points` + `prefetch` | **Yes, as of v1.10.0.** The [Hybrid Queries page](https://qdrant.tech/documentation/search/hybrid-queries/) is marked "Available as of v1.10.0": "Its main building block is the `prefetch` parameter". Fusion methods are `rrf` and `dbsf` (DBSF since v1.11.0). A configurable RRF `k` arrived in v1.16.0 and weighted RRF in v1.17.0. | High |
| Sparse vectors | **Yes.** Sparse vectors are documented in [Vectors](https://qdrant.tech/documentation/manage-data/vectors/), and the hybrid examples fuse sparse and dense prefetches. The current page carries no version marker on the sparse section. | High (support); version not stated on the page |
| Built-in BM25: server-side sparse model | **Since v1.15.2.** The release notes say: "Implement BM25 inference in Qdrant locally" ([v1.15.2](https://github.com/qdrant/qdrant/releases/tag/v1.15.2)). You pass `{"text": ..., "model": "qdrant/bm25"}` in place of a vector, and Qdrant generates the sparse vector on the server ([BM25 inference](https://qdrant.tech/documentation/inference/inference-bm25/)). | High |
| Built-in BM25: scoring on a full-text index (now the recommended path) | **Since v1.20.0 only.** The [v1.20.0 release notes](https://github.com/qdrant/qdrant/releases/tag/v1.20.0) (2026-10-22) list "BM25 scoring directly on the full-text payload index", and [Indexing → BM25 Scoring](https://qdrant.tech/documentation/manage-data/indexing/) is marked "Available as of v1.20.0". The BM25 inference page itself now says: "We recommend running BM25 on a full-text index rather than with the BM25 model". A text query can be used as a `prefetch` and fused with a dense query by RRF ([Hybrid Search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)). qdrant-client 1.20.0 adds `TextQuery` support for it ([client v1.20.0](https://github.com/qdrant/qdrant-client/releases/tag/v1.20.0)). | High |
| Repo pin | `pyproject.toml` pins `qdrant-client>=1.19.0,<1.20` (lock: 1.19.0), and `docker-compose.yml` runs `qdrant/qdrant:v1.19.0`. On this pin you can use dense + sparse prefetch with RRF and the server-side `qdrant/bm25` sparse model. You **cannot** use full-text-index BM25 scoring without moving server and client to 1.20.x. The latest server is v1.20.1 (2026-11-13). | High |

### 6. mem0 OpenMemory

| Claim | Finding | Confidence |
|---|---|---|
| Was OpenMemory sunset? | **Yes, and the code has been removed from the mem0 monorepo.** Before removal, the OpenMemory README read: "**Sunsetting Notice:** OpenMemory is being sunset. For local self-hosted memory with a dashboard, please use the Mem0 self-hosted server instead" (`openmemory/README.md` at the parent of commit `ea2ee075`). [PR #6530](https://github.com/mem0ai/mem0/pull/6530), merged 2026-07-29, "Removes the `openmemory/` directory (195 files) … OpenMemory has been sunset". It names `server/` (FastAPI + PostgreSQL/pgvector + Neo4j) as "the supported path" and preserves a read-only snapshot under `openmemory-archive/` in mem0ai/openmemory#5. | High |
| What is at `mem0ai/openmemory` now? | **A different product with the same name.** [mem0ai/openmemory](https://github.com/mem0ai/openmemory) (MIT, 107 stars) is an "Open-source CLI & TUI to port AI coding sessions across Claude Code, Codex, and OpenCode", currently in beta, with "Autosync … (coming soon)". Do not confuse it with the sunset memory MCP server. | High |
| mem0's current direction for coding agents | The mem0 repo now ships agent plugins (`integrations/claude-code-plugin`, `codex-plugin`, `cursor-plugin`, `hermes-plugin-mem0`, `opencode-plugin`, and others; [marketplace.json](https://github.com/mem0ai/mem0/blob/main/marketplace.json)). The Claude Code plugin: "hooks capture session details locally, a background worker turns them into Mem0 memories". It requires a Mem0 Platform API key ([plugin README](https://github.com/mem0ai/mem0/blob/main/integrations/claude-code-plugin/README.md)). mem0's core retrieval is now "Multi-signal retrieval -- semantic, BM25 keyword, and entity matching scored in parallel and fused" ([README](https://github.com/mem0ai/mem0/blob/main/README.md)). | High |

**Inference:** the README row for mem0 ("SDK / hosted API") understates mem0's position today. mem0 now ships hook-based coding-agent plugins for many of the same clients BlueOcean targets, though they are tied to its hosted platform.

### 7. Rows checked for the README refresh (same day)

These rows were not part of items 1-6 and were checked afterwards, from each project's own README (via `gh api repos/<owner>/<repo>/readme`), before the README table was rewritten.

| Project | Finding | Confidence |
|---|---|---|
| [threadctx-dev/threadctx-mcp](https://github.com/threadctx-dev/threadctx-mcp) (1 star, last push 2026-07-29) | "Shared memory MCP server for AI coding agents (Claude Code, Cursor). Free & local by default." Local matching "is keyword-based"; "Cloud (paid Team tier+): memory shared across everyone on the repo, with real semantic search". It has a "Passive capture — turn git history into memory" section (`threadctx capture` reads commits since the last run). | High |
| [getzep/graphiti](https://github.com/getzep/graphiti) | "Hybrid Retrieval: Combines semantic embeddings, keyword (BM25), and graph traversal". The repo ships an `mcp_server/` directory. | High |
| [letta-ai/letta](https://github.com/letta-ai/letta) | "The current source code lives in [`letta-ai/letta-code`](https://github.com/letta-ai/letta-code), which includes the agent harness, interactive terminal UI, App Server, channels, and the runtime". The `archive` branch holds "the retired Letta V1 API server". Letta's current retrieval behaviour was not checked, which is why the README no longer lists it as a row. | High (relocation); not checked (retrieval) |

## Local evidence (this repo, 2026-11-13)

These come from this server's own telemetry and a few direct tests, not from the market.

- **Usage grew roughly 10x.** `memory_usage(days=90)` counts 788 calls: 323 `memory_search`, 291 `memory_store`, 92 `memory_manifest`. In September the count was 74. By agent, **`hermes-agent` made 213 calls**, more than any other. Its provider recalls automatically on every turn. An agent that reports its name only as `mcp` made 145 calls, and `codex-mcp-client` made 66. Errors were 4, the zero-result rate was 6.5%, and p95 latency was 984 ms. 161 entries have never been returned by any search.
- **Searches for exact identifiers are unreliable.** Four searches for strings that appear verbatim in stored entries gave one clean hit, one near-miss and two misses. `BLUEOCEAN_HEALTH_EMBED_TTL` ranked 2nd. The commit SHA `4731aff` did not reach the top 3. `issue #6` did not reach the top 3. `sync_turns_platforms` returned related entries but not the literal match. The search is dense-only (`vector_store.py`, `search()`), with no keyword path and no score floor.
- **Issue #8 is decided but not built.** The [mechanism and its ceiling](https://github.com/thammarongg/blueocean-vector/issues/8) makes the server-composed **Session Trace** the guaranteed floor. Its measured prerequisite is the `mcp-session-id` defect: 35 of 80 events fell back to a per-process id. Under 2026-07-28 that defect becomes total (item 1 above).

## Recommendations

Ranked by evidence and urgency. Each one lists its market evidence, its local evidence, any conflict with a recorded decision, and rough effort (S = days, M = 1-2 weeks, L = several weeks).

### 0. Correct the README comparison first (S)

Four rows are now wrong or missing. Memorix has an HTTP server, Docker and bearer auth, hooks with auto-capture, and BM25 or hybrid search. mem0 ships hook plugins for Claude Code, Codex, Cursor and Hermes. claude-mem (116k stars, hybrid search, multi-agent) is absent. A comparison that a reader can disprove in five minutes costs more credibility than it earns.

What still differentiates BlueOcean, checkable today:
- **Fully local and free, no account.** mem0's plugins require a Mem0 Platform key, and claude-mem turns anonymous analytics on by default.
- **MIT licence.** basic-memory is AGPL-3.0.
- **Multilingual dense embedding by default.** Thai and English are verified.
- **One store across tools *and* machines.** Claude Code and Codex built-ins are documented as machine-local, and claude-mem is local by default. Memorix now competes here too, so this edge is narrower than it was.

### 1. Make the server 2026-07-28-safe (M), P0

- **Market / spec:** sessions, `Mcp-Session-Id` and `initialize` are removed with "no deprecation window". `instructions` now arrive only via `server/discover`, which clients *MAY* call. SEP-2567 says to carry state in server-minted handles passed as tool arguments.
- **Local:** `_agent_identity` keys sessions on `mcp-session-id`, so every modern HTTP client collapses into one process id. That breaks telemetry's session counts *and* the attribution the Session Trace needs. The 145 calls under the generic agent name `mcp` suggest identity is already leaking.
- **Do:**
  1. Bump `mcp` 2.0.0 to 2.3.0.
  2. Have `memory_manifest` (the documented first call) mint an opaque session handle that later calls accept. Fall back to (clientInfo, auth token, idle window).
  3. Stop treating server `instructions` as the delivery channel. The skill and plugin files carry the workflow now.
- **Decision conflict:** none. This is a prerequisite of issue #8.

### 2. Build the Session Trace from issue #8 (M), P1

- **Market:** every leading competitor records sessions without asking the agent. claude-mem and Memorix use hooks, mem0 uses hooks plus a worker.
- **Local:** 20 of 21 measured sessions ended without a record. The design is decided, the `kind` filtering it needs is shipped, and only the build is missing.
- **Decision conflict:** none. It is the recorded floor.

### 3. Hybrid search: dense + BM25 with RRF (M-L), P1

- **Market:** claude-mem (FTS5 + Chroma), Memorix (BM25 or hybrid) and mem0 (semantic + BM25 + entity) all fuse keyword and semantic search. BlueOcean is now the only one in the comparison that is dense-only.
- **Local:** the identifier misses above. Coding memory is full of SHAs, issue numbers, env vars and function names, which are exactly the strings dense embeddings blur.
- **Two paths:**
  - **(a)** Move to Qdrant 1.20.x server and client, then use full-text-index BM25 as a `prefetch` fused with dense by RRF. Qdrant now recommends this path, and it needs no sparse vectors stored.
  - **(b)** Stay on 1.19 and add a `qdrant/bm25` sparse vector.

  Both change the collection schema, so ship a migrate command and take a snapshot first. The README gotcha about the client and server being pinned in lockstep applies.
- **Open risk (unverified):** Thai is written without spaces. Confirm that the full-text tokenizer segments Thai before you promise multilingual keyword search. Otherwise keep dense as the Thai path and say so.
- Add a relevance floor so weak matches are dropped instead of filling `top_k`.

### 4. Write hygiene: dedup, update/supersede, provenance (S-M), P1

- **Market:** mcp-memory-service dedups by content hash, Memorix keeps hook captures "pending until qualified", and Zep/Graphiti model facts that change over time.
- **Local:** 161 entries never retrieved in 90 days, and the recorded memory-loss incident where concurrent sessions overwrote entries. There is no `update`, only `store` and `delete`.
- **Do:**
  - Check `memory_store` for near-duplicates within the same area and module, and return the existing id.
  - Add `memory_update` or a `supersedes` field, so stale facts stay visible as replaced and are not deleted.
  - Stamp the git branch and HEAD SHA on write, so "when memory and git disagree, git wins" can be checked mechanically.
- **Decision conflict:** none.

### 5. Reopen mechanical auto-capture, without LLM extraction (M), P2, needs your decision

- **Market:** the field's largest project (claude-mem, 116k stars) and mem0's new plugins are both hook-first. Memorix turns hooks on by default for Claude Code.
- **Local:** Hermes, the only client with automatic recall, is the top caller at 213 of 788 calls. Automatic beat voluntary here as well.
- **Conflicts with:**
  - the "no automatic extraction" stance in the README;
  - the deferred Claude-Code-only Stop hook (2026-08-18). Its premise, "no cross-tool primitive", was already partly overturned by issue #2's findings on per-client lifecycle hooks.
- **Proposal:** per-client hook packs (Claude Code, Codex, OpenCode, Hermes) that write *mechanical* traces (`kind` kept out of default search) plus automatic recall at session start. Keep LLM summarisation out, which preserves the product stance.

### 6. Human-facing review and Markdown export (M), P2

- **Market:** Basic Memory's pitch is notes in Markdown that both you and the AI edit. Codex's docs tell teams to keep required guidance in checked-in `AGENTS.md`.
- **Do:**
  - A dashboard page to browse, edit and prune entries, which makes the 161 unused entries actionable.
  - `blueocean-admin export --markdown` and `import`, so memory can be reviewed in a PR and promoted into `AGENTS.md`.

### 7. Team and multi-user (L), P3, later

- **Market:** Memorix has team mode, locks and multi-device sync. claude-mem offers TeamBrain as an opt-in hosted service.
- **Do:** per-token identity, read-only tokens and per-project scopes on the shared server. Do this only after items 1-4, because it multiplies the impact of every data-hygiene gap.

### Getting found (separate from features)

The repo has 0 stars against a field of 1k to 117k. The deciding factor is distribution, not missing features:
- List it in the official MCP registry and on PyPI.
- Offer a one-command install per client.
- Publish a small, reproducible **coding-memory** eval: identifier lookups, "why did we choose X", and cross-tool handoff. The vendors' LoCoMo and LongMemEval claims are self-reported, mutually inconsistent, and measure conversational memory, not coding memory.
- The free, local, Thai-capable angle is a niche nobody else claims.

### Deliberately not recommended

- Graph memory and LLM fact extraction. These are mem0, Zep and Letta territory, and they contradict the product stance.
- Chasing LoCoMo scores.
- Growing to Memorix's 48 tools. More tools cost every client context tokens and dilute the workflow.

## Sources

- MCP spec 2026-07-28 changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog
- MCP spec 2026-07-28 overview: https://modelcontextprotocol.io/specification/2026-07-28
- MCP `server/discover`: https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- MCP schema 2026-07-28: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts
- SEP-2567 (Final): https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2567-sessionless-mcp.md and https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2567
- MCP blog, RC post: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- MCP blog, release post: https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP blog, 2026 roadmap (Mar 9, 2026): https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/
- MCP blog, The New MCP Roadmap (Aug 22, 2026): https://blog.modelcontextprotocol.io/posts/mcp-roadmap/
- MCP Python SDK releases: https://github.com/modelcontextprotocol/python-sdk/releases (v2.0.0: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0, v2.3.0: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
- Memorix README: https://github.com/AVIDS2/memorix/blob/main/README.md
- Memorix architecture: https://github.com/AVIDS2/memorix/blob/main/docs/ARCHITECTURE.md
- Memorix Docker: https://github.com/AVIDS2/memorix/blob/main/docs/DOCKER.md
- claude-mem README: https://github.com/thedotmack/claude-mem/blob/main/README.md
- threadctx-mcp README: https://github.com/threadctx-dev/threadctx-mcp
- Graphiti README: https://github.com/getzep/graphiti/blob/main/README.md
- Letta README and its current source: https://github.com/letta-ai/letta, https://github.com/letta-ai/letta-code
- GitHub repos (stats via `gh api repos/...`): https://github.com/thedotmack/claude-mem, https://github.com/mem0ai/mem0, https://github.com/AVIDS2/memorix, https://github.com/basicmachines-co/basic-memory, https://github.com/getzep/graphiti, https://github.com/letta-ai/letta, https://github.com/thammarongg/blueocean-vector
- Claude Code memory docs: https://code.claude.com/docs/en/memory
- Codex memories (redirect target of developers.openai.com/codex/memories): https://learn.chatgpt.com/docs/customization/memories?surface=app
- Cursor 2.1 changelog: https://cursor.com/changelog/2-1
- Cursor Rules docs: https://cursor.com/docs/rules
- Cursor docs index: https://cursor.com/llms.txt
- Qdrant Hybrid Queries: https://qdrant.tech/documentation/search/hybrid-queries/
- Qdrant Hybrid Search (dense + BM25): https://qdrant.tech/documentation/search/text-search/hybrid-search/
- Qdrant Full-Text Search (BM25): https://qdrant.tech/documentation/search/text-search/full-text-search/
- Qdrant Indexing (BM25 scoring, v1.20.0): https://qdrant.tech/documentation/manage-data/indexing/
- Qdrant BM25 inference: https://qdrant.tech/documentation/inference/inference-bm25/
- Qdrant Vectors (sparse): https://qdrant.tech/documentation/manage-data/vectors/
- Qdrant release v1.15.2: https://github.com/qdrant/qdrant/releases/tag/v1.15.2
- Qdrant release v1.20.0: https://github.com/qdrant/qdrant/releases/tag/v1.20.0
- qdrant-client release v1.20.0: https://github.com/qdrant/qdrant-client/releases/tag/v1.20.0
- mem0 README: https://github.com/mem0ai/mem0/blob/main/README.md
- mem0 PR #6530 (remove OpenMemory): https://github.com/mem0ai/mem0/pull/6530
- mem0 Claude Code plugin: https://github.com/mem0ai/mem0/blob/main/integrations/claude-code-plugin/README.md
- mem0ai/openmemory (new product): https://github.com/mem0ai/openmemory
