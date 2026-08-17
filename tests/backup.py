"""Real disaster-recovery simulation for `blueocean-admin snapshot`/`restore`.

Stores data, snapshots it, deletes the collection entirely (simulating a
lost/corrupted local Qdrant), restores from the downloaded snapshot file,
and asserts the content survived -- not just that the commands ran without
raising.

Run with:
    uv run python -m tests.backup
"""

import os
import tempfile

from qdrant_client import QdrantClient

from blueocean_mcp.admin import cmd_restore, cmd_snapshot
from blueocean_mcp.config import DEFAULT_QDRANT_URL
from blueocean_mcp.embeddings import create_embedder
from blueocean_mcp.vector_store import VectorStore, collection_name

from ._helpers import reset_project

PROJECT = "backup-test-project"


class _Args:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def main() -> None:
    reset_project(PROJECT)
    store = VectorStore(create_embedder())
    point_id = store.store(
        PROJECT,
        area="a",
        module="m",
        content="survive the disaster",
        summary="s",
        importance=5,
    )
    print(f"== stored {point_id} ==")

    client = QdrantClient(url=DEFAULT_QDRANT_URL)
    name = collection_name(PROJECT)

    with tempfile.TemporaryDirectory() as tmpdir:
        print("== snapshot ==")
        cmd_snapshot(_Args(project=PROJECT, out=tmpdir))
        files = os.listdir(tmpdir)
        assert len(files) == 1, f"expected exactly one snapshot file, got {files}"
        snapshot_path = os.path.join(tmpdir, files[0])
        size = os.path.getsize(snapshot_path)
        assert size > 0, "snapshot file is empty"
        print(f"  wrote {snapshot_path} ({size} bytes)")

        print("== server-side snapshot copy was cleaned up after download ==")
        remaining = client.list_snapshots(collection_name=name)
        assert remaining == [], f"expected no server-side snapshots left, got {remaining}"

        print("== simulating disaster: delete the collection entirely ==")
        client.delete_collection(name)

        print("== restore refuses to run without --yes ==")
        cmd_restore(_Args(project=PROJECT, snapshot_file=snapshot_path, yes=False))
        assert not client.collection_exists(name), (
            "restore without --yes should not have touched anything"
        )
        print("  correctly refused")

        print("== restore ==")
        cmd_restore(_Args(project=PROJECT, snapshot_file=snapshot_path, yes=True))

        print("== restore doesn't leave its own orphaned snapshot behind either ==")
        # recover_from_uploaded_snapshot() registers the uploaded file as a
        # server-side snapshot as a side effect of restoring it -- that needs
        # cleaning up too, or every restore leaves a permanent orphaned blob
        # in the same volume, undermining the whole point of this feature.
        remaining_after_restore = client.list_snapshots(collection_name=name)
        assert remaining_after_restore == [], (
            f"expected no server-side snapshots left after restore, got {remaining_after_restore}"
        )
        print("  clean")

    print("== data survived the delete -> restore cycle ==")
    restored = store.get(PROJECT, point_id)
    assert restored is not None, "restored entry not found"
    assert restored["content"] == "survive the disaster"
    print(f"  {point_id} content intact")

    print("\nBACKUP/RESTORE TEST PASSED")


if __name__ == "__main__":
    main()
