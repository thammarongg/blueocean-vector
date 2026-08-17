"""Regression tests for concurrency bugs found during a production-readiness
review, specifically the scenario of multiple agents (or multiple team
members) reading/writing the same project at the same time:

1. Collection-creation race: concurrent `memory_store` calls to a brand-new
   project raced on Qdrant's `create_collection` (loser got a 409 and the
   whole call failed).
2. Shared-connection race: a single `QdrantClient` instance handling many
   concurrent threads intermittently raised
   `ResponseHandlingException: [Errno 9] Bad file descriptor`.
3. Delete TOCTOU race: concurrent `memory_delete` calls for the SAME point
   ID each independently saw it existed and each reported True, when at
   most one deletion should be able to claim that.
4. Mixed workload: store/search/stats/manifest all firing concurrently on
   one collection should never error or corrupt counts.

Run with:
    uv run python -m tests.concurrency
"""

import concurrent.futures
import random

from blueocean_mcp.embeddings import create_embedder
from blueocean_mcp.vector_store import VectorStore

from ._helpers import reset_project

PROJECT = "concurrency-test-project"
N = 20


def test_new_collection_race(store: VectorStore) -> None:
    print(f"== {N} concurrent memory_store calls to a brand-new project ==")
    project = f"{PROJECT}-new-collection"
    reset_project(project)

    def worker(i: int) -> str:
        return store.store(
            project, area="a", module="m",
            content=f"concurrent entry {i}", summary=f"entry {i}", importance=3,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        futures = [ex.submit(worker, i) for i in range(N)]
        ids, errors = [], []
        for f in concurrent.futures.as_completed(futures):
            try:
                ids.append(f.result())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    print(f"  succeeded: {len(ids)}/{N}, errors: {len(errors)}")
    for e in errors:
        print(f"    {type(e).__name__}: {e}")
    assert not errors, f"{len(errors)} concurrent stores to a new project failed"
    assert len(ids) == N
    assert len(set(ids)) == N, "duplicate ids returned"

    stats = store.stats(project)
    assert stats["total_entries"] == N, (
        f"expected {N} entries persisted, got {stats['total_entries']}"
    )


def test_shared_client_under_load(store: VectorStore) -> None:
    print(f"== {N * 3} concurrent memory_store calls on an existing collection ==")
    project = f"{PROJECT}-existing-collection"
    reset_project(project)
    store.store(project, area="a", module="m", content="seed", summary="seed", importance=1)

    load = N * 3

    def worker(i: int) -> str:
        return store.store(
            project, area="a", module="m",
            content=f"load entry {i}", summary=f"load {i}", importance=3,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=load) as ex:
        futures = [ex.submit(worker, i) for i in range(load)]
        ids, errors = [], []
        for f in concurrent.futures.as_completed(futures):
            try:
                ids.append(f.result())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    print(f"  succeeded: {len(ids)}/{load}, errors: {len(errors)}")
    for e in errors[:5]:
        print(f"    {type(e).__name__}: {e}")
    assert not errors, (
        f"{len(errors)} concurrent stores failed under load -- possible shared "
        "QdrantClient thread-safety regression"
    )
    assert len(set(ids)) == load

    stats = store.stats(project)
    assert stats["total_entries"] == load + 1


def test_delete_toctou_race(store: VectorStore) -> None:
    print(f"== {N} concurrent memory_delete calls for the SAME point id ==")
    project = f"{PROJECT}-delete-race"
    reset_project(project)
    point_id = store.store(
        project, area="a", module="m", content="entry to delete", summary="s", importance=3
    )

    def worker(_: int) -> bool:
        return store.delete(project, point_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(worker, range(N)))

    true_count = sum(1 for r in results if r is True)
    false_count = sum(1 for r in results if r is False)
    print(f"  True: {true_count}, False: {false_count} (expected exactly 1 True)")
    assert true_count == 1, (
        f"expected exactly 1 caller to get True (it actually deleted the point), "
        f"got {true_count} -- delete() is not correctly serialized"
    )
    assert false_count == N - 1


def test_mixed_concurrent_workload(store: VectorStore) -> None:
    print("== mixed store/search/stats/manifest firing concurrently ==")
    project = f"{PROJECT}-mixed"
    reset_project(project)
    store.store(project, area="a", module="m", content="seed entry", summary="seed", importance=3)

    def do_store(i: int) -> None:
        store.store(project, area="a", module="m", content=f"e{i}", summary=f"s{i}", importance=3)

    def do_search(_: int) -> None:
        store.search(project, "seed", top_k=5)

    def do_stats(_: int) -> None:
        store.stats(project)

    def do_manifest(_: int) -> None:
        store.manifest(project)

    n_stores = 15
    ops = [do_store] * n_stores + [do_search] * 10 + [do_stats] * 10 + [do_manifest] * 10
    random.shuffle(ops)

    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(op, i) for i, op in enumerate(ops)]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

    print(f"  {len(ops)} mixed ops, errors: {len(errors)}")
    for e in errors[:5]:
        print(f"    {e}")
    assert not errors, f"{len(errors)} errors under mixed concurrent workload"

    final_stats = store.stats(project)
    expected = 1 + n_stores
    assert final_stats["total_entries"] == expected, (
        f"expected {expected} entries, got {final_stats['total_entries']}"
    )


def main() -> None:
    store = VectorStore(create_embedder())

    test_new_collection_race(store)
    test_shared_client_under_load(store)
    test_delete_toctou_race(store)
    test_mixed_concurrent_workload(store)

    print("\nCONCURRENCY TEST PASSED")


if __name__ == "__main__":
    main()