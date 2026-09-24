"""Shared on-demand media preparation with short PG transactions and fenced publication.

Only the lease holder downloads. Each attempt writes an immutable object; the
database pointer is published after HEAD verification. Project copies have an
independent lifetime. No provider I/O, object I/O or waiting holds a transaction.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app import content_store
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.storage import StorageAdapter, StoredObject
from app.viral_tikhub import ViralSourceError

logger = logging.getLogger(__name__)
MEDIA_PROCESSING_VERSION = "decrypted-original-v1"
_LEASE_SECONDS = 120
_HEARTBEAT_SECONDS = 20
_NOW = "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
_UNTIL = (
    "to_char((clock_timestamp() AT TIME ZONE 'UTC') + "
    f"interval '{_LEASE_SECONDS} seconds', 'YYYY-MM-DD HH24:MI:SS')"
)


@contextmanager
def _connection() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw:
        yield BusinessConnection.postgres(raw)


class ViralMediaBusy(ViralSourceError):
    """The caller may retry without starting another paid preparation."""


class ViralMediaLeaseLost(ViralSourceError):
    """This attempt must not publish or return its uncommitted object."""


@dataclass(frozen=True)
class MediaLease:
    id: str
    attempt: int
    token: str
    scope: str
    key: str


class ViralMediaPreparation:
    def __init__(
        self, *, storage: StorageAdapter, wait_seconds: float = 90, poll_seconds: float = 0.25
    ) -> None:
        self.storage = storage
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.scope = hashlib.sha256(
            f"{storage.cache_namespace}|{MEDIA_PROCESSING_VERSION}".encode()
        ).hexdigest()

    def _key(self, row: Any, kind: str, *, scope: str | None = None) -> str:
        extension = "mp3" if kind == "audio" else "mp4"
        identity = hashlib.sha256(str(row["id"]).encode()).hexdigest()
        return f"viral/prepared/{scope or self.scope}/{identity}/{row['attempt']}.{extension}"

    def cached(self, *, platform: str, video_id: str, kind: str) -> StoredObject | None:
        with _connection() as conn:
            row = conn.execute(
                "SELECT * FROM viral_media_preparations "
                "WHERE platform=%s AND video_id=%s AND media_kind=%s",
                (platform, video_id, kind),
            ).fetchone()
        stored = self._cached(row, kind)
        return stored if stored is not None else self._migrate_local_cache(row, kind)

    def _migrate_local_cache(self, row: Any, kind: str) -> StoredObject | None:
        """Copy a verified development-era object before atomically moving its pointer.

        No source-provider call and no removal of the old object. A failed copy
        leaves the old pointer intact; production rejects legacy local storage.
        """
        if (
            row is None
            or row["status"] != "SUCCEEDED"
            or self.storage.provider != "cos"
            or not str(row["storage_uri"] or "").startswith("local://")
        ):
            return None
        from app.media_routes import storage_for_asset

        with _connection() as conn:
            source_storage = storage_for_asset(conn, str(row["storage_uri"]))
        # Local directories may have been moved during restore. Validate the
        # persisted immutable identity instead of trusting a new root's scope.
        old_scope = str(row["cache_scope"] or "")
        if re.fullmatch(r"[a-f0-9]{64}", old_scope) is None:
            return None
        source = source_storage.head_object(self._key(row, kind, scope=old_scope))
        if (
            source is None
            or source.uri != row["storage_uri"]
            or source.size <= 0
            or not source.sha256
        ):
            return None
        destination = self._key(row, kind)
        migrated = self.storage.head_object(destination)
        if migrated is None:
            with tempfile.TemporaryDirectory(prefix="viral-cache-migration-") as directory:
                path = Path(directory) / "media"
                digest = hashlib.sha256()
                size = 0
                with path.open("wb") as output:
                    for chunk in source_storage.iter_object(source.key):
                        size += len(chunk)
                        if size > source.size:
                            raise ViralMediaBusy("历史素材校验失败，请重新归档。")
                        digest.update(chunk)
                        output.write(chunk)
                if size != source.size or digest.hexdigest() != source.sha256:
                    raise ViralMediaBusy("历史素材校验失败，请重新归档。")
                self.storage.put_file(destination, path, content_type=source.content_type)
            migrated = self.storage.head_object(destination)
        if migrated is None or migrated.size != source.size or migrated.sha256 != source.sha256:
            raise ViralMediaBusy("云端素材校验失败，原素材仍保留，请稍后重试。")
        with _connection() as conn:
            updated = conn.execute(
                f"""UPDATE viral_media_preparations SET storage_uri=%s,cache_scope=%s,
                    updated_at={_NOW}
                WHERE id=%s AND status='SUCCEEDED' AND attempt=%s
                    AND cache_scope=%s AND storage_uri=%s""",
                (
                    migrated.uri,
                    self.scope,
                    row["id"],
                    row["attempt"],
                    row["cache_scope"],
                    row["storage_uri"],
                ),
            ).rowcount
            current = conn.execute(
                "SELECT * FROM viral_media_preparations WHERE id=%s", (row["id"],)
            ).fetchone()
        result = self._cached(current, kind)
        if not updated and result is None:
            raise ViralMediaLeaseLost("素材记录已更新，请重新读取。")
        return result

    def _cached(self, row: Any, kind: str) -> StoredObject | None:
        if row is None or row["status"] != "SUCCEEDED" or row["cache_scope"] != self.scope:
            return None
        # Reconstruct the only valid key instead of trusting an arbitrary DB URI.
        stored = self.storage.head_object(self._key(row, kind))
        if stored and stored.uri == row["storage_uri"] and stored.size > 0 and stored.sha256:
            return stored
        return None

    def fetch(
        self,
        *,
        platform: str,
        video_id: str,
        kind: str,
        prepare: Callable[[str, Callable[[], None]], StoredObject],
    ) -> tuple[StoredObject, bool]:
        deadline = time.monotonic() + self.wait_seconds
        while True:
            with _connection() as conn:
                conn.execute(
                    """INSERT INTO viral_media_preparations(id,platform,video_id,media_kind)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(platform,video_id,media_kind) DO NOTHING""",
                    (str(uuid4()), platform, video_id, kind),
                )
                row = conn.execute(
                    "SELECT * FROM viral_media_preparations "
                    "WHERE platform=%s AND video_id=%s AND media_kind=%s",
                    (platform, video_id, kind),
                ).fetchone()
            cached = self._cached(row, kind)
            if cached is not None:
                return cached, True
            token = str(uuid4())
            with _connection() as conn:
                claimed = conn.execute(
                    f"""UPDATE viral_media_preparations SET status='RUNNING', attempt=attempt+1,
                        locked_by=%s, locked_until={_UNTIL}, owner_task_id=NULL,
                        cache_scope=%s, storage_uri=NULL, error_code=NULL, completed_at=NULL,
                        updated_at={_NOW}
                    WHERE id=%s AND attempt=%s AND status=%s
                        AND cache_scope IS NOT DISTINCT FROM %s
                        AND storage_uri IS NOT DISTINCT FROM %s AND (
                        status IN ('PENDING','SUCCEEDED') OR
                        (status='FAILED' AND updated_at < to_char(
                            (clock_timestamp() AT TIME ZONE 'UTC') - interval '5 seconds',
                            'YYYY-MM-DD HH24:MI:SS')) OR
                        (status='RUNNING' AND (locked_until IS NULL OR locked_until <= {_NOW})))
                    RETURNING *""",
                    (
                        token,
                        self.scope,
                        row["id"],
                        row["attempt"],
                        row["status"],
                        row["cache_scope"],
                        row["storage_uri"],
                    ),
                ).fetchone()
            if claimed is not None:
                lease = MediaLease(
                    str(claimed["id"]),
                    int(claimed["attempt"]),
                    token,
                    self.scope,
                    self._key(claimed, kind),
                )
                return self._perform(lease, prepare), False
            if row["status"] == "FAILED":
                raise ViralMediaBusy("素材准备暂未完成，请稍后重试。")
            if time.monotonic() >= deadline:
                raise ViralMediaBusy("该视频素材正在准备中，请稍后重试。")
            time.sleep(self.poll_seconds)

    def _renew(self, lease: MediaLease) -> bool:
        with _connection() as conn:
            return (
                conn.execute(
                    f"""UPDATE viral_media_preparations SET locked_until={_UNTIL}, updated_at={_NOW}
                WHERE id=%s AND attempt=%s AND locked_by=%s AND cache_scope=%s
                    AND status='RUNNING' AND locked_until > {_NOW}""",
                    (lease.id, lease.attempt, lease.token, lease.scope),
                ).rowcount
                == 1
            )

    def _publish(self, lease: MediaLease, stored: StoredObject) -> None:
        with _connection() as conn:
            updated = conn.execute(
                f"""UPDATE viral_media_preparations SET status='SUCCEEDED', storage_uri=%s,
                    locked_by=NULL, locked_until=NULL, completed_at={_NOW}, updated_at={_NOW}
                WHERE id=%s AND attempt=%s AND locked_by=%s AND cache_scope=%s
                    AND status='RUNNING' AND locked_until > {_NOW}""",
                (stored.uri, lease.id, lease.attempt, lease.token, lease.scope),
            )
            if updated.rowcount != 1:
                raise ViralMediaLeaseLost("素材准备任务已失效，请重试。")

    def _perform(
        self, lease: MediaLease, prepare: Callable[[str, Callable[[], None]], StoredObject]
    ) -> StoredObject:
        stop, lost = threading.Event(), threading.Event()

        def heartbeat() -> None:
            while not stop.wait(_HEARTBEAT_SECONDS):
                try:
                    if self._renew(lease):
                        continue
                except Exception:
                    logger.warning("Shared media lease renewal failed")
                lost.set()
                return

        def check() -> None:
            if lost.is_set():
                raise ViralMediaLeaseLost("素材准备任务已失效，请重试。")

        thread = threading.Thread(target=heartbeat, daemon=True, name="viral-media-lease")
        thread.start()
        # An ambiguous publication failure must retain this immutable object:
        # the DB commit may have succeeded before the connection was lost.
        publishing = False
        try:
            stored = prepare(lease.key, check)
            check()
            verified = self.storage.head_object(lease.key)
            if (
                verified is None
                or verified.size <= 0
                or not verified.sha256
                or verified.uri != stored.uri
                or verified.size != stored.size
                or verified.sha256 != stored.sha256
            ):
                raise ViralSourceError("媒体存储校验失败，请稍后重试。")
            if not self._renew(lease):
                raise ViralMediaLeaseLost("素材准备任务已失效，请重试。")
            publishing = True
            self._publish(lease, verified)
            return verified
        except BaseException:
            with _connection() as conn:
                conn.execute(
                    f"""UPDATE viral_media_preparations SET status='FAILED',
                        error_code='VIRAL_MEDIA_PREPARATION_FAILED',
                        locked_by=NULL, locked_until=NULL, updated_at={_NOW}, completed_at={_NOW}
                    WHERE id=%s AND attempt=%s AND locked_by=%s AND status='RUNNING'""",
                    (lease.id, lease.attempt, lease.token),
                )
            if not publishing:
                try:
                    content_store.delete_object_outside_content_namespace(self.storage, lease.key)
                except Exception:
                    logger.warning("Unpublished shared media cleanup deferred")
            raise
        finally:
            stop.set()
            thread.join()
