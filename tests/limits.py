"""Resource guards on the shared VectorStore: content-size ceiling and
top_k clamping.

This is a shared server that multiple agents hit at once, so a single
misbehaving caller shouldn't be able to store unbounded payloads or force a
wide-open Qdrant scan via an absurd top_k. Run with:

    uv run python -m tests.limits
"""

from blueocean_mcp.config import MAX_CONTENT_CHARS, MAX_TOP_K
from blueocean_mcp.embeddings import create_embedder
from blueocean_mcp.vector_store import VectorStore, clamp_top_k

from ._helpers import reset_project

PROJECT = "limits-test"


def main() -> None:
    reset_project(PROJECT)
    embedder = create_embedder()
    store = VectorStore(embedder)

    print("== clamp_top_k is a pure clamp ==")
    assert clamp_top_k(5) == 5
    assert clamp_top_k(0) == 1
    assert clamp_top_k(-10) == 1
    assert clamp_top_k(MAX_TOP_K + 5000) == MAX_TOP_K
    print(f"  MAX_TOP_K={MAX_TOP_K} enforced, in-range values pass through unchanged")

    print("== search() survives an absurd top_k instead of erroring or hanging ==")
    store.store(
        PROJECT, area="a", module="m", content="hello world", summary="hi", importance=3
    )
    hits = store.search(PROJECT, "hello", top_k=10_000_000)
    assert hits, "expected the one stored entry back"
    print(f"  requested top_k=10,000,000, got {len(hits)} hit(s) back with no error")

    print("== store() rejects content over the size ceiling ==")
    too_big = "x" * (MAX_CONTENT_CHARS + 1)
    try:
        store.store(PROJECT, area="a", module="m", content=too_big, summary="hi", importance=3)
        raise AssertionError("expected ValueError for oversized content")
    except ValueError as e:
        print(f"  rejected as expected: {e}")

    print("== store() still accepts content right at the ceiling ==")
    exactly_at_limit = "y" * MAX_CONTENT_CHARS
    point_id = store.store(
        PROJECT, area="a", module="m", content=exactly_at_limit, summary="hi", importance=3
    )
    assert store.get(PROJECT, point_id) is not None
    print("  accepted content at exactly MAX_CONTENT_CHARS")

    print("\nLIMITS TEST PASSED")


if __name__ == "__main__":
    main()
