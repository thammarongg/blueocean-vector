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
            "WHILE WORKING: memory_store key decisions/architecture with "
            "importance=5, routine facts with importance=3.\n\n"
            "BEFORE ENDING a session (especially if about to run low on "
            "context/tokens): call memory_summarize_session so the next "
            "agent -- possibly a different tool entirely -- can pick up "
            "without re-reading everything.\n\n"
            "Always scope searches with area/module and prefer the summary "
            "layer to keep token usage low."
        ),
    )
    register_tools(mcp, store)
    return mcp