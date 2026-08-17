"""Shared test helpers.

Every test in this directory is a standalone integration script (not pytest),
run against a real, already-running Qdrant. Without explicit cleanup, running
a test twice in a row accumulates leftover points in its project's collection,
which makes exact-count assertions fail for reasons unrelated to the code
under test (order-dependent, not reproducible). `reset_project` makes each
test's project collection empty before it runs, so results are deterministic
regardless of what earlier runs left behind.
"""

from pathlib import Path

from qdrant_client import QdrantClient

from blueocean_mcp.config import DEFAULT_QDRANT_URL
from blueocean_mcp.vector_store import collection_name

# Repo root and the venv's installed console script, computed relative to
# this file rather than hardcoded, so these tests work on any machine/user
# that clones the repo, not just the one that wrote them.
REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = str(REPO_ROOT / ".venv" / "bin" / "blueocean-mcp")


def reset_project(project: str, url: str | None = None) -> None:
    """Delete the given project's collection if it exists, ignoring absence."""
    client = QdrantClient(url=url or DEFAULT_QDRANT_URL)
    name = collection_name(project)
    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        client.delete_collection(name)