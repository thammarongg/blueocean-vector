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
import sys
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


def _report_audit(row: dict) -> bool:
    """Send an audit row to the server, returning whether it was recorded.

    If the server is not reachable we do NOT write the file: that would be a
    silent fallback to the host's default path while the real database lives
    at the container's, producing a record nobody ever reads. Print it instead
    so the operator still has it.

    The caller is expected to exit non-zero on a False. A nightly prune that
    exits 0 would never reveal that its audit trail has a hole in it.
    """
    from .telemetry import client

    try:
        client.post_audit(row, token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None)
    except client.ServerUnavailable as e:
        print(f"warning: audit row not recorded ({e})", file=sys.stderr)
        print(json.dumps({"unrecorded_audit": row}), file=sys.stderr)
        return False
    return True


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
    if not args.dry_run and not _report_audit(
        {"tool": "prune", "project": args.project, "deleted_count": len(to_delete)}
    ):
        # The prune above already happened and stays done: an unreachable
        # telemetry server must not be able to block a destructive operation
        # that has nothing to do with it. Only the exit code carries the fault.
        raise SystemExit(1)


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
    recorded = _report_audit(
        {"tool": "restore", "project": args.project, "deleted_count": info.points_count}
    )

    # recover_from_uploaded_snapshot() registers the uploaded file as a
    # server-side snapshot too, as a side effect of restoring it -- left
    # alone, every restore leaves a permanent orphaned blob in the same
    # volume, which is exactly the accumulation `snapshot` is careful to
    # avoid. Anything listed here right after a restore is that leftover,
    # not a legitimate snapshot someone else is relying on.
    for leftover in client.list_snapshots(collection_name=name):
        try:
            client.delete_snapshot(collection_name=name, snapshot_name=leftover.name)
        # Best-effort sweep on purpose: whatever Qdrant raises here (network
        # blip, snapshot already gone, permission quirk) must not turn a
        # successful restore into a failure, and listing every exception type
        # qdrant-client can throw would couple us to its internals. A warning
        # is the honest response; the orphan costs disk, not correctness.
        except Exception as e:  # noqa: BLE001 - intentional best-effort cleanup
            print(f"Warning: could not clean up leftover snapshot {leftover.name!r}: {e}")

    # Raised only after the cleanup above: an unrecorded audit row must not
    # cost us the orphaned-blob sweep that every restore depends on.
    if not recorded:
        raise SystemExit(1)


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


def _local_tz_offset_minutes() -> int:
    return -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone) // 60


def _print_table(title: str, rows: list[dict], columns: list[str]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print("  " + "  ".join(str(row.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def cmd_refresh_prices(args: argparse.Namespace) -> None:
    """Fetch embedding prices from OpenRouter and hand them to the server.

    This is the only command that sends anything off this machine. The server
    deliberately never calls out on its own: an automatic refresh would make
    an outbound request nobody asked for and add a hidden network dependency
    to a dashboard meant to work air-gapped.
    """
    from .telemetry import client, pricing

    if os.getenv("BLUEOCEAN_OFFLINE_TEST") == "1":
        prices = {"openai/text-embedding-3-small": 0.02}
    else:
        prices = pricing.fetch_openrouter()
    if not prices:
        print("OpenRouter returned no usable prices", file=sys.stderr)
        raise SystemExit(1)

    if args.db:
        pricing.write_file(None, prices)
        print(f"Wrote {len(prices)} model prices to the local pricing file")
        return

    try:
        client.post_prices(prices, token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None)
    except client.ServerUnavailable as e:
        print(
            f"{e}\nPrices were fetched but not stored. Start the server, or pass "
            "--db to write the local pricing file directly (development only).",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    flagged = [m for m in prices if pricing.retains_data(m)]
    print(f"Sent {len(prices)} model prices to the server")
    if flagged:
        print(
            f"note: {len(flagged)} of these are OpenRouter ':free' models, which may "
            "retain requests and embeddings for training"
        )


def cmd_usage(args: argparse.Namespace) -> None:
    from .telemetry import client

    if args.refresh_prices:
        cmd_refresh_prices(args)
        return

    tz_offset = _local_tz_offset_minutes()
    if args.db:
        # Explicit opt-in: the caller is telling us there is no server, which
        # is true for stdio-only development. Never reached by accident.
        from .telemetry import db as telemetry_db
        from .telemetry.queries import build_stats

        conn = telemetry_db.connect(args.db)
        try:
            stats = build_stats(conn, days=args.days, tz_offset_minutes=tz_offset,
                                project=args.project)
        finally:
            conn.close()
    else:
        try:
            stats = client.fetch_stats(
                days=args.days,
                tz_offset_minutes=tz_offset,
                project=args.project,
                token=os.getenv("BLUEOCEAN_AUTH_TOKEN") or None,
            )
        except client.ServerUnavailable as e:
            print(
                f"{e}\nStart the server, or pass --db <path> to read a local "
                "telemetry file directly (development only).",
                file=sys.stderr,
            )
            raise SystemExit(1) from e

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    if args.audit:
        _print_table("Audit", stats["audit"], ["ts", "tool", "project", "deleted_count", "origin"])
        return
    if args.unused:
        _print_table(
            "Never retrieved (since first observed)",
            stats["unused"], ["project", "point_id", "hits", "full_hits", "last_seen_at"],
        )
        return

    tiles = stats["tiles"]
    print(f"Window: last {stats['window']['days']} day(s)")
    print(
        f"  calls={tiles['calls']}  errors={tiles['errors']}  "
        f"p95={tiles['p95_ms']}ms  cost=${tiles['est_cost_usd']:.6f}  "
        f"unpriced={stats['unpriced_calls']}"
    )
    key = {"tool": "tool", "agent": "agent_name", "project": "project", "day": "day"}[args.by]
    source = {"tool": stats["tools"], "agent": stats["agents"],
              "project": stats["projects"], "day": stats["daily"]}[args.by]
    columns = [key, "calls"] + (["p50_ms", "p95_ms", "errors"] if args.by == "tool" else [])
    _print_table(f"By {args.by}", source, columns)


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

    p_usage = sub.add_parser("usage")
    p_usage.add_argument("--project", default=None,
                         help="Scope to one project (default: every project)")
    p_usage.add_argument("--days", type=int, default=7)
    p_usage.add_argument("--by", choices=["tool", "agent", "project", "day"], default="tool")
    p_usage.add_argument("--audit", action="store_true", help="Show the audit trail instead")
    p_usage.add_argument("--unused", action="store_true",
                         help="Show entries never retrieved, since first observed")
    p_usage.add_argument("--json", action="store_true")
    p_usage.add_argument("--db", default=None,
                         help="Read this telemetry file directly instead of asking the "
                              "server. Development only: the server must not be running.")
    p_usage.add_argument("--refresh-prices", action="store_true",
                         help="Fetch embedding prices from OpenRouter and hand them to the server")
    p_usage.set_defaults(func=cmd_usage)

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