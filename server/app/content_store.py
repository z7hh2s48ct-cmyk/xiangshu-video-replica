"""Content-addressed storage: one copy of the bytes, many owner-scoped views.

Why this module exists
----------------------
``assets`` is a *view* table — one row per (owner, project, purpose) the
business logic cares about.  The bytes behind those rows used to be private to
each row, so the same viral video imported into ten projects occupied ten
times the space.  ``content_objects`` is the registry mapping a content hash to
the one physical object holding it; ``assets.content_object_id`` points at it.

Two rules make sharing safe, and both are load-bearing:

1. **Ownership is part of the identity.**  ``scope='user'`` rows are keyed by
   the owning user as well as the hash, so one customer's upload can never
   satisfy another customer's lookup.  Only ``scope='global'`` content — media
   whose bytes are public by construction, currently platform-collected viral
   videos — is shared across users.

2. **Deletion is decided by ``ref_count``, never by comparing ``storage_uri``.**
   String comparison cannot see a reference whose ``project_id`` is NULL
   (``NULL <> 'p'`` evaluates to NULL, not TRUE), which silently deletes
   objects that are still in use.  Counting cannot fail that way.  The legacy
   comparison is still available, in NULL-safe form, for rows that predate this
   registry — see :func:`object_referenced_by_another_asset`.

Reclaim is deliberately two-phase.  Dropping ``ref_count`` to zero only
*schedules* removal (``reclaim_after``); the bytes go away in
:func:`reclaim_expired_content_objects`, which re-checks the count under a lock
so a reference created during the grace window is never destroyed.

3. **No business module calls ``storage.delete_object``.**  Sixteen call sites
   each re-derived the "is anyone else using this?" rule, and one of them got it
   wrong.  Deletion now goes through :func:`delete_object_outside_content_namespace`
   (no connection available) or :func:`delete_object_if_unreferenced` (connection
   available), and ``tests/test_delete_object_gate.py`` fails the build if a new
   direct call appears.  The rule is only worth stating if something enforces it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from app.db_portable import BusinessConnection
from app.storage import (
    StorageAdapter,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

ContentScope = Literal["user", "global"]

OBJECT_PREFIX = "content"
CONTENT_SCOPES: tuple[ContentScope, ...] = ("user", "global")

# The grace window between "nothing references this" and "delete the bytes".
# Long enough that a delete-then-immediately-recreate flow reuses the object
# instead of re-uploading it, short enough that abandoned content does not
# linger.  Overridable per call for tests.
RECLAIM_DELAY_HOURS = 24


class SqlConnection(Protocol):
    """The slice of a connection the read-only guards need.

    ``params`` is ``Any`` rather than ``Sequence[object]`` on purpose: parameter
    types are contravariant, so a narrower annotation here would stop
    ``BusinessConnection`` from satisfying the protocol.  Both it and the raw
    ``psycopg`` connection used by the upload-reclamation CLI conform as written.
    """

    def execute(self, sql: str, params: Any = ...) -> Any: ...


@dataclass(frozen=True)
class ContentObject:
    id: str
    sha256: str
    size_bytes: int
    content_type: str | None
    provider: str
    bucket: str
    object_key: str
    scope: str
    scope_owner: str | None
    ref_count: int
    pinned: bool = False
    reclaim_after: datetime | None = None

    @property
    def storage_uri(self) -> str:
        return f"{self.provider}://{self.bucket}/{self.object_key}"


def content_object_key(sha256: str, suffix: str) -> str:
    """Two-level fan-out keeps any single listing directory small."""
    if len(sha256) < 4:
        raise ValueError("sha256 digest is too short to shard")
    normalised = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
    return f"{OBJECT_PREFIX}/{sha256[:2]}/{sha256[2:4]}/{sha256}{normalised}"


def _to_content_object(row: Any) -> ContentObject:
    return ContentObject(
        id=str(row["id"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        content_type=None if row["content_type"] is None else str(row["content_type"]),
        provider=str(row["provider"]),
        bucket=str(row["bucket"]),
        object_key=str(row["object_key"]),
        scope=str(row["scope"]),
        scope_owner=None if row["scope_owner"] is None else str(row["scope_owner"]),
        ref_count=int(row["ref_count"]),
        pinned=bool(row["pinned"]),
        reclaim_after=(
            None if row["reclaim_after"] is None else cast(datetime, row["reclaim_after"])
        ),
    )


def _owner_parameter(scope: ContentScope, owner_user_id: str | None) -> str | None:
    if scope == "global":
        return None
    if not owner_user_id:
        raise ValueError("scope='user' requires an owner_user_id")
    return owner_user_id


def find_content_object(
    conn: BusinessConnection,
    *,
    sha256: str,
    size_bytes: int,
    provider: str,
    bucket: str,
    scope: ContentScope,
    owner_user_id: str | None = None,
    for_update: bool = False,
) -> ContentObject | None:
    """Look up the holder of these exact bytes for this scope.

    ``scope='user'`` additionally matches on the owner, which is what stops one
    customer from being served another customer's row.
    """
    owner = _owner_parameter(scope, owner_user_id)
    lock = " FOR UPDATE" if for_update else ""
    if scope == "user":
        row = conn.execute(
            """
            SELECT * FROM content_objects
            WHERE sha256 = %s AND size_bytes = %s
              AND provider = %s AND bucket = %s
              AND scope = 'user' AND scope_owner = %s
            LIMIT 1
            """
            + lock,
            (sha256, size_bytes, provider, bucket, owner),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM content_objects
            WHERE sha256 = %s AND size_bytes = %s
              AND provider = %s AND bucket = %s
              AND scope = 'global'
            LIMIT 1
            """
            + lock,
            (sha256, size_bytes, provider, bucket),
        ).fetchone()
    return None if row is None else _to_content_object(row)


def find_content_object_by_id(
    conn: BusinessConnection,
    content_object_id: str,
    *,
    for_update: bool = False,
) -> ContentObject | None:
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(
        "SELECT * FROM content_objects WHERE id = %s" + lock,
        (content_object_id,),
    ).fetchone()
    return None if row is None else _to_content_object(row)


def register_content_object(
    conn: BusinessConnection,
    *,
    sha256: str,
    size_bytes: int,
    content_type: str | None,
    provider: str,
    bucket: str,
    object_key: str,
    scope: ContentScope,
    owner_user_id: str | None = None,
    pinned: bool = False,
) -> ContentObject:
    """Insert the registry row for an object, returning the winner.

    A concurrent insert wins the unique index and this call returns that row
    instead, so the caller must tolerate getting back a different ``id`` and
    ``object_key`` than it passed in.
    """
    owner = _owner_parameter(scope, owner_user_id)
    candidate_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO content_objects (
            id, sha256, size_bytes, content_type, provider, bucket,
            object_key, scope, scope_owner, ref_count, pinned
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            candidate_id,
            sha256,
            size_bytes,
            content_type,
            provider,
            bucket,
            object_key,
            scope,
            owner,
            pinned,
        ),
    )
    existing = find_content_object(
        conn,
        sha256=sha256,
        size_bytes=size_bytes,
        provider=provider,
        bucket=bucket,
        scope=scope,
        owner_user_id=owner_user_id,
        for_update=True,
    )
    if existing is not None:
        return existing
    row = conn.execute(
        "SELECT * FROM content_objects WHERE id = %s",
        (candidate_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - only reachable on a lost race
        raise RuntimeError("content object registration lost both insert paths")
    return _to_content_object(row)


def retain_content_object(
    conn: BusinessConnection,
    *,
    sha256: str,
    size_bytes: int,
    content_type: str | None,
    provider: str,
    bucket: str,
    object_key: str,
    scope: ContentScope,
    owner_user_id: str | None = None,
    pinned: bool = False,
) -> tuple[ContentObject, bool]:
    """Resolve (or create) the registry row for these bytes and take a reference.

    Returns ``(content, deduplicated)``.  ``deduplicated`` is True when the row
    already existed, which is the caller's signal that the bytes need not be
    transferred again.

    ``pinned=True`` marks bytes owned by something outside the asset graph — the
    shared viral-media cache is the case today.  A pinned row is still counted
    so accounting stays honest, but it is never reclaimed by the sweeper: no
    number of asset deletions should be able to force a re-download of a
    platform-collected video.
    """
    owner = _owner_parameter(scope, owner_user_id)
    candidate_id = str(uuid4())
    inserted = conn.execute(
        """
        INSERT INTO content_objects (
            id, sha256, size_bytes, content_type, provider, bucket,
            object_key, scope, scope_owner, ref_count, pinned
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            candidate_id,
            sha256,
            size_bytes,
            content_type,
            provider,
            bucket,
            object_key,
            scope,
            owner,
            pinned,
        ),
    )
    if inserted.rowcount == 1:
        row = conn.execute(
            "SELECT * FROM content_objects WHERE id = %s",
            (candidate_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError("content object vanished immediately after insert")
        return _to_content_object(row), False

    existing = find_content_object(
        conn,
        sha256=sha256,
        size_bytes=size_bytes,
        provider=provider,
        bucket=bucket,
        scope=scope,
        owner_user_id=owner_user_id,
        for_update=True,
    )
    if existing is None:  # pragma: no cover - unique index guarantees visibility
        raise RuntimeError("content object conflict left no visible row")
    conn.execute(
        """
        UPDATE content_objects
        SET ref_count = ref_count + 1,
            reclaim_after = NULL,
            pinned = pinned OR %s
        WHERE id = %s
        """,
        (pinned, existing.id),
    )
    refreshed = find_content_object_by_id(conn, existing.id)
    if refreshed is None:  # pragma: no cover
        raise RuntimeError("content object disappeared during retain")
    return refreshed, True


def retain_existing_content_object(
    conn: BusinessConnection,
    content_object_id: str,
) -> ContentObject:
    """Take an extra reference to an already-registered object."""
    current = find_content_object_by_id(conn, content_object_id, for_update=True)
    if current is None:
        raise ValueError(f"unknown content object: {content_object_id}")
    conn.execute(
        "UPDATE content_objects SET ref_count = ref_count + 1, reclaim_after = NULL WHERE id = %s",
        (content_object_id,),
    )
    refreshed = find_content_object_by_id(conn, content_object_id)
    if refreshed is None:  # pragma: no cover
        raise RuntimeError("content object disappeared during retain")
    return refreshed


def release_content_object(
    conn: BusinessConnection,
    content_object_id: str,
    *,
    reclaim_delay_hours: int = RECLAIM_DELAY_HOURS,
) -> int:
    """Drop one reference, scheduling reclaim when the last one goes.

    Returns the remaining reference count (``-1`` when the row is already gone,
    which is not an error: deleting an asset twice must stay idempotent).

    This deliberately does **not** touch storage.  Removing bytes here would
    race with a concurrent re-reference; the sweeper owns that decision.
    """
    current = find_content_object_by_id(conn, content_object_id, for_update=True)
    if current is None:
        return -1
    if current.ref_count <= 0:
        # Already released; the sweeper is the only thing that may delete it.
        return 0
    remaining = current.ref_count - 1
    if remaining == 0 and not current.pinned:
        conn.execute(
            """
            UPDATE content_objects
            SET ref_count = 0,
                reclaim_after = now() + make_interval(hours => %s)
            WHERE id = %s
            """,
            (reclaim_delay_hours, content_object_id),
        )
    else:
        conn.execute(
            "UPDATE content_objects SET ref_count = %s, reclaim_after = NULL WHERE id = %s",
            (remaining, content_object_id),
        )
    return remaining


def release_assets_content_objects(
    conn: BusinessConnection,
    *,
    asset_ids: list[str],
    reclaim_delay_hours: int = RECLAIM_DELAY_HOURS,
) -> int:
    """Release every registry reference held by ``asset_ids``.

    Returns the number of registry rows that dropped to zero references.
    """
    if not asset_ids:
        return 0
    placeholders = ", ".join(["%s"] * len(asset_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT content_object_id FROM assets
        WHERE id IN ({placeholders}) AND content_object_id IS NOT NULL
        """,
        list(asset_ids),
    ).fetchall()
    emptied = 0
    for row in rows:
        content_id = str(row["content_object_id"])
        released = release_content_object(
            conn,
            content_id,
            reclaim_delay_hours=reclaim_delay_hours,
        )
        if released != 0:
            continue
        # A pinned row sitting at zero is still owned by something outside the
        # asset graph, so it must not be reported as scheduled for removal.
        current = find_content_object_by_id(conn, content_id)
        if current is not None and not current.pinned:
            emptied += 1
    return emptied


def object_referenced_by_another_asset(
    conn: SqlConnection,
    *,
    storage_uri: str,
    excluding_asset_ids: list[str] | None = None,
) -> bool:
    """NULL-safe form of the legacy shared-object check.

    ``IS DISTINCT FROM`` is the whole point: a plain ``project_id <> %s`` tests
    NULL against a value and yields NULL, so an asset owned by no project —
    material-library and character uploads are exactly that — was invisible to
    the old check and its object got deleted out from under the other
    reference.  Keep this helper as the one place that comparison is written.
    """
    sql = "SELECT 1 FROM assets WHERE storage_uri = %s"
    params: list[Any] = [storage_uri]
    if excluding_asset_ids:
        placeholders = ", ".join(["%s"] * len(excluding_asset_ids))
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(excluding_asset_ids)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def object_referenced_by_content_registry(
    conn: SqlConnection,
    *,
    provider: str,
    bucket: str,
    object_key: str,
    excluding_content_object_id: str | None = None,
) -> bool:
    """Whether any live registry row still points at these bytes."""
    sql = "SELECT 1 FROM content_objects WHERE provider = %s AND bucket = %s AND object_key = %s"
    params: list[Any] = [provider, bucket, object_key]
    if excluding_content_object_id:
        sql += " AND id <> %s"
        params.append(excluding_content_object_id)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def is_content_object_key(object_key: str) -> bool:
    """Whether a key lives in the namespace the registry owns."""
    return object_key == OBJECT_PREFIX or object_key.startswith(f"{OBJECT_PREFIX}/")


def delete_object_outside_content_namespace(
    storage: StorageAdapter,
    object_key: str,
    *,
    actor_id: str | None = None,
) -> bool:
    """Delete a private staging object; refuse anything the registry owns.

    For cleanup helpers that run without a connection — an object written by a
    request that failed before any row could reference it.  Returns True when
    the bytes were removed.

    A key under ``content/`` belongs to :func:`reclaim_expired_content_objects`,
    which alone can see the reference count.  Deleting it here could strand
    another asset's bytes.  Refusing leaks one object, which the sweeper reaps
    later; the opposite mistake is not recoverable.
    """
    if is_content_object_key(object_key):
        logger.warning("refused direct deletion of content-addressed object %s", object_key)
        return False
    storage.delete_object(object_key, actor_id=actor_id)
    return True


def delete_object_if_unreferenced(
    conn: SqlConnection,
    storage: StorageAdapter,
    object_key: str,
    *,
    actor_id: str | None = None,
    excluding_asset_ids: list[str] | None = None,
) -> bool:
    """Delete bytes only once no surviving reference can serve them.

    Two independent guards, ordered by authority:

    1. A ``content_objects`` row for the same key means the registry owns these
       bytes.  Releasing the reference schedules reclaim; deleting inline would
       bypass both the grace window and the count.
    2. A surviving ``assets`` row carrying the same ``storage_uri`` is a live
       reference.  The comparison inside
       :func:`object_referenced_by_another_asset` is NULL-safe, which is the
       whole reason this guard lives here rather than at the call site.

    Call *after* the owning rows are gone.  When the caller still has those rows
    in scope it should pass them in ``excluding_asset_ids``: the rows are deleted
    in the caller's transaction but their ids remain the most accurate way to
    distinguish "mine, now gone" from "someone else's, still live".

    Returns True when the bytes were removed.
    """
    if object_referenced_by_content_registry(
        conn,
        provider=storage.provider,
        bucket=storage.bucket,
        object_key=object_key,
    ):
        logger.warning("refused direct deletion of registered content object %s", object_key)
        return False
    uri = f"{storage.provider}://{storage.bucket}/{object_key}"
    if object_referenced_by_another_asset(
        conn,
        storage_uri=uri,
        excluding_asset_ids=excluding_asset_ids,
    ):
        return False
    storage.delete_object(object_key, actor_id=actor_id)
    return True


def reclaim_expired_content_objects(
    conn: BusinessConnection,
    storage: StorageAdapter,
    *,
    limit: int = 100,
) -> dict[str, int]:
    """Delete bytes whose grace window elapsed with no references left.

    The count is re-read under ``FOR UPDATE SKIP LOCKED`` so a reference taken
    during the window survives, and so two sweepers never fight over one row.
    A storage failure leaves the row in place for the next pass rather than
    losing track of an object that may still exist.
    """
    result = {"scanned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    rows = conn.execute(
        """
        SELECT * FROM content_objects
        WHERE ref_count = 0 AND NOT pinned
          AND reclaim_after IS NOT NULL AND reclaim_after <= now()
        ORDER BY reclaim_after
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        result["scanned"] += 1
        content = _to_content_object(row)
        # Re-read under the lock: a retain between the scan and here must win.
        locked = find_content_object_by_id(conn, content.id, for_update=True)
        if locked is None or locked.ref_count != 0 or locked.pinned:
            result["skipped"] += 1
            continue
        if locked.provider != storage.provider or locked.bucket != storage.bucket:
            # The object lives in another backend; that backend's own sweep owns it.
            result["skipped"] += 1
            continue
        try:
            stored = storage.head_object(locked.object_key)
            if stored is not None:
                storage.delete_object(locked.object_key, actor_id=None)
            conn.execute("DELETE FROM content_objects WHERE id = %s", (locked.id,))
            result["deleted"] += 1
        except Exception:  # noqa: BLE001 - one bad object must not stop the sweep
            logger.warning("content reclaim failed for %s", locked.id, exc_info=True)
            result["failed"] += 1
    return result


def content_object_metadata(content: ContentObject) -> str:
    """Audit-friendly snapshot of a registry row (never the raw bytes)."""
    return json.dumps(
        {
            "content_object_id": content.id,
            "sha256_prefix": content.sha256[:12],
            "scope": content.scope,
            "ref_count": content.ref_count,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def backfill_content_objects(
    conn: BusinessConnection,
    *,
    limit: int = 500,
    apply: bool = False,
) -> dict[str, int]:
    """Register bytes that are already stored so new uploads can reuse them.

    This is phase 1 of the plan: it only *adds* registry rows and links assets to
    them. **No object is moved** — the legacy keys stay exactly where they are,
    because rewriting them would mean copying an unbounded amount of data with a
    rollback we would rather not have to perform.

    What this buys: an asset uploaded *before* the registry existed becomes
    reusable, so re-uploading the same file later hits the dedup path instead of
    storing a second copy. Assets that already share bytes across different keys
    are not merged — the duplicates stay until they are naturally reclaimed.

    Safe to re-run: assets that already carry a ``content_object_id`` are skipped,
    so repeated passes converge and stop finding work.

    ``scope_owner`` falls back from ``created_by_user_id`` to the project owner so
    that project-scoped uploads still land in a real user's scope; two customers
    with identical bytes always get two rows, never one shared object.
    """
    result = {"scanned": 0, "registered": 0, "reused": 0, "linked": 0, "skipped": 0}
    if not 1 <= limit <= 5000:
        raise ValueError("backfill requires a bounded page")
    rows = conn.execute(
        """
        SELECT a.id, a.sha256, a.size_bytes, a.content_type, a.storage_uri,
               a.created_by_user_id, p.owner_user_id AS project_owner
        FROM assets a
        LEFT JOIN projects p ON p.id = a.project_id
        WHERE a.content_object_id IS NULL
          AND a.sha256 IS NOT NULL AND a.sha256 <> ''
          AND a.size_bytes > 0
        ORDER BY a.id
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    seen: set[tuple[str, int, str, str, str]] = set()
    for row in rows:
        result["scanned"] += 1
        try:
            ref = storage_object_ref_from_uri(str(row["storage_uri"]))
        except ValueError:
            result["skipped"] += 1
            continue
        owner = row["created_by_user_id"] or row["project_owner"]
        if not owner:
            result["skipped"] += 1
            continue
        owner = str(owner)
        digest = str(row["sha256"])
        size = int(row["size_bytes"])
        if not apply:
            # Dry run: count distinct groups without touching the registry.
            group = (digest, size, ref.provider, ref.bucket, owner)
            if group not in seen:
                seen.add(group)
                result["registered"] += 1
            continue
        content, deduplicated = retain_content_object(
            conn,
            sha256=digest,
            size_bytes=size,
            content_type=(None if row["content_type"] is None else str(row["content_type"])),
            provider=ref.provider,
            bucket=ref.bucket,
            object_key=ref.key,
            scope="user",
            owner_user_id=owner,
        )
        if deduplicated:
            result["reused"] += 1
        else:
            result["registered"] += 1
        conn.execute(
            "UPDATE assets SET content_object_id = %s WHERE id = %s",
            (content.id, str(row["id"])),
        )
        result["linked"] += 1
    return result


def storage_key_of_uri(storage_uri: str) -> str:
    """Thin wrapper so callers need not import storage internals for one hop."""
    return storage_object_ref_from_uri(storage_uri).key
