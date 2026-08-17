"""Qdrant-backed vector store.

One collection per project. Payload carries ``area``, ``module``,
``importance``, ``timestamp``, ``summary``, ``content``, and arbitrary
metadata. Filters on area/module/importance/time scope down searches so that a
large (XL) project collection is not scanned wholesale.
"""

import re
import threading
import time
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from .config import (
    DEFAULT_COLLECTION_PREFIX,
    DEFAULT_HNSW_EF_CONSTRUCT,
    DEFAULT_HNSW_M,
    DEFAULT_QDRANT_URL,
    MAX_CONTENT_CHARS,
    MAX_TOP_K,
)
from .embeddings import Embedder

PAYLOAD_INDEX_FIELDS = ["area", "module", "importance", "timestamp"]


_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


def _suggest_slug(project: str) -> str:
    slug = project.strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "project"


def collection_name(project: str) -> str:
    """Map a project name to its Qdrant collection name.

    Strict, not normalizing: different callers (agents/tools) each guess a
    project's name independently, so silently folding "Team A", "team/a",
    and "TEAM-A" into the same collection would let unrelated callers'
    memory quietly mix with zero warning. Rejecting anything outside the
    canonical form forces every caller onto the one string that maps to a
    given project, which is what actually prevents the collision -- not a
    smarter normalization function.
    """
    if not _PROJECT_NAME_RE.match(project):
        raise ValueError(
            f"invalid project name {project!r}: allowed: lowercase letters, "
            f"digits, '-', '_', starting with a letter/digit, max 100 chars. "
            f"did you mean {_suggest_slug(project)!r}?"
        )
    return f"{DEFAULT_COLLECTION_PREFIX}_{project}"


def clamp_top_k(top_k: int) -> int:
    """Keep a caller-supplied top_k within [1, MAX_TOP_K].

    This is a shared server multiple agents query concurrently; an absurd
    top_k (accidental or not) would force a wide-open Qdrant scan on
    everyone else's behalf.
    """
    return max(1, min(int(top_k), MAX_TOP_K))


class VectorStore:
    """Thread-safe wrapper around QdrantClient.

    A single shared ``QdrantClient`` instance handling concurrent requests
    from multiple threads intermittently raises
    ``ResponseHandlingException: [Errno 9] Bad file descriptor`` -- its
    underlying HTTP connection pool is not safe to use concurrently across
    threads. This matters here: multiple agents (or multiple team members)
    can legitimately call ``memory_store``/``memory_search`` on the same
    project at the same time, and the MCP server dispatches tool calls to a
    thread pool, so this is not a hypothetical edge case.

    We give each thread its own ``QdrantClient`` (via ``threading.local``)
    instead of sharing one. Construction is cheap (a couple of ms, no
    network handshake), so this has no meaningful performance cost.
    """

    def __init__(
        self,
        embedder: Embedder,
        url: str | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self._embedder = embedder
        self._url = url or DEFAULT_QDRANT_URL
        self._api_key = api_key
        self._local = threading.local()
        # Striped locks for delete()'s check-then-delete section: without
        # this, concurrent deletes of the SAME point_id (e.g. two agents
        # both reacting to the same stale entry) each independently see it
        # exists and each report True, when at most one deletion actually
        # happens. A fixed-size stripe (hashed by point_id) avoids an
        # unbounded lock dict while still serializing same-key deletes.
        self._delete_locks = [threading.Lock() for _ in range(256)]

    @property
    def _client(self) -> QdrantClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = QdrantClient(url=self._url, api_key=self._api_key)
            self._local.client = client
        return client

    def _ensure_collection(self, project: str) -> None:
        name = collection_name(project)
        existing = self._client.get_collections().collections
        if any(c.name == name for c in existing):
            return
        try:
            self._client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=self._embedder.dimension,
                    distance=qmodels.Distance.COSINE,
                ),
                hnsw_config=qmodels.HnswConfigDiff(
                    m=DEFAULT_HNSW_M,
                    ef_construct=DEFAULT_HNSW_EF_CONSTRUCT,
                ),
            )
        except UnexpectedResponse as e:
            # Two concurrent callers can both pass the existence check above
            # and race to create the same brand-new project's collection.
            # Qdrant returns 409 for the loser -- that's fine, the collection
            # exists either way, so treat it as success rather than raising.
            if e.status_code != 409:
                raise
        # Idempotent: safe to call even if another caller already created
        # these indexes (or is doing so concurrently).
        self._create_payload_indexes(name)

    def _create_payload_indexes(self, name: str) -> None:
        schema: list[tuple[str, qmodels.PayloadSchemaType]] = [
            ("importance", qmodels.PayloadSchemaType.INTEGER),
            ("timestamp", qmodels.PayloadSchemaType.INTEGER),
            ("area", qmodels.PayloadSchemaType.KEYWORD),
            ("module", qmodels.PayloadSchemaType.KEYWORD),
        ]
        for field, dtype in schema:
            self._client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=dtype,
            )

    def _prepare_payload(
        self,
        area: str,
        module: str,
        importance: int,
        content: str,
        summary: str,
        extra_metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "area": area,
            "module": module,
            "importance": importance,
            "timestamp": int(time.time()),
            "content": content,
            "summary": summary,
        }
        if extra_metadata:
            for k, v in extra_metadata.items():
                if k not in payload:
                    payload[k] = v
        return payload

    def store(
        self,
        project: str,
        area: str,
        module: str,
        content: str,
        summary: str,
        importance: int,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        importance = max(1, min(5, int(importance)))
        if len(content) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"content is {len(content)} chars, over the {MAX_CONTENT_CHARS}-char "
                "limit (BLUEOCEAN_MAX_CONTENT_CHARS) -- shorten it or split it into "
                "multiple entries"
            )
        self._ensure_collection(project)
        point_id = str(uuid.uuid4())
        payload = self._prepare_payload(
            area, module, importance, content, summary, metadata
        )
        vector = self._embedder.embed_documents([content])[0]
        self._client.upsert(
            collection_name=collection_name(project),
            points=[
                qmodels.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
        return point_id

    def _scope_filter(
        self,
        area: str | None,
        module: str | None,
        time_range: tuple[int, int] | None,
        importance_min: int | None,
    ) -> qmodels.Filter | None:
        conditions: list[qmodels.FieldCondition] = []
        if area:
            conditions.append(
                qmodels.FieldCondition(key="area", match=qmodels.MatchValue(value=area))
            )
        if module:
            conditions.append(
                qmodels.FieldCondition(
                    key="module", match=qmodels.MatchValue(value=module)
                )
            )
        if importance_min is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="importance",
                    range=qmodels.Range(gte=importance_min),
                )
            )
        if time_range is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="timestamp",
                    range=qmodels.Range(gte=time_range[0], lte=time_range[1]),
                )
            )
        if not conditions:
            return None
        return qmodels.Filter(must=conditions)

    def search(
        self,
        project: str,
        query: str,
        *,
        top_k: int,
        area: str | None = None,
        module: str | None = None,
        time_range: tuple[int, int] | None = None,
        importance_min: int | None = None,
    ) -> list[dict]:
        self._ensure_collection(project)
        vector = self._embedder.embed_query(query)
        # .search() was removed in qdrant-client 1.19; .query_points() is the
        # replacement (returns a QueryResponse wrapping .points).
        response = self._client.query_points(
            collection_name=collection_name(project),
            query=vector,
            limit=clamp_top_k(top_k),
            query_filter=self._scope_filter(
                area, module, time_range, importance_min
            ),
        )
        hits = response.points
        return [
            {
                "id": hit.id,
                "score": hit.score,
                "content": hit.payload.get("content", ""),
                "summary": hit.payload.get("summary", ""),
                "metadata": {
                    k: v
                    for k, v in hit.payload.items()
                    if k not in ("content", "summary")
                },
            }
            for hit in hits
        ]

    def get(self, project: str, point_id: str) -> dict | None:
        self._ensure_collection(project)
        points = self._client.retrieve(
            collection_name=collection_name(project),
            ids=[point_id],
        )
        if not points:
            return None
        p = points[0].payload
        return {
            "id": point_id,
            "content": p.get("content", ""),
            "summary": p.get("summary", ""),
            "metadata": {k: v for k, v in p.items() if k not in ("content", "summary")},
        }

    def delete(self, project: str, point_id: str) -> bool:
        """Delete a point by ID. Returns True only for the one caller (if
        any) whose delete actually removed it.

        Qdrant's delete API reports COMPLETED/ACKNOWLEDGED even for IDs that
        never existed, so we check existence first to give the caller an
        accurate signal. That check-then-delete is only atomic against other
        callers *in this process* because it's guarded by an in-process lock
        (see ``_delete_locks``) -- correct for the recommended deployment
        (one shared server process for all agents/team members), not for
        multiple separate stdio server processes deleting the same ID at
        the exact same instant.
        """
        self._ensure_collection(project)
        name = collection_name(project)
        lock = self._delete_locks[hash(point_id) % len(self._delete_locks)]
        with lock:
            existed = bool(self._client.retrieve(collection_name=name, ids=[point_id]))
            if not existed:
                return False
            self._client.delete(collection_name=name, points_selector=[point_id])
            return True

    def manifest(self, project: str) -> dict:
        """Group area/module values present in a project collection."""
        self._ensure_collection(project)
        client = self._client
        name = collection_name(project)
        counts: dict[str, dict] = {}
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
                a = point.payload.get("area")
                m = point.payload.get("module")
                if a is None:
                    continue
                area_entry = counts.setdefault(str(a), {"modules": set(), "count": 0})
                if m is not None:
                    area_entry["modules"].add(str(m))
                area_entry["count"] += 1
            if offset is None:
                break
        return {
            "project": project,
            "areas": {
                area: {"count": v["count"], "modules": sorted(v["modules"])}
                for area, v in sorted(counts.items())
            },
        }

    def stats(self, project: str) -> dict:
        self._ensure_collection(project)
        name = collection_name(project)
        info = self._client.get_collection(name)
        total = info.points_count or 0
        importance: dict[str, int] = {}
        areas: dict[str, int] = {}
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=name,
                with_payload=True,
                with_vectors=False,
                limit=1000,
                offset=offset,
            )
            for point in points:
                imp = point.payload.get("importance")
                area = point.payload.get("area")
                if imp is not None:
                    importance[str(imp)] = importance.get(str(imp), 0) + 1
                if area is not None:
                    areas[str(area)] = areas.get(str(area), 0) + 1
            if offset is None:
                break
        return {
            "project": project,
            "total_entries": total,
            "importance_distribution": dict(sorted(importance.items())),
            "area_distribution": areas,
            "status": info.status,
        }

    def list_projects(self) -> list[str]:
        prefix = f"{DEFAULT_COLLECTION_PREFIX}_"
        collections = self._client.get_collections().collections
        return [
            c.name[len(prefix) :]
            for c in collections
            if c.name.startswith(prefix)
        ]