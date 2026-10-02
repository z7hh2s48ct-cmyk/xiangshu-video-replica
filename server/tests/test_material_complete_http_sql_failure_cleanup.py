"""HTTP regressions for compatibility derivatives on material video uploads.

These tests deliberately use a newly migrated PostgreSQL database and the real
FastAPI material routes.  Storage is the in-memory adapter, so no external
service or billing path is reachable while the transaction boundary remains
the production PostgreSQL one.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter

ORAL_VIDEO_COMPAT_TEST_DB = "oral_video_compat_test"
MATERIALS_URL = "/api/studio/materials"


@pytest.fixture(scope="module")
def compat_dsn() -> Iterator[str]:
    """A migration-from-empty database, not a stale shared test fixture."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(ORAL_VIDEO_COMPAT_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        close_pg_pool()
        drop_test_database(ORAL_VIDEO_COMPAT_TEST_DB)


@pytest.fixture()
def compat_state(compat_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, compat_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    with psycopg.connect(compat_dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('http_owner_a', 'http_owner_a', 'HTTP Owner A', 'employee'), "
            "('http_owner_b', 'http_owner_b', 'HTTP Owner B', 'employee'), "
            "('reuse_owner_a', 'reuse_owner_a', 'Reuse Owner A', 'employee'), "
            "('reuse_owner_b', 'reuse_owner_b', 'Reuse Owner B', 'employee'), "
            "('recovery_owner', 'recovery_owner', 'Recovery Owner', 'employee') "
            "ON CONFLICT (id) DO NOTHING"
        )
    try:
        yield compat_dsn
    finally:
        close_pg_pool()


class _RouteBusinessDb:
    """Route dependency double that keeps each write a real PG transaction."""

    def __init__(self, actor_id: str) -> None:
        self.actor = CurrentUser(
            id=actor_id,
            username=actor_id,
            display_name=actor_id,
            role="employee",
        )
        self.write_calls = 0

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        self.write_calls += 1
        with pg_transaction() as raw:
            yield BusinessConnection.postgres(raw), self.actor


@contextmanager
def _material_client(
    actor_id: str, storage: FakeStorageAdapter
) -> Iterator[tuple[TestClient, _RouteBusinessDb]]:
    from app.main import app

    db = _RouteBusinessDb(actor_id)
    app.dependency_overrides[get_business_db] = lambda: db
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        yield TestClient(app, raise_server_exceptions=False), db
    finally:
        app.dependency_overrides.clear()


def _hevc_payload() -> tuple[bytes, str]:
    """Build a tiny HEVC/AAC upload so probe and conversion are both real."""
    from app.media_tools import resolve_media_binary

    with tempfile.TemporaryDirectory(prefix="oral-video-compat-") as directory:
        path = Path(directory) / "source-hevc.mp4"
        subprocess.run(
            [
                resolve_media_binary("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=144x256:r=12",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=32000",
                "-t",
                "0.5",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx265",
                "-tag:v",
                "hvc1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        payload = path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest()


def _new_video_intent(client: TestClient, payload: bytes, digest: str) -> dict[str, Any]:
    response = client.post(
        f"{MATERIALS_URL}/upload-intent",
        json={
            "filename": "source-hevc.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(payload),
            "sha256": digest,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _scalar(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def test_material_complete_http_sql_failure_cleanup(
    compat_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB-aborted completion removes only its fresh derivative and can retry."""
    import app.materials as materials

    storage = FakeStorageAdapter(provider="fake", bucket="oral-video-compat")
    payload, digest = _hevc_payload()
    original_write_audit = materials.write_audit
    original_delete_gate = materials.delete_object_if_unreferenced
    deleted_keys: list[tuple[str, str | None]] = []
    compatible_keys: list[str] = []
    real_put = storage.put_object

    def record_put(key: str, content: bytes, *, content_type: str):
        if key.startswith("materials/compatible/"):
            compatible_keys.append(key)
        return real_put(key, content, content_type=content_type)

    def record_delete_gate(
        conn: BusinessConnection,
        adapter: FakeStorageAdapter,
        object_key: str,
        *,
        actor_id: str | None = None,
        excluding_asset_ids: list[str] | None = None,
    ) -> bool:
        deleted_keys.append((object_key, actor_id))
        return original_delete_gate(
            conn,
            adapter,
            object_key,
            actor_id=actor_id,
            excluding_asset_ids=excluding_asset_ids,
        )

    def abort_persist_with_real_sql(conn: BusinessConnection, **_: Any) -> None:
        # This is deliberately a PostgreSQL statement error, not a Python
        # exception: after it the persistence transaction is aborted.
        conn.execute("SELECT 1 / 0")

    monkeypatch.setattr(storage, "put_object", record_put)
    monkeypatch.setattr(materials, "delete_object_if_unreferenced", record_delete_gate)

    with _material_client("http_owner_a", storage) as (client, db):
        intent = _new_video_intent(client, payload, digest)
        original_key = intent["storage_key"]
        assert isinstance(original_key, str)
        storage.put_object(original_key, payload, content_type="video/mp4")
        writes_before_complete = db.write_calls
        monkeypatch.setattr(materials, "write_audit", abort_persist_with_real_sql)

        failed = client.post(f"{MATERIALS_URL}/uploads/{intent['asset_id']}/complete")
        assert failed.status_code == 500, failed.text
        # prepare, failed persist, then the route's fresh cleanup transaction.
        assert db.write_calls == writes_before_complete + 3
        assert len(compatible_keys) == 1
        compatible_key = compatible_keys[0]
        assert deleted_keys == [(compatible_key, "http_owner_a")]
        assert storage.head_object(compatible_key) is None
        assert storage.head_object(original_key) is not None
        assert (
            _scalar(
                compat_state,
                "SELECT metadata_json::jsonb->>'upload_status' FROM assets WHERE id = %s",
                (intent["asset_id"],),
            )
            == "PENDING"
        )
        assert (
            _scalar(
                compat_state,
                "SELECT count(*) FROM video_compat_derivatives WHERE original_asset_id = %s",
                (intent["asset_id"],),
            )
            == 0
        )
        # No generation supplier or charging path is part of a material retry.
        assert _scalar(compat_state, "SELECT count(*) FROM wallet_transactions") == 0
        assert _scalar(compat_state, "SELECT count(*) FROM generation_tasks") == 0

        monkeypatch.setattr(materials, "write_audit", original_write_audit)
        retried = client.post(f"{MATERIALS_URL}/uploads/{intent['asset_id']}/complete")
        assert retried.status_code == 200, retried.text
        assert retried.json()["asset_id"] == intent["asset_id"]

    assert (
        _scalar(
            compat_state,
            "SELECT count(*) FROM video_compat_derivatives WHERE original_asset_id = %s",
            (intent["asset_id"],),
        )
        == 1
    )


def test_fresh_migration_hevc_instant_reuse_shares_derivative_and_isolates_owner(
    compat_state: str,
) -> None:
    """Two originals may point at one derivative; a different owner cannot reuse it."""
    storage = FakeStorageAdapter(provider="fake", bucket="oral-video-compat")
    payload, digest = _hevc_payload()

    with _material_client("reuse_owner_a", storage) as (client, _db):
        first = _new_video_intent(client, payload, digest)
        assert first["upload_required"] is True
        first_key = first["storage_key"]
        assert isinstance(first_key, str)
        storage.put_object(first_key, payload, content_type="video/mp4")
        completed = client.post(f"{MATERIALS_URL}/uploads/{first['asset_id']}/complete")
        assert completed.status_code == 200, completed.text

        reused = _new_video_intent(client, payload, digest)
        assert reused["upload_required"] is False
        assert reused["asset_id"] != first["asset_id"]
        assert reused["reused_from_asset_id"] == first["asset_id"]
        # A client normally skips this call after instant reuse; keeping it in
        # the route matrix proves the READY replay has no unique collision.
        replay = client.post(f"{MATERIALS_URL}/uploads/{reused['asset_id']}/complete")
        assert replay.status_code == 200, replay.text

    with _material_client("reuse_owner_b", storage) as (client, _db):
        foreign = _new_video_intent(client, payload, digest)
        assert foreign["upload_required"] is True
        assert foreign["reused_from_asset_id"] is None
        assert foreign["asset_id"] not in {first["asset_id"], reused["asset_id"]}

    relations: list[tuple[str, str]]
    with psycopg.connect(compat_state, autocommit=True) as pg:
        relations = [
            (str(row[0]), str(row[1]))
            for row in pg.execute(
                "SELECT original_asset_id, compatible_asset_id "
                "FROM video_compat_derivatives WHERE original_asset_id = ANY(%s) "
                "ORDER BY original_asset_id",
                ([first["asset_id"], reused["asset_id"]],),
            ).fetchall()
        ]
        reused_row = pg.execute(
            "SELECT created_by_user_id, metadata_json FROM assets WHERE id = %s",
            (reused["asset_id"],),
        ).fetchone()
    assert reused_row is not None
    assert reused_row[0] == "reuse_owner_a"
    assert json.loads(reused_row[1])["compatibility_status"] == "READY"
    by_original = dict(relations)
    assert set(by_original) == {first["asset_id"], reused["asset_id"]}
    assert by_original[first["asset_id"]] == by_original[reused["asset_id"]]
    assert (
        _scalar(
            compat_state,
            "SELECT count(*) FROM assets WHERE id = %s AND kind = 'oral_compatible_video'",
            (by_original[first["asset_id"]],),
        )
        == 1
    )


def test_instant_video_reuse_rebuilds_a_deleted_derived_object(
    compat_state: str,
) -> None:
    """派生对象缺失后，本地重建且仍复用已保留的原片。"""
    storage = FakeStorageAdapter(provider="fake", bucket="oral-video-compat")
    payload, digest = _hevc_payload()

    with _material_client("recovery_owner", storage) as (client, _db):
        first = _new_video_intent(client, payload, digest)
        assert first["storage_key"] is not None
        storage.put_object(first["storage_key"], payload, content_type="video/mp4")
        completed = client.post(f"{MATERIALS_URL}/uploads/{first['asset_id']}/complete")
        assert completed.status_code == 200, completed.text

        with psycopg.connect(compat_state, autocommit=True) as pg:
            old = pg.execute(
                """
                SELECT d.compatible_asset_id, a.storage_uri
                FROM video_compat_derivatives d
                JOIN assets a ON a.id = d.compatible_asset_id
                WHERE d.original_asset_id = %s
                """,
                (first["asset_id"],),
            ).fetchone()
        assert old is not None
        old_compatible_id, old_uri = str(old[0]), str(old[1])
        old_key = old_uri.removeprefix("fake://oral-video-compat/")
        storage.delete_object(old_key)
        assert storage.head_object(old_key) is None

        recovered = _new_video_intent(client, payload, digest)
        assert recovered["upload_required"] is False
        assert recovered["reused_from_asset_id"] == first["asset_id"]
        assert recovered["asset_id"] != first["asset_id"]

    with psycopg.connect(compat_state, autocommit=True) as pg:
        rebuilt = pg.execute(
            """
            SELECT d.compatible_asset_id, a.storage_uri
            FROM video_compat_derivatives d
            JOIN assets a ON a.id = d.compatible_asset_id
            WHERE d.original_asset_id = %s
            """,
            (first["asset_id"],),
        ).fetchone()
        copied = pg.execute(
            "SELECT compatible_asset_id FROM video_compat_derivatives WHERE original_asset_id = %s",
            (recovered["asset_id"],),
        ).fetchone()
    assert rebuilt is not None and copied is not None
    rebuilt_id, rebuilt_uri = str(rebuilt[0]), str(rebuilt[1])
    assert rebuilt_id != old_compatible_id
    assert str(copied[0]) == rebuilt_id
    rebuilt_key = rebuilt_uri.removeprefix("fake://oral-video-compat/")
    assert storage.head_object(rebuilt_key) is not None
    assert _scalar(compat_state, "SELECT count(*) FROM wallet_transactions") == 0
    assert _scalar(compat_state, "SELECT count(*) FROM generation_tasks") == 0
