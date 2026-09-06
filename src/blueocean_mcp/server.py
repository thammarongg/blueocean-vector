"""MCP server wiring for blueocean memory using the mcp 2026 high-level API."""

import os

from mcp.server.mcpserver import MCPServer

from .embeddings import create_embedder
from .tools import register_tools
from .vector_store import VectorStore


def build_server() -> MCPServer:
    embedder = create_embedder()
    store = VectorStore(
        embedder,
        url=os.getenv("BLUEOCEAN_QDRANT_URL"),
        api_key=os.getenv("BLUEOCEAN_QDRANT_API_KEY"),
    )
    mcp = MCPServer(
        name="blueocean",
        title="BlueOcean Memory",
        description=(
            "Unified agent memory. Agents can store and retrieve project "
            "knowledge (decisions, architecture, session summaries) that "
            "persists across Claude Code, Codex, Cursor, and other MCP agents."
        ),
        instructions=(
            "This server is shared, persistent memory across ALL coding agents "
            "(Claude Code, Codex, Cursor, Gemini/Antigravity, Kiro, etc.) for "
            "the same project -- it survives switching agents or running out "
            "of tokens in one tool.\n\n"
            "AT THE START of every session working in a project directory: "
            "call memory_manifest(project) to see what's already known, then "
            "memory_search(project, \"current state\") scoped to relevant "
            "area(s) before starting work. Use the project directory's name "
            "as `project` unless told otherwise.\n\n"
            "EVERY TIME YOU DECIDE SOMETHING a future session would "
            "otherwise have to work out again -- why an approach was chosen "
            "over the alternatives, what an investigation concluded, what "
            "turned out not to work -- call memory_store right then, while "
            "the reasoning is still in front of you. Do not save it for "
            "later: sessions end without warning and nothing you were "
            "waiting to write gets written. Use importance=5 for decisions "
            "and architecture, importance=3 for routine facts. A session "
            "that decided nothing writes nothing, which is correct.\n\n"
            "memory_summarize_session records a whole session at once. It is "
            "worth calling when you are asked to wrap up, or when handing "
            "over to another tool -- but it is not a substitute for storing "
            "decisions as you reach them.\n\n"
            "Always scope searches with area/module and prefer the summary "
            "layer to keep token usage low."
        ),
    )
    register_tools(mcp, store)
    return mcp