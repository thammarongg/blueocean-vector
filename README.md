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
  - [Hermes: automatic recall](#hermes-automatic-recall)
  - [Alternative: stdio](#alternative-stdio-per-agent-local-process)
- [The tools an agent gets](#the-tools-an-agent-gets)
- [Configuration](#configuration)
- [Admin CLI](#admin-cli)
- [Running the tests](#running-the-tests)
- [Usage telemetry](#usage-telemetry)
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

Checked against each project's own repository on 2026-11-13; the sources and confidence for every cell are in [docs/market-research-2026-11.md](docs/market-research-2026-11.md). This field moves fast, so treat the table as dated.

| Project | How an agent talks to it | Who decides what's remembered | Retrieval |
|---|---|---|---|
| [claude-mem](https://github.com/thedotmack/claude-mem) | Plugin with hooks for Claude Code, Codex, Gemini, Hermes, Copilot, OpenCode and others; MCP search tools | Automatic — hooks hand tool activity to a local LLM "observer" that writes observations (explicit saves also possible) | Hybrid: SQLite FTS5 keyword + Chroma vectors |
| [mem0](https://github.com/mem0ai/mem0) | SDK / hosted API, plus hook plugins for Claude Code, Codex, Cursor, Hermes, OpenCode (needs a Mem0 Platform key) | Automatic — an LLM extracts facts on ingest | Fused: semantic + BM25 keyword + entity matching |
| [Zep / Graphiti](https://github.com/getzep/graphiti) | SDK, or its own MCP server | Automatic — entities/relationships extracted into a knowledge graph | Hybrid: semantic + BM25 keyword + graph traversal |
| [Memorix](https://github.com/AVIDS2/memorix) | MCP over stdio by default, or one shared HTTP server (Docker, bearer token); integrations for ~20 clients | Explicit writes, plus hook capture (on by default for Claude Code), git-commit ingestion and optional LLM extraction; hook captures stay pending until qualified | Keyword-first (BM25 / FTS5); hybrid semantic when an embedding provider is configured |
| [threadctx-mcp](https://github.com/threadctx-dev/threadctx-mcp) | MCP-native | Explicit + optional passive git-commit capture | Keyword locally; semantic search is the **paid cloud tier** |
| **BlueOcean Vector** | MCP-native, one shared server (bearer token, optional TLS) | Explicit — the calling agent writes (optional automatic recall through the [Hermes provider](#hermes-automatic-recall)) | Dense vectors only, always on, multilingual by default; **no keyword path yet**, so exact identifiers (a commit SHA, an issue number) can miss |

[Letta](https://github.com/letta-ai/letta) (formerly MemGPT) used to sit in this table as a memory server; its current source is [letta-code](https://github.com/letta-ai/letta-code), a coding-agent harness with its own memory, so it is a different kind of thing now.

The agents' own built-in memories ([Claude Code auto memory](https://code.claude.com/docs/en/memory), [Codex memories](https://learn.chatgpt.com/docs/customization/memories?surface=app)) are documented as machine-local, and neither describes sharing with another tool.

Two honest takeaways:

- **The "shared memory for every coding agent" niche is crowded, and BlueOcean is the smallest entrant in it.** claude-mem and mem0 both reach the same clients through hooks, and Memorix now runs as a shared HTTP server too. What is still different here: it is fully local and free with no account or API key (mem0's plugins need a platform key); it is MIT-licensed; the default embedding model is genuinely multilingual (Thai+English tested) with no extra setup; and it is built to run as one shared, persistent server across tools *and* machines — bearer-token auth, optional TLS, a documented path to ECS, Kubernetes-ready health probes, and real fixes for the concurrency problems a *shared* server actually hits. Where it is behind: every larger project above fuses keyword and semantic search, and this one is still dense-only.
- **No automatic extraction or consolidation** — unlike mem0, Graphiti, Letta, cognee, or LangMem, nothing here reads your conversation and decides what's worth remembering. That's a deliberate simplicity trade-off, not a missing feature: an agent has to explicitly call `memory_store`. The one automatic write is the [Hermes provider](#hermes-automatic-recall)'s Turn Trace, a mechanical record of a local session's turn rather than a judgment about what matters, and it is kept out of every search that doesn't ask for it. If you want a system that reasons about what to keep on your behalf, one of the projects above will do that better than this will.

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

It's one canonical `SKILL.md`, symlinked into each tool's own skills directory — edit it once, every tool picks up the change. The repo carries a copy at `skills/blueocean-memory/SKILL.md`, and the installer seeds `~/.agents` from it on a machine that doesn't have it yet; an existing canonical file is never overwritten, so local edits survive.

To update an installed skill from the repository, use `./scripts/update.sh` as described in [Updating an existing installation](#updating-an-existing-installation). It compares the files and backs up the installed skill before replacing it when different. Running the installer again does not update an existing skill.

### Hermes: automatic recall

Every other client only reads memory when the agent decides to call `memory_search`. [Hermes](https://github.com/NousResearch/hermes-agent) can do better: it calls its active *memory provider* itself, before and after every turn. `integrations/hermes/blueocean/` is that provider.

- **Before each turn** it finds which project the conversation is about and injects the relevant entries. It takes the project from a name in your message, or from Hermes's working directory, and you can correct it with the `blueocean_set_project` tool. The default budget is 600 tokens, and an entry already injected in the same session is not injected again.
- **After each turn** on a platform listed in `sync_turns_platforms` (default: Hermes's local sessions `cli`, `tui`, `desktop` and `acp`; Discord, Telegram and other chat apps are left out, so casual talk never reaches the store every agent reads) it stores a **Turn Trace** (`kind=hermes_turn`, `importance=1`, area `hermes`, module `turns`). Before storing, it strips recalled memory and redacts credentials: `NAME_TOKEN=`/`*_SECRET`/`*_KEY`/`credentials`/`passphrase` assignments, quoted JSON values, Bearer and `Authorization: Basic` headers, OpenAI `sk-`, Stripe `sk_live_`/`rk_test_`, Google `AIza`, AWS `AKIA`/`ASIA`, GitHub, GitLab `glpat-`, Hugging Face `hf_`, `npm_` and Slack tokens, PEM and PGP private keys, and URL passwords. The same redaction is applied to the recall query before it is sent. Traces are excluded from every search that doesn't ask for `kind="hermes_turn"`, so they never compete with real entries.
- Redaction is best-effort pattern matching, so don't paste secrets you can't afford to have stored. Known misses include bare JWTs, Discord and Telegram bot tokens, a password stated in prose ("my password is X", in English or Thai), `mysql -pX`, `curl -u user:pass`, and unquoted values with spaces (`password = my pass phrase` keeps `pass phrase`).
- Explicit writes still go through the `blueocean` MCP server entry. Keep it configured.

Update the server first: without the server change that hides traces, every Turn Trace would show up in every agent's default search.

```bash
./scripts/update.sh                     # rebuild the server (hides Turn Traces from default search)
./scripts/install_hermes_plugin.sh      # symlink into ~/.hermes/plugins/blueocean
hermes memory setup blueocean           # pick it as the memory provider
# then restart the Hermes gateway
```

Settings (`hermes memory setup`, stored in `~/.hermes/blueocean.json`): `url`, `prefetch_max_tokens`, `ignore_projects` (globs never auto-detected; default skips test and probe projects), `sync_turns`, `sync_turns_platforms`, and `projects`.

`projects` is an allowlist with aliases. Without it, any project in the store can be picked up from a message, including ones named like ordinary words (`kubernetes`, `kops`), and a picked project stays active for the rest of the session. With it, only the listed projects and their aliases are detected, `ignore_projects` no longer applies, and Hermes's working directory only counts when it is a listed name or alias. A plain list (`["blueocean-vector", "tidepool"]`) means projects without aliases; a listed name the server would reject is skipped:

```json
{
  "projects": {"blueocean-vector": ["blueocean"], "thalassaops": [], "tidepool": []},
  "sync_turns_platforms": ["cli", "tui", "desktop", "acp"]
}
```

In `hermes memory setup` the same allowlist is typed as `blueocean-vector:blueocean|bo, thalassaops, tidepool` (`|` separates aliases) and stored as that text. A blank answer keeps the current value; edit `~/.hermes/blueocean.json` to clear it. `blueocean_set_project` can still choose any valid project explicitly. Old traces can be removed with `blueocean-admin prune <project> --kind hermes_turn --older-days 30`.

If the server is down, recall quietly returns nothing. After one failure the provider pauses for 30 s instead of making every turn wait. A write the server did not confirm within its deadline is dropped rather than resent, so a Turn Trace is never stored twice. To roll back, set `memory.provider` back to empty.

### Alternative: stdio (per-agent local process)

No Docker available, or you'd rather not run a shared server? Run:

```bash
uv run blueocean-mcp --transport stdio --qdrant-url http://localhost:6333
```

and point the tool's MCP config at the `command` (see `.venv/bin/blueocean-mcp`) instead of a `url`.

---

## Updating an existing installation

From your existing `blueocean-vector` checkout, commit or stash any local changes first, then run:

```bash
./scripts/update.sh
```

The script stops if the checkout has local changes, pulls from the current branch's upstream with `--ff-only`, syncs local Python dependencies, rebuilds the MCP server, and waits up to 180 seconds for the services to become healthy. It stops on failure; earlier successful steps are not rolled back.

After the runtime update succeeds, it compares `skills/blueocean-memory/SKILL.md` in the updated repository with `~/.agents/skills/blueocean-memory/SKILL.md`:

- **Identical:** skips the skill without rewriting it.
- **Different:** backs up the installed file as `SKILL.md.bak.<unique suffix>` beside it, then replaces it with the repository version. Local customizations remain in the backup; they are not merged automatically.
- **Missing:** creates the shared skill from the repository copy.

Agents linked to the shared skill receive the updated file. Start a new agent session to load the new instructions. Manually installed copies elsewhere are not updated; use `./scripts/install_skill.sh --list` to inspect your installation. The update script does not register new clients or create agent symlinks; use `./scripts/install_skill.sh all` for initial skill installation.

For a **stdio installation**, run:

```bash
./scripts/update.sh --stdio
```

This pulls code, syncs dependencies, and updates the skill without invoking Docker. Restart the MCP process through your client afterward.

If your checkout predates `scripts/update.sh`, first run `git pull --ff-only` to get it. Alternatively, update the runtime manually with these commands, one at a time (these do not update the installed skill):

```bash
# Check for local changes before pulling
git status --short

# Download the latest code from your branch's upstream
git pull --ff-only

# Rebuild and replace the shared MCP server with the updated code
docker compose up -d --build blueocean-mcp

# Update local Python dependencies for tools such as blueocean-admin
uv sync --extra dev
```

If you have local changes, commit or stash them before pulling. If `git pull --ff-only` fails, stop and resolve the Git issue before continuing. The MCP server is briefly unavailable while its container is replaced. A plain `docker compose restart` does not rebuild the image and therefore does not apply source-code changes.

Check that the services are ready after the update:

```bash
docker compose ps
curl -fsS http://localhost:8765/health
```

Wait for both services to show `healthy` and for `/health` to return `"status":"ok"`. If the server does not become healthy, inspect its logs:

```bash
docker compose logs --tail=100 blueocean-mcp
```

Reconnect the MCP client if it lost its connection during the update. You do not need to register the server again unless its URL or authentication token changed.

Memory persists in the `qdrant_storage` Docker volume, and telemetry and pricing data persist in `./data`. Keep the same checkout, Compose project name, and `.env` when updating. **Do not run `docker compose down -v` as an update step:** it deletes the Compose-managed volumes, including the stored memory. Keep `./data` as well.

---

## The tools an agent gets

| Tool | What it does |
|---|---|
| `memory_store` | Save an entry — content, a condensed summary, an importance score, and area/module tags |
| `memory_search` | Semantic search, token-budgeted: cheap summaries first, full content for what fits. Scope by area/module/importance/time, and by `kind` — `kind="session_summary"` for only session records, `exclude_kinds=["session_summary"]` to keep them out |
| `memory_get` | Fetch one entry's full content by ID |
| `memory_delete` | Remove one entry by ID |
| `memory_list_projects` | List every project that has a memory collection |
| `memory_manifest` | See what areas/modules exist before searching, so you scope the query sensibly |
| `memory_summarize_session` | Leave a condensed handoff note for whichever agent picks this up next. Lands in the `sessions` module of the given area unless you pass a `module`; the session id goes to metadata, not the manifest |
| `memory_stats` | Counts and distribution, mostly for admin/debugging |

A reasonable agent workflow: call `memory_manifest` then `memory_search` at the start of a session to load context cheaply; `memory_store` real decisions as you go (importance 5 for "why we chose X over Y", importance 3 for routine status); call `memory_summarize_session` before switching tools or running low on budget.

---

## Configuration

Everything lives in `.env` (copy `.env.example` to start). The defaults work for local, single-machine use; the interesting knobs are:

- `BLUEOCEAN_EMBEDDING` — `fastembed` (default, local and free), `openai`, or `bedrock`. Pin `BLUEOCEAN_EMBED_MODEL` too: vectors written with one model can't be meaningfully searched with another, so local and cloud need to agree on it.
- `BLUEOCEAN_QDRANT_URL` — where Qdrant lives.
- `BLUEOCEAN_MAX_TOKENS` / `BLUEOCEAN_TOP_K` — the default search budget.
- `BLUEOCEAN_AUTH_TOKEN` — unset by default (fine for `127.0.0.1`-only use). See [Security](#security) if you're exposing this beyond your own machine.
- `BLUEOCEAN_TLS_CERTFILE` / `BLUEOCEAN_TLS_KEYFILE` — serve HTTPS with this certificate and key. Unset by default (plain HTTP). See [TLS](#tls).
- `BLUEOCEAN_SESSION_IDLE_SECONDS` — how many seconds of silence end a telemetry session (default `1800`). See [Usage telemetry](#usage-telemetry).
- `BLUEOCEAN_TRACE_KINDS` — record kinds kept out of every search that doesn't name them with `kind=` (default `hermes_turn`, comma-separated, empty disables). Traces are written mechanically every turn; without this they would crowd real entries out of other agents' search budget.

Transport (`streamable-http` vs `stdio`) is a CLI flag, not an env var — it's a "how do I run this" choice made at startup, not a persistent setting.

---

## Admin CLI

```bash
uv run blueocean-admin stats <project>
uv run blueocean-admin manifest <project>
uv run blueocean-admin list
uv run blueocean-admin export <project>
uv run blueocean-admin prune <project> --older-days 90 --max-importance 2 [--dry-run]
uv run blueocean-admin prune <project> --kind hermes_turn --older-days 30 [--dry-run]
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
uv run python -m tests.tls_transport   # HTTPS end to end, bad certificate settings, the compose healthcheck
uv run python -m tests.mcp_e2e
uv run python -m tests.backup   # real snapshot -> delete collection -> restore cycle
uv run python -m tests.health   # /health diagnostics + the cloud-provider self-test TTL cache
uv run python -m tests.trace_kinds            # trace kinds excluded by default; prune --kind
uv run python -m tests.hermes_logic           # Hermes provider: pure logic
uv run python -m tests.hermes_bridge          # Hermes provider: bridge vs a real server
uv run python -m tests.hermes_contract        # Hermes provider loaded by Hermes itself (skips without Hermes)
uv run python -m tests.install_hermes_plugin
```

`tests.hermes_contract` runs on Hermes's own dependency venv (`~/.hermes/installs/*/environments/*/venv/bin/python`, override with `HERMES_PYTHON`), never imports `hermes_bootstrap`, and fails if it changes any file in the Hermes install.

`tests/auth.py` specifically checks that unauthenticated and wrong-token requests get rejected (401) and that a correct token works via both the header and the `?token=` query-param path.

---

## Usage telemetry

Every MCP tool call is recorded to a local SQLite event log: which tool, which
project, which agent (from the MCP `clientInfo` handshake), how long it took,
whether it failed, how many results a search returned, and what the embedding
cost. It is on by default and never leaves the machine.

It never stores query text, memory content, summaries, entry metadata, or
bearer tokens. A test asserts this by pushing unique sentinel strings through
every input and dumping every column to prove they are absent.

Each call also gets a session id, and `session_source` says where it came
from. A handshake-era client echoes `mcp-session-id` (`header`). A 2026-07-28
client sends no session at all, so the server derives one from the agent's
name and version, the caller (a hash of its bearer token, or its address) and
the project, and starts a new one after `BLUEOCEAN_SESSION_IDLE_SECONDS` of
silence (`derived`). On stdio the process stands in (`process`). The limit of
`derived`: two windows of the same agent on the same project, from the same
caller, count as one session. The token hash is only held in memory to tell
callers apart; it is never written to the database.

Turn it off with `BLUEOCEAN_TELEMETRY=0`, in which case no database file is
opened and the HTTP endpoints answer 503.

Three ways to read it:

```bash
# From the terminal (reads over HTTP; the server owns the file)
uv run blueocean-admin usage --days 7 --by tool
uv run blueocean-admin usage --unused     # entries never retrieved

# In the browser
open "http://127.0.0.1:8765/dashboard?token=$BLUEOCEAN_AUTH_TOKEN"
```

Agents can call the `memory_usage` tool, which returns a compact summary
inside a token budget, with `view="unused" | "tools" | "errors"` for detail.

Embedding prices come from a small built-in table. To refresh them from
OpenRouter's public embedding price feed:

```bash
uv run blueocean-admin usage --refresh-prices
```

That is the only command in the project that sends anything off the machine.
The server never calls out on its own.

Note on the audit trail: destructive operations are recorded, and rows the
server observed itself are distinguished from rows a client reported. With a
single shared auth token this explains accidents; it cannot prove a row was
not forged.

---

## Security

No auth by default — reasonable for `127.0.0.1`-only local use, not reasonable the moment this is reachable from anywhere else.

> [!IMPORTANT]
> If you expose this server beyond localhost (a shared machine, the cloud), set `BLUEOCEAN_AUTH_TOKEN` before you do anything else, and serve it over [TLS](#tls). Without TLS the token and everything you store cross the network in the clear.

```bash
uv run blueocean-admin generate-token --write-env
docker compose up -d --force-recreate blueocean-mcp
./scripts/register_mcp.sh   # reads the token from .env, re-sends it to every tool
```

Not every tool can set a custom header when registering a remote server by URL, so the server accepts the token two ways and each client uses whichever it supports:

- `Authorization: Bearer <token>` — Claude Code, Gemini/Antigravity
- `?token=<token>` on the URL — Codex, Kiro, Cursor

`stdio` transport skips this entirely: it's a locally spawned subprocess, already gated by OS process-spawn permissions rather than sitting on the network.

`GET /health` is deliberately unauthenticated and checks that Qdrant is actually reachable, not just that the process is alive. It's what `docker-compose.yml`'s healthcheck polls. It also reports the active embedding provider/model, and for `openai`/`bedrock` (not `fastembed`, whose model load already gates process startup) validates credentials via a free control-plane call rather than the billed embed endpoint, caching the result for `BLUEOCEAN_HEALTH_EMBED_TTL` seconds (default 60) so a 10s probe interval doesn't turn into a provider API call on every hit:

```json
{"status": "ok", "qdrant": "reachable", "embedding": {"provider": "fastembed", "model": "intfloat/multilingual-e5-large", "ok": true}}
```

Set the token via `BLUEOCEAN_AUTH_TOKEN` (env var / `.env`), not the `--auth-token` CLI flag — a value passed as a CLI argument is visible to any other local user via `ps`. Request access logging is also off by default (`access_log=False`), since three of the five supported clients send the token as `?token=...` and a plain access log would put it in plaintext in your logs on every single request.

### TLS

Plain HTTP is the default and is fine on `127.0.0.1`. To reach the server across a network, either give it a certificate as below, or keep it private behind a reverse proxy that terminates TLS. TLS protects the `?token=` URLs on the wire, but the token still sits in each client's own config.

```bash
uv run blueocean-mcp --transport streamable-http --host 0.0.0.0 \
  --tls-certfile fullchain.pem --tls-keyfile privkey.pem
```

Under Docker Compose, put the files in `./data/tls/` (ignored by git, and visible in the container as `/data/tls/`), then add the container's paths to `.env`:

```bash
BLUEOCEAN_TLS_CERTFILE=/data/tls/fullchain.pem
BLUEOCEAN_TLS_KEYFILE=/data/tls/privkey.pem
```

```bash
docker compose up -d --force-recreate blueocean-mcp
```

The container runs as uid 10001, which must be able to read the key: on Linux, `sudo chown -R 10001:10001 data/tls`. The healthcheck switches to HTTPS by itself. It skips certificate checks because the certificate names your public host rather than `localhost`, and the probe stays inside the container and sends no secret.

The port serves HTTPS only, so everything pointing at the old `http://` URL has to change:

```bash
BLUEOCEAN_URL=https://memory.example.com:8765/mcp ./scripts/register_mcp.sh
export BLUEOCEAN_SERVER_URL=https://memory.example.com:8765   # blueocean-admin
```

For Hermes, set `url` in `~/.hermes/blueocean.json`.

**How it behaves:**

- Set both files or neither. A missing file, a key that doesn't match the certificate, or only one of the two stops the server at startup. It never falls back to plain HTTP.
- The key must be unencrypted: nothing could type a passphrase when a container starts. Protect it with file permissions.
- Certificates are read once at startup, so restart after renewing one: `docker compose restart blueocean-mcp`.
- Bound to `127.0.0.1`, `localhost` or `::1`, the server only accepts those names in the `Host` header (DNS rebinding protection) and answers anything else with a 421. To use a certificate for another name, bind to a non-loopback address, as Docker does.
- Not included: client certificates (mTLS), automatic renewal, and redirecting HTTP to HTTPS.

**Clients and private CAs.** A certificate from a public CA needs no client setup, which makes it the practical choice when several tools connect. A private CA or self-signed certificate has to be trusted by each client separately:

| Client | How it trusts a private CA |
|---|---|
| `blueocean-admin` | `SSL_CERT_FILE=/path/to/ca.pem`, set when you run it. |
| Hermes | The OS trust store, or `SSL_CERT_FILE`. That variable replaces the default roots for the whole Hermes process, so point it at a bundle that also holds the public CAs, or its other HTTPS calls will fail. |
| Claude Code | `NODE_EXTRA_CA_CERTS=/path/to/ca.pem`. This is from Claude Code's documentation and not covered by this repo's tests. |
| Codex, Gemini, Kiro, Cursor | Each has its own mechanism, if any: check its documentation. |

There is deliberately no option to skip certificate verification. It would send the token to anyone who can intercept the connection.

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

With [TLS](#tls) on, add `scheme: HTTPS` to both `httpGet` blocks.

**Linux bind-mount ownership.** The container runs as uid 10001 (`appuser` in the `Dockerfile`) and writes its telemetry database and pricing file into the `./data` bind mount. On macOS, Docker Desktop maps bind-mount ownership to the host user, so this is invisible. On Linux the container's uid is the file's uid, and a `./data` directory created by your normal user (typically uid 1000) is not writable by 10001. Create and hand over the directory **before** the first `docker compose up`:

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

If the stack already started with a wrongly-owned `./data`, chown is not enough on its own: after a failed open, `TelemetryWriter` disables itself for the life of that process, so telemetry stays empty even once the permission is fixed. Chown, then restart the service:

```bash
sudo chown -R 10001:10001 data && docker compose restart blueocean-mcp
```

(`chgrp` + `chmod g+rwx` works too if you would rather keep the directory owned by yourself; the server only needs to create and write files inside it. Hosts that need to inspect `data/telemetry.db` should do it read-only — see the note in "Usage telemetry" about SQLite locking across bind mounts.)

---

## A few gotchas worth knowing before you touch this

- **`qdrant-client` is pinned to the Qdrant server's exact version** (see the image tag in `docker-compose.yml`). Qdrant versions its client and server in lockstep, and the API has changed between releases — `.search()` was removed in favor of `.query_points()` in 1.19. If you bump the server image, bump `qdrant-client` to match and re-run the test suite; don't jump several versions on real data without a snapshot first.
- **`mcp` is pinned `>=2.3.0,<3.0.0`**, tighter than most dependencies here. Its API (`mcp.server.mcpserver.MCPServer` and friends) has changed shape significantly between releases, and a loose constraint risks a Docker build silently resolving something incompatible — Docker builds don't use `uv.lock` (which is how the live container ended up on 2.3.0 while the lock still said 2.0.0).
- **Embedding provider and model are a matched pair.** Switch either one and old vectors become unsearchable garbage against new ones. Pin the model in `.env` rather than trusting a library default that might change out from under you.
- **Project vocabulary lives in [`CONTEXT.md`](CONTEXT.md), and one term is a live trap.** "Summary" covers two things that do not substitute for one another: a *Session Summary* is written by an agent and carries what was decided and why, while a *Session Trace* is composed by the server and records only what a session touched. Only the first exists today (`memory_summarize_session`); the second is a decided design, not shipped. Read the glossary before naming anything new in this codebase.

---

## License

MIT — see [LICENSE](LICENSE).
