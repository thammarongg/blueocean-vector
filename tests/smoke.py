"""End-to-end smoke test for the blueocean vector store.

Tests store / search / get / delete / manifest / stats against a live Qdrant
using the default FastEmbed model. Run with:

    uv run python -m tests.smoke
"""

from blueocean_mcp.embeddings import create_embedder
from blueocean_mcp.token_budget import allocate
from blueocean_mcp.vector_store import VectorStore

from ._helpers import reset_project

PROJECT = "demo-project"


def main() -> None:
    reset_project(PROJECT)

    embedder = create_embedder()
    store = VectorStore(embedder)

    print("== storing entries ==")
    id1 = store.store(
        PROJECT,
        area="backend",
        module="auth",
        content="Authentication uses JWT with 24h expiry, stored in Redis. " * 8,
        summary="Auth: JWT 24h via Redis",
        importance=5,
    )
    id2 = store.store(
        PROJECT,
        area="frontend",
        module="login",
        content="Login page calls /api/auth/login and stores token in httpOnly cookie.",
        summary="Login: httpOnly cookie",
        importance=3,
    )
    id3 = store.store(
        PROJECT,
        area="infra",
        module="ci",
        content="CI runs on GitHub Actions, deploys to ECS Fargate on merge to main.",
        summary="CI/CD: GH Actions -> ECS Fargate",
        importance=4,
    )
    print(f"  stored {id1}, {id2}, {id3}")

    print("== manifest ==")
    manifest = store.manifest(PROJECT)
    print(manifest)
    # Real assertions, not just a print: exact area set and per-area counts,
    # so a broken/empty manifest() would fail this test loudly.
    assert manifest["project"] == PROJECT
    assert set(manifest["areas"].keys()) == {"backend", "frontend", "infra"}
    assert manifest["areas"]["backend"] == {"count": 1, "modules": ["auth"]}
    assert manifest["areas"]["frontend"] == {"count": 1, "modules": ["login"]}
    assert manifest["areas"]["infra"] == {"count": 1, "modules": ["ci"]}

    print("== search (scoped to backend) ==")
    hits = store.search(PROJECT, "how does authentication work?", top_k=5, area="backend")
    print(f"  {len(hits)} hits, top score {hits[0]['score']:.4f}")
    assert hits, "expected at least one backend hit"
    assert hits[0]["id"] == id1, "top backend hit should be the JWT/Redis auth entry"

    print("== search scoping actually excludes other areas (negative test) ==")
    # If the area filter were silently ignored, this query -- which strongly
    # matches the *infra* CI/CD entry -- would still return it when scoped to
    # "frontend". Scoping must exclude it entirely.
    scoped_hits = store.search(
        PROJECT, "GitHub Actions ECS Fargate deploy", top_k=5, area="frontend"
    )
    returned_ids = {h["id"] for h in scoped_hits}
    assert id3 not in returned_ids, (
        "area='frontend' search returned the infra/CI entry -- "
        "area filter is not actually scoping results"
    )
    if scoped_hits:
        assert all(h["id"] == id2 for h in scoped_hits), (
            "area='frontend' search returned an entry outside the frontend area"
        )

    print("== token budget on those hits ==")
    alloc = allocate(hits, 2000)
    print(f"  summary entries: {len(alloc.summaries)}, "
          f"full entries: {len(alloc.full_payloads)}, "
          f"truncated: {alloc.truncated}, "
          f"tokens: {alloc.total_tokens}")
    assert alloc.total_tokens <= 2000, "budget exceeded"
    assert alloc.summaries[0]["id"] == id1
    assert alloc.summaries[0]["summary"] == "Auth: JWT 24h via Redis"

    print("== get ==")
    got = store.get(PROJECT, id1)
    assert got and "JWT" in got["content"]
    print(f"  got entry {got['id'][:8]}... ok")

    print("== stats ==")
    stats = store.stats(PROJECT)
    print(f"  total entries: {stats['total_entries']}")
    assert stats["total_entries"] == 3
    assert stats["importance_distribution"] == {"3": 1, "4": 1, "5": 1}
    assert stats["area_distribution"] == {"backend": 1, "frontend": 1, "infra": 1}

    print("== delete ==")
    store.delete(PROJECT, id3)
    assert store.stats(PROJECT)["total_entries"] == 2
    print("  deleted id3, now 2 entries")

    print("== list projects ==")
    projects = store.list_projects()
    print(f"  projects: {projects}")
    assert PROJECT in projects, "list_projects() did not include this project"

    print("\nSMOKE TEST PASSED ✅")


if __name__ == "__main__":
    main()