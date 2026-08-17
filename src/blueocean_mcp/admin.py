"""Admin CLI for the blueocean vector store.

Commands:
    stats <project>            - collection stats
    manifest <project>         - list areas/modules
    list                      - list projects
    export <project>           - dump all entries as JSON lines
    prune <project> [--older-days N] [--max-importance I] [--dry-run]
                               - delete low-importance + old entries
    snapshot <project> [--out DIR]
                               - point-in-time backup (vectors included),
                                 downloaded to local disk
    restore <project> <file> --yes
                               - restore a project's collection from a
                                 snapshot file (overwrites current data)
    generate-token [--write-env] - generate a bearer token for streamable-http auth
"""

import argparse
import json
import os
import time

import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .auth import generate_token
from .config import DEFAULT_QDRANT_URL
from .embeddings import create_embedder
from .vector_store import VectorStore, collection_name


def _qdrant_url() -> str:
    return os.getenv("BLUEOCEAN_QDRANT_URL", DEFAULT_QDRANT_URL).rstrip("/")


def _client() -> QdrantClient:
    return QdrantClient(
        url=_qdrant_url(),
        api_key=os.getenv("BLUEOCEAN_QDRANT_API_KEY"),
    )


def cmd_stats(args: argparse.Namespace) -> None:
    store = VectorStore(create_embedder())
    print(json.dumps(store.stats(args.project), indent=2, ensure_ascii=False))


def cmd_manifest(args: argparse.Namespace) -> None:
    store = VectorStore(create_embedder())
    print(json.dumps(store.manifest(args.project), indent=2, ensure_ascii=False))


def cmd_list(_args: argparse.Namespace) -> None:
    store = VectorStore(create_embedder())
    for p in store.list_projects():
        print(p)


def cmd_export(args: argparse.Namespace) -> None:
    client = _client()
    name = collection_name(args.project)
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name,
            with_payload=True,
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for point in points:
            print(json.dumps({"id": point.id, **point.payload}, ensure_ascii=False))
        if offset is None:
            break


def cmd_prune(args: argparse.Namespace) -> None:
    client = _client()
    name = collection_name(args.project)
    now = int(time.time())
    max_age = args.older_days * 86400 if args.older_days is not None else None
    max_importance = args.max_importance

    must: list = []
    if max_age is not None:
        must.append(
            qmodels.FieldCondition(
                key="timestamp",
                range=qmodels.Range(lt=now - max_age),
            )
        )
    if max_importance is not None:
        must.append(
            qmodels.FieldCondition(
                key="importance",
                range=qmodels.Range(lte=max_importance),
            )
        )
    if not must:
        print("Nothing to prune: provide --older-days and/or --max-importance.")
        return

    flt = qmodels.Filter(must=must) if must else None
    offset = None
    to_delete: list[str] = []
    while True:
        points, offset = client.scroll(
            collection_name=name,
            with_payload=False,
            with_vectors=False,
            limit=1000,
            offset=offset,
            scroll_filter=flt,
        )
        to_delete.extend(str(p.id) for p in points)
        if offset is None:
            break

    print(f"Pruning {len(to_delete)} entries (dry-run)" if args.dry_run
          else f"Pruning {len(to_delete)} entries")
    if not args.dry_run and to_delete:
        client.delete(
            collection_name=name,
            points_selector=to_delete,
        )


def cmd_snapshot(args: argparse.Namespace) -> None:
    """Point-in-time backup: Qdrant's own native snapshot (vectors, payload,
    and index state, not just a payload dump), downloaded to local disk.

    The server-side copy is deleted once the local copy is confirmed intact
    -- keeping backups only inside the same Qdrant volume they're backing up
    out of defeats the point of a backup.
    """
    client = _client()
    name = collection_name(args.project)  # raises ValueError for a bad project name

    desc = client.create_snapshot(collection_name=name)
    if desc is None:
        print(f"Snapshot creation failed for project {args.project!r} (no snapshot returned).")
        return

    headers = {}
    api_key = os.getenv("BLUEOCEAN_QDRANT_API_KEY")
    if api_key:
        headers["api-key"] = api_key
    # qdrant-client's own get_snapshot() wrapper tries to JSON-decode the
    # response and crashes on binary content -- download it directly instead.
    url = f"{_qdrant_url()}/collections/{name}/snapshots/{desc.name}"
    response = httpx.get(url, headers=headers, timeout=120.0)
    response.raise_for_status()
    content = response.content

    if desc.size is not None and len(content) != desc.size:
        print(
            f"Downloaded snapshot is {len(content)} bytes but the server reported "
            f"{desc.size} -- not deleting the server-side copy, not trusting this backup."
        )
        return

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
    out_path = os.path.join(out_dir, f"{args.project}_{timestamp}.snapshot")
    with open(out_path, "wb") as f:
        f.write(content)

    client.delete_snapshot(collection_name=name, snapshot_name=desc.name)
    print(f"Wrote {out_path} ({len(content)} bytes)")


def cmd_restore(args: argparse.Namespace) -> None:
    """Restore a project's collection from a local snapshot file.

    Overwrites whatever is currently in that project's collection (creates
    it if it doesn't exist), so this requires --yes.
    """
    if not args.yes:
        print(
            f"This will overwrite all current data in project {args.project!r}. "
            "Re-run with --yes to proceed."
        )
        return

    client = _client()
    name = collection_name(args.project)
    with open(args.snapshot_file, "rb") as f:
        client.http.snapshots_api.recover_from_uploaded_snapshot(
            collection_name=name,
            snapshot=f,
        )
    info = client.get_collection(name)
    print(f"Restored project {args.project!r}: {info.points_count} points.")

    # recover_from_uploaded_snapshot() registers the uploaded file as a
    # server-side snapshot too, as a side effect of restoring it -- left
    # alone, every restore leaves a permanent orphaned blob in the same
    # volume, which is exactly the accumulation `snapshot` is careful to
    # avoid. Anything listed here right after a restore is that leftover,
    # not a legitimate snapshot someone else is relying on.
    for leftover in client.list_snapshots(collection_name=name):
        try:
            client.delete_snapshot(collection_name=name, snapshot_name=leftover.name)
        except Exception as e:
            print(f"Warning: could not clean up leftover snapshot {leftover.name!r}: {e}")


def cmd_generate_token(args: argparse.Namespace) -> None:
    token = generate_token()
    if not args.write_env:
        print(token)
        return

    env_path = args.env_file
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    key = "BLUEOCEAN_AUTH_TOKEN"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == f"# {key}=":
            lines[i] = f"{key}={token}\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={token}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)
    print(f"Wrote {key} to {env_path}")
    print(f"Token: {token}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="blueocean-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("project")
    p_stats.set_defaults(func=cmd_stats)

    p_manifest = sub.add_parser("manifest")
    p_manifest.add_argument("project")
    p_manifest.set_defaults(func=cmd_manifest)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_export = sub.add_parser("export")
    p_export.add_argument("project")
    p_export.set_defaults(func=cmd_export)

    p_prune = sub.add_parser("prune")
    p_prune.add_argument("project")
    p_prune.add_argument("--older-days", type=int, default=None)
    p_prune.add_argument("--max-importance", type=int, default=None)
    p_prune.add_argument("--dry-run", action="store_true")
    p_prune.set_defaults(func=cmd_prune)

    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("project")
    p_snapshot.add_argument(
        "--out", default="./backups", help="Directory to write the snapshot file to (default: ./backups)"
    )
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("project")
    p_restore.add_argument("snapshot_file")
    p_restore.add_argument(
        "--yes", action="store_true", help="Confirm overwriting the project's current data"
    )
    p_restore.set_defaults(func=cmd_restore)

    p_token = sub.add_parser("generate-token")
    p_token.add_argument(
        "--write-env",
        action="store_true",
        help="Write BLUEOCEAN_AUTH_TOKEN into an env file instead of just printing it",
    )
    p_token.add_argument("--env-file", default=".env", help="Path to the env file (default: .env)")
    p_token.set_defaults(func=cmd_generate_token)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()