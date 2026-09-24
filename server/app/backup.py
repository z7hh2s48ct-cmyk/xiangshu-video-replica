"""Legacy SQLite backup/restore tools — internal P0 lane only (CW-057).

Classification (CW-057 command×database matrix, PG-08): ``python -m
app.backup`` is a *historical* internal-P0 SQLite entry. It is only reachable
from the internal single-host deployment (``deploy/systemd/video-replica-
backup.service``, which self-labels "Internal P0 SQLite only") and from the
CW-060-isolated historical import tooling (``sqlite_to_postgres`` reuses
``SqliteSnapshot``/``create_readonly_snapshot``). The customer PostgreSQL
backup/restore path is ``deploy/postgres/pitr-*.sh`` (pg_basebackup + WAL)
verified by ``scripts.pitr_recovery_facts``; the SQLite backup timer is
excluded from the customer formal deployment package (CW-032) and the whole
internal lane exits with CW-040/CW-042. No current maintenance/seed/admin
CLI may call into this module.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.db import connect_database


@dataclass(frozen=True)
class SqliteSnapshot:
    """Immutable evidence for one legacy SQLite cutover snapshot."""

    path: Path
    source_sha256: str
    snapshot_sha256: str
    source_size_bytes: int
    created_at: str

    @property
    def snapshot_path(self) -> Path:
        """Compatibility alias used by migration callers and evidence tooling."""

        return self.path

    @property
    def sha256(self) -> str:
        """Compatibility alias for the immutable snapshot digest."""

        return self.snapshot_sha256


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sidecars(database: Path) -> tuple[Path, ...]:
    return tuple(
        Path(f"{database}{suffix}")
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{database}{suffix}").exists()
    )


def _assert_quiescent_source(database: Path) -> None:
    """Fail closed unless SQLite has checkpointed and all writers are stopped.

    A WAL commit can leave the main database file unchanged, so comparing only
    that file's hash is insufficient. During the maintenance window every
    SQLite process must be stopped and the final connection closed, which
    checkpoints/removes WAL sidecars. Any sidecar before or after the backup is
    therefore treated as evidence of an active or incompletely stopped writer.
    """

    sidecars = _sqlite_sidecars(database)
    if sidecars:
        names = ", ".join(path.name for path in sidecars)
        raise RuntimeError(
            "SQLite source is not quiescent: WAL/journal sidecars are present "
            f"({names}); stop every writer, close all connections, checkpoint, and retry"
        )


def _assert_delete_journal_mode(database: Path) -> None:
    """Fail closed unless the source's persistent journal mode is DELETE.

    ``journal_mode = WAL`` is persisted in the database header (bytes 18/19):
    even after the WAL file itself is checkpointed away on the last close, the
    next ordinary connection re-creates the ``-wal``/``-shm`` sidecars — and
    the writer fence itself is such a connection. The maintenance-window
    contract therefore requires the source to end in DELETE journal mode
    (``PRAGMA wal_checkpoint(TRUNCATE)`` + ``PRAGMA journal_mode = DELETE``
    after the final writer closes), which is checked here by reading the
    header directly, without opening any connection.
    """

    with database.open("rb") as stream:
        header = stream.read(20)
    if len(header) >= 20 and (header[18] == 2 or header[19] == 2):
        raise RuntimeError(
            "SQLite source is still in WAL journal mode; inside the maintenance "
            "window run wal_checkpoint(TRUNCATE) and switch journal_mode to "
            "DELETE before creating the snapshot"
        )


def _readonly_connection(database: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=5,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _create_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def _publish_without_overwrite(temporary: Path, destination: Path) -> None:
    """Atomically publish a same-directory file without replacing evidence."""

    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise FileExistsError(f"migration snapshot already exists: {destination}") from None
    try:
        temporary.unlink()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def _writer_fence(database: Path) -> Iterator[None]:
    """Hold a SQLite writer fence for the entire snapshot creation.

    An accidentally resumed writer must not be able to commit after the
    pre-snapshot hash/stat were sampled, while the snapshot hash and metadata
    are computed, or after the final re-check has passed. The fence is an
    open ``BEGIN IMMEDIATE`` transaction on the source database: every other
    process' write fails with SQLITE_BUSY while it is held, and the lock
    disappears automatically if this process dies. The fence never writes,
    so it creates no journal sidecar and never invalidates the
    quiescent-source checks.
    """

    fence = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        try:
            fence.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise RuntimeError(
                "cannot acquire the migration writer fence; another writer "
                "already holds the source database"
            ) from error
        try:
            yield
        finally:
            fence.execute("ROLLBACK")
    finally:
        fence.close()


def create_readonly_snapshot(
    source_path: str | Path,
    snapshot_path: str | Path,
) -> SqliteSnapshot:
    """Create a private, immutable, integrity-checked SQLite snapshot.

    The source is opened read-only and immutable. WAL/journal sidecars are
    rejected before and after the online backup so a WAL-backed write cannot be
    silently omitted while the main file hash remains unchanged. A writer
    fence (``BEGIN IMMEDIATE``) is held from the first hash until the snapshot
    and its metadata are fully computed, so an accidentally resumed writer
    cannot land a commit inside or after the re-check window. The output is
    created as mode ``0600`` and published with a no-overwrite hard link.
    """

    source = Path(source_path)
    snapshot = Path(snapshot_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == snapshot.resolve():
        raise ValueError("snapshot path must differ from the source database")
    if snapshot.exists():
        raise FileExistsError(f"migration snapshot already exists: {snapshot}")
    _assert_quiescent_source(source)
    _assert_delete_journal_mode(source)

    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with _writer_fence(source):
        temporary = snapshot.with_name(f".{snapshot.name}.{os.getpid()}.{uuid4().hex}.tmp")
        _create_private_file(temporary)

        before_hash = sha256_file(source)
        before_stat = source.stat()
        linked = False
        try:
            _assert_quiescent_source(source)
            with closing(_readonly_connection(source)) as source_conn:
                _check_integrity(source_conn)
                data_version_before = int(source_conn.execute("PRAGMA data_version").fetchone()[0])
                with closing(sqlite3.connect(temporary)) as snapshot_conn:
                    source_conn.backup(snapshot_conn)
                    _check_integrity(snapshot_conn)
                data_version_after = int(source_conn.execute("PRAGMA data_version").fetchone()[0])
                if data_version_before != data_version_after:
                    raise RuntimeError(
                        "SQLite source changed while the snapshot was created; "
                        "stop every writer and retry the maintenance window"
                    )

            _assert_quiescent_source(source)
            after_stat = source.stat()
            after_hash = sha256_file(source)
            if (
                before_hash != after_hash
                or before_stat.st_size != after_stat.st_size
                or before_stat.st_mtime_ns != after_stat.st_mtime_ns
            ):
                raise RuntimeError(
                    "SQLite source changed while the snapshot was created; "
                    "stop every writer and retry the maintenance window"
                )

            os.chmod(temporary, 0o600)
            # Windows FlushFileBuffers fails with EBADF on a read-only file
            # descriptor; "rb+" keeps the durability flush meaningful
            # cross-platform.
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            _publish_without_overwrite(temporary, snapshot)
            linked = True
            _assert_quiescent_source(source)
            # Hash first, then stat: a write landing while (or right after) the
            # final hash reads the file is still caught by the stat comparison,
            # and any earlier write by the hash comparison.
            final_hash = sha256_file(source)
            final_stat = source.stat()
            if (
                before_hash != final_hash
                or before_stat.st_size != final_stat.st_size
                or before_stat.st_mtime_ns != final_stat.st_mtime_ns
            ):
                raise RuntimeError(
                    "SQLite source changed while the snapshot was created; "
                    "stop every writer and retry the maintenance window"
                )
            os.chmod(snapshot, 0o600)
            # Windows reports a synthesized 0o666 mode for every file
            # regardless of the requested 0600, so the privacy assertion is
            # POSIX-only.
            if sys.platform != "win32" and snapshot.stat().st_mode & 0o077:
                raise PermissionError(
                    "migration snapshot permissions are not private (expected 0600)"
                )
            metadata = SqliteSnapshot(
                path=snapshot.resolve(),
                source_sha256=before_hash,
                snapshot_sha256=sha256_file(snapshot),
                source_size_bytes=before_stat.st_size,
                created_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            if linked:
                snapshot.unlink(missing_ok=True)
            raise
        return metadata


def check_database(database_path: str | Path) -> Path:
    database = Path(database_path)
    if not database.exists():
        raise FileNotFoundError(database)

    with connect_database(database) as conn:
        _check_integrity(conn)
    return database.resolve()


def _private_temporary(destination: Path) -> Path:
    """A same-directory temporary owned exclusively by this writer.

    The name carries the PID and a random suffix so concurrent runs never
    unlink or reuse each other's file (the old fixed ``.{name}.tmp`` let two
    backups clobber each other), and it is created O_EXCL with 0600 so the
    database contents inside are not world-readable — the snapshot hygiene
    applied to the backup/restore paths (M1 review M3).
    """

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp")
    _create_private_file(temporary)
    return temporary


def _flush_to_disk(path: Path) -> None:
    # Windows FlushFileBuffers fails with EBADF on a read-only file
    # descriptor; "rb+" keeps the durability flush meaningful
    # cross-platform.
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def backup_database(source_path: str | Path, backup_path: str | Path) -> Path:
    source = Path(source_path)
    backup = Path(backup_path)
    if not source.exists():
        raise FileNotFoundError(source)

    backup.parent.mkdir(parents=True, exist_ok=True)
    temp_backup = _private_temporary(backup)
    try:
        with connect_database(source) as source_conn:
            _check_integrity(source_conn)
            # sqlite3's context manager only commits/rolls back the transaction;
            # closing() is required to release the file handle before
            # os.replace, otherwise Windows raises WinError 32 on the rename.
            with closing(sqlite3.connect(temp_backup)) as backup_conn:
                source_conn.backup(backup_conn)
                _check_integrity(backup_conn)
        _flush_to_disk(temp_backup)
        os.replace(temp_backup, backup)
    except BaseException:
        temp_backup.unlink(missing_ok=True)
        raise
    return backup.resolve()


def restore_database(backup_path: str | Path, target_path: str | Path) -> Path:
    backup = Path(backup_path)
    target = Path(target_path)
    if not backup.exists():
        raise FileNotFoundError(backup)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = _private_temporary(target)
    try:
        with closing(sqlite3.connect(backup)) as backup_conn:
            _check_integrity(backup_conn)
            with closing(sqlite3.connect(temp_target)) as target_conn:
                backup_conn.backup(target_conn)
                _check_integrity(target_conn)
        _flush_to_disk(temp_target)
        os.replace(temp_target, target)
    except BaseException:
        temp_target.unlink(missing_ok=True)
        raise
    return target.resolve()


def run_daily_backup(source_path: str | Path, backup_dir: str | Path) -> Path:
    source = Path(source_path)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    backup_path = backup_database(source, Path(backup_dir) / f"{source.stem}-{timestamp}.db")
    _prune_backups(Path(backup_dir), source.stem, keep=BACKUP_RETENTION_COUNT)
    return backup_path


BACKUP_RETENTION_COUNT = 30


def _prune_backups(backup_dir: Path, stem: str, *, keep: int) -> None:
    backups = sorted(backup_dir.glob(f"{stem}-*.db"))
    for stale in backups[:-keep]:
        stale.unlink(missing_ok=True)


def _check_integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        detail = "missing result" if result is None else str(result[0])
        raise sqlite3.DatabaseError(f"SQLite integrity check failed: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite backup and restore tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("database")

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("source")
    backup_parser.add_argument("backup")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("target")

    daily_parser = subparsers.add_parser("daily")
    daily_parser.add_argument("source")
    daily_parser.add_argument("backup_dir")

    args = parser.parse_args()
    if args.command == "check":
        output = check_database(args.database)
    elif args.command == "backup":
        output = backup_database(args.source, args.backup)
    elif args.command == "restore":
        output = restore_database(args.backup, args.target)
    else:
        output = run_daily_backup(args.source, args.backup_dir)
    print(output)


if __name__ == "__main__":
    main()
