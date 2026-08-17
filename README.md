# BlueOcean Vector

*Shared, persistent memory for coding agents.*

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED.svg?logo=docker&logoColor=white)
![Status](https://img.shields.io/badge/status-alpha-orange.svg)

The kind of memory that survives switching from Claude Code to Codex to Cursor mid-project — and survives you running out of tokens in one of them.

If you've ever burned through a context window, opened a different tool, and then spent ten minutes re-explaining what you were doing, this is for that problem. BlueOcean Vector runs one small server on your machine. Any MCP-capable agent can read from it and write to it. Whichever tool you open next just asks "what do we know about this project?" and picks up where the last one left off.

> [!TIP]
> Store a decision in Claude Code → open Codex tomorrow → it already knows *why* you chose Postgres over DynamoDB, not just that you did.

---

## Contents

- [Why it exists](#why-it-exists)
- [How this compares](#how-this-compares)
- [How it fits together](#how-it-fits-together)
- [Getting started](#getting-started)
  - [Teaching agents to actually use it](#teaching-agents-to-actually-use-it)
  - [Alternative: stdio](#alternative-stdio-per-agent-local-process)
- [The tools an agent gets](#the-tools-an-agent-gets)
- [Configuration](#configuration)
- [Admin CLI](#admin-cli)
- [Running the tests](#running-the-tests)
- [Security](#security)
- [Deploying beyond localhost](#deploying-beyond-localhost)
- [Gotchas](#a-few-gotchas-worth-knowing-before-you-touch-this)
- [License](#license)

---

## Why it exists

Every agent session starts from zero. You explain the project, the constraints, the "we tried that already, it didn't work" — and then the session ends and it's gone. Multiply that by every tool you use, and you're spending real tokens just re-establishing context that already existed an hour ago.

BlueOcean Vector is a small, boring fix: one shared memory store, one URL, and a common set of tools (`memory_store`, `memory_search`, `memory_summarize_session`, and a few more) that any MCP client can call. It doesn't try to be clever about what to remember — it just gives agents a place to put things down and pick them back up, scoped per project so a search in one codebase doesn't surface noise from another.

---

## How this compares

There's already a well-populated field of "memory for AI agents" projects. Worth being upfront about where this one actually sits, instead of pretending the space is empty.

| Project | How an agent talks to it | Who decides what's remembered | Semantic vector search |
|---|---|---|---|
| [mem0](https://github.com/mem0ai/mem0) | SDK / hosted API | Automatic — an LLM extracts facts on ingest | Yes, wrapped behind the extraction layer |
| [Zep / Graphiti](https://github.com/getzep/graphiti) | SDK, or an official MCP server | Automatic — entities/relationships extracted into a knowledge graph | Secondary to graph traversal |
| [Letta](https://github.com/letta-ai/letta) (formerly MemGPT) | Full stateful-agent platform, server + SDK | Semi-automatic — the agent's own LLM pages memory in/out | Yes, for archival memory |
| [Memorix](https://github.com/AVIDS2/memorix) | MCP-native, no server to run | Explicit — the calling agent writes | **Fallback only** (~1.8s), keyword search is primary |
| threadctx-mcp | MCP-native | Explicit + optional passive git capture | **Paid cloud tier** — local mode is keyword-only |
| **BlueOcean Vector** | MCP-native, one shared server | Explicit — the calling agent writes | **Primary and always-on** |

Two honest takeaways:

- **The "MCP-native, works with any client" niche isn't empty** — Memorix already lives there, with more built-in tools. What's different here is that vector search is the primary retrieval path rather than a fallback or something gated behind a paid tier, the default embedding model is genuinely multilingual (Thai+English tested), and it's built to run as one shared, persistent server rather than a zero-install per-agent tool — bearer-token auth, a documented path to ECS, Kubernetes-ready health probes, and real fixes for the concurrency problems a *shared* server actually hits.
- **No automatic extraction or consolidation** — unlike mem0, Graphiti, Letta, cognee, or LangMem, nothing here reads your conversation and decides what's worth remembering. That's a deliberate simplicity trade-off, not a missing feature: an agent has to explicitly call `memory_store`. If you want a system that reasons about what to keep on your behalf, one of the projects above will do that better than this will.

### Memory shouldn't try to hold a million lines

Some projects are a million lines of code. And no memory system — BlueOcean Vector included — should try to store all of it. Storing code is a code-search tool's job, not a memory server's.

BlueOcean's job is narrower and more useful: **remember what mattered, and where to find it.** It holds the decisions, the architecture, the "we tried that, it didn't work" — the condensed knowledge an agent would otherwise have to rediscover from a million lines — plus just enough context to point the agent back at the real code when it needs details.

The result is that memory grows with *what's actually worth remembering*, not with the size of the codebase. A million-line project can have a few thousand memory entries. That keeps retrieval cheap no matter how big the project gets.

### The token math

Reading memory back is where that distinction pays off. The cheapest alternative — a skill or plugin that dumps project notes into a `.remember` file an agent reads back — works great until the file outgrows the context window, then it silently stops being useful.

BlueOcean caps every search at a token budget (default **2000 tokens**, configurable via `BLUEOCEAN_MAX_TOKENS`). Semantic search pulls only the relevant entries, then splits the budget: ~60% for condensed summaries, ~40% for the full content of the top hits. Entries beyond the budget are truncated, never dumped wholesale.

| Approach | Cost per retrieval | Grows with memory size? |
|---|---|---|
| **BlueOcean Vector** (`memory_search`) | **capped** at the token budget (default 2000) | No — bounded, regardless of collection size |
| `.remember` file (read whole file) | equal to the whole file size | Yes — linear; eventually exceeds the context window |
| `.remember` file (agent reads one section) | equal to that section | Partial — but the agent must guess which section without relevance ranking |

A real search against a small demo project returned **121 tokens** for one summary + one full entry — a few percent of the 2000-token budget, and that budget never grows as the project accumulates memory. With a plain file, the same read costs the entire file every time, so a 5k-entry project (hundreds of thousands of tokens) is unreadable in one shot.

---

## How it fits together

```
┌────────────┐ ┌──────┐ ┌────────┐ ┌───────────────┐ ┌──────┐
│Claude Code │ │Cursor│ │ Codex  │ │Gemini/Antigrav│ │ Kiro │  ...any MCP-http tool
└─────┬──────┘ └──┬───┘ └───┬────┘ └───────┬───────┘ └──┬───┘
      └───────────┴─────────┴──────────────┴────────────┘
                            │  http://localhost:8765/mcp
                 ┌───────────────────────────┐
                 │  blueocean-mcp             │   Python MCP server
                 │  (one shared, persistent   │   (docker compose)
                 │  server, not per-agent)    │
                 └─────────────┬─────────────┘
                               │
                 ┌───────────────────────────┐
                 │  Qdrant (vector DB)        │   Docker locally → ECS Fargate in the cloud
                 └───────────────────────────┘
```

A few design choices worth knowing about:

| Choice | Why |
|---|---|
| **One server, reached by URL** | Every mainstream MCP client (and plenty of niche ones) has its own "add a remote server" command. Point them all at the same URL and none of them need bespoke config-file editing from us. |
| **Qdrant underneath, one collection per project** | Memory for `project-a` never leaks into a search for `project-b`. |
| **Multilingual by default** | Embedding model is `intfloat/multilingual-e5-large`, so project notes mixing Thai and English (or any other pair it covers) still search across both without extra setup. |
| **Token-budgeted reads** | `memory_search` returns short summaries first and only expands the top matches into full content until it hits a budget you set — agents stay cheap to run even against a memory store that's grown large. |

`stdio` transport also works if you'd rather each tool spawn its own local process instead of talking to the shared server — see [Alternative: stdio](#alternative-stdio-per-agent-local-process) below. The shared HTTP server is still the recommended path; stdio spins up a separate copy of the embedding model per agent.

---

## Getting started

```bash
# 1. Bring up Qdrant + the MCP server (both run in the background via docker compose)
./scripts/setup_local.sh

# 2. Register the URL with whichever agents you use
./scripts/register_mcp.sh
```

That's it. `setup_local.sh` starts both containers, waits for Qdrant to actually respond (not just "the process started"), copies `.env.example` to `.env` on first run, and syncs the Python package. `register_mcp.sh` then calls each tool's own `mcp add` CLI (or, for Cursor, edits `~/.cursor/mcp.json` directly, since Cursor's CLI only works while the app is open) to point it at `http://localhost:8765/mcp`.

For any other MCP-http-capable tool, including ones we've never heard of, just give it the same URL through that tool's own "add remote MCP server" feature:

```
http://localhost:8765/mcp
```

### Teaching agents to actually use it

Registering the server gets the tools *available*; it doesn't make an agent reach for them on its own. `scripts/install_skill.sh` installs a small skill — "check memory at the start of a session, write to it before you run low on context" — into whichever agents you use, so the habit is there without you repeating it in every prompt:

```bash
./scripts/install_skill.sh          # interactive picker
./scripts/install_skill.sh all      # install into every supported tool found
./scripts/install_skill.sh --list   # see what's installed where
```

It's one canonical `SKILL.md`, symlinked into each tool's own skills directory — edit it once, every tool picks up the change.

### Alternative: stdio (per-agent local process)

No Docker available, or you'd rather not run a shared server? Run:

```bash
uv run blueocean-mcp --transport stdio --qdrant-url http://localhost:6333
```

and point the tool's MCP config at the `command` (see `.venv/bin/blueocean-mcp`) instead of a `url`.

---

## The tools an agent gets

| Tool | What it does |
|---|---|
| `memory_store` | Save an entry — content, a condensed summary, an importance score, and area/module tags |
| `memory_search` | Semantic search, token-budgeted: cheap summaries first, full content for what fits |
| `memory_get` | Fetch one entry's full content by ID |
| `memory_delete` | Remove one entry by ID |
| `memory_list_projects` | List every project that has a memory collection |
| `memory_manifest` | See what areas/modules exist before searching, so you scope the query sensibly |
| `memory_summarize_session` | Leave a condensed handoff note for whichever agent picks this up next |
| `memory_stats` | Counts and distribution, mostly for admin/debugging |

A reasonable agent workflow: call `memory_manifest` then `memory_search` at the start of a session to load context cheaply; `memory_store` real decisions as you go (importance 5 for "why we chose X over Y", importance 3 for routine status); call `memory_summarize_session` before switching tools or running low on budget.

---

## Configuration

Everything lives in `.env` (copy `.env.example` to start). The defaults work for local, single-machine use; the interesting knobs are:

- `BLUEOCEAN_EMBEDDING` — `fastembed` (default, local and free), `openai`, or `bedrock`. Pin `BLUEOCEAN_EMBED_MODEL` too: vectors written with one model can't be meaningfully searched with another, so local and cloud need to agree on it.
- `BLUEOCEAN_QDRANT_URL` — where Qdrant lives.
- `BLUEOCEAN_MAX_TOKENS` / `BLUEOCEAN_TOP_K` — the default search budget.
- `BLUEOCEAN_AUTH_TOKEN` — unset by default (fine for `127.0.0.1`-only use). See [Security](#security) if you're exposing this beyond your own machine.

Transport (`streamable-http` vs `stdio`) is a CLI flag, not an env var — it's a "how do I run this" choice made at startup, not a persistent setting.

---

## Admin CLI

```bash
uv run blueocean-admin stats <project>
uv run blueocean-admin manifest <project>
uv run blueocean-admin list
uv run blueocean-admin export <project>
uv run blueocean-admin prune <project> --older-days 90 --max-importance 2 [--dry-run]
uv run blueocean-admin snapshot <project> [--out ./backups]
uv run blueocean-admin restore <project> <snapshot-file> --yes
uv run blueocean-admin generate-token --write-env
```

> [!WARNING]
> If more than one agent session shares a project, `prune` doesn't know that. It deletes whatever matches your filters, even entries another session wrote five minutes ago. Run with `--dry-run` first, and prefer narrow filters over a broad reset.

`export` only dumps payload as JSON (`with_vectors=False`) — restoring from it means re-embedding everything from scratch, not a real point-in-time restore. `snapshot`/`restore` use Qdrant's own native snapshot mechanism instead: vectors, payload, and index state, captured atomically. `snapshot` downloads the file to local disk and deletes the server-side copy once the download is confirmed intact (backups living only inside the same Qdrant volume they're backing up out of aren't backups). `restore` overwrites the project's current data, so it requires `--yes`.

Project names are validated strictly (`^[a-z0-9][a-z0-9_-]*$`, matching the directory-name convention this project already recommends) rather than silently normalized — two agents guessing slightly different spellings of the same project (`"Team A"` vs `"team-a"`) used to merge into one collection with no warning; now the mismatched one is rejected instead.

---

## Running the tests

Test files under `tests/` are standalone scripts (`if __name__ == "__main__":`), not `pytest`-discovered files — run them as modules:

```bash
uv run python -m tests.smoke
uv run python -m tests.auth
uv run python -m tests.mcp_e2e
uv run python -m tests.backup   # real snapshot -> delete collection -> restore cycle
```

`tests/auth.py` specifically checks that unauthenticated and wrong-token requests get rejected (401) and that a correct token works via both the header and the `?token=` query-param path.

---

## Security

No auth by default — reasonable for `127.0.0.1`-only local use, not reasonable the moment this is reachable from anywhere else.

> [!IMPORTANT]
> If you expose this server beyond localhost (a shared machine, the cloud), set `BLUEOCEAN_AUTH_TOKEN` before you do anything else.

```bash
uv run blueocean-admin generate-token --write-env
docker compose up -d --force-recreate blueocean-mcp
./scripts/register_mcp.sh   # reads the token from .env, re-sends it to every tool
```

Not every tool can set a custom header when registering a remote server by URL, so the server accepts the token two ways and each client uses whichever it supports:

- `Authorization: Bearer <token>` — Claude Code, Gemini/Antigravity
- `?token=<token>` on the URL — Codex, Kiro, Cursor

`stdio` transport skips this entirely: it's a locally spawned subprocess, already gated by OS process-spawn permissions rather than sitting on the network.

`GET /health` is deliberately unauthenticated and checks that Qdrant is actually reachable, not just that the process is alive. It's what `docker-compose.yml`'s healthcheck polls.

Set the token via `BLUEOCEAN_AUTH_TOKEN` (env var / `.env`), not the `--auth-token` CLI flag — a value passed as a CLI argument is visible to any other local user via `ps`. Request access logging is also off by default (`access_log=False`), since three of the five supported clients send the token as `?token=...` and a plain access log would put it in plaintext in your logs on every single request.

---

## Deploying beyond localhost

`docker compose up -d` runs two long-lived services: `qdrant` (port 6333) and `blueocean-mcp` (port 8765). For the cloud, the same two services move to ECS Fargate (or Qdrant Cloud plus a small Fargate/App Runner service for `blueocean-mcp`) — register the public URL with each tool exactly the way you would locally. The `Dockerfile` pins the embedding model so vectors produced in the cloud are compatible with ones produced on your laptop.

Kubernetes doesn't read `docker-compose.yml`'s `healthcheck:` — it needs its own probes in the Pod spec, but they can point at the same path:

```yaml
readinessProbe:
  httpGet: { path: /health, port: 8765 }
livenessProbe:
  httpGet: { path: /health, port: 8765 }
```

---

## A few gotchas worth knowing before you touch this

- **`qdrant-client` is pinned to the Qdrant server's exact version** (see the image tag in `docker-compose.yml`). Qdrant versions its client and server in lockstep, and the API has changed between releases — `.search()` was removed in favor of `.query_points()` in 1.19. If you bump the server image, bump `qdrant-client` to match and re-run the test suite; don't jump several versions on real data without a snapshot first.
- **`mcp` is pinned `>=2.0.0,<3.0.0`**, tighter than most dependencies here. Its API (`mcp.server.mcpserver.MCPServer` and friends) has changed shape significantly between releases, and a loose constraint risks a Docker build silently resolving something incompatible — Docker builds don't use `uv.lock`.
- **Embedding provider and model are a matched pair.** Switch either one and old vectors become unsearchable garbage against new ones. Pin the model in `.env` rather than trusting a library default that might change out from under you.

---

## License

MIT — see [LICENSE](LICENSE).
