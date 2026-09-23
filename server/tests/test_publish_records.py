"""Publish records (phase 2) on a real PG lane: lifecycle, scheduling, claims.

Dedicated database ``publish_records_test`` (registered in pg_test_kit),
migrated to alembic head so ``publish_records`` and the browser-account status
columns exist. Platform delivery is always a fake — no network, no vendor
imports; the worker round is exercised through ``run_publish_round`` with the
delivery seam monkeypatched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from test_publish_accounts import (
    _TEST_KEY,
    _clear_dependency_overrides,
    _PgBusinessDb,
    actor,
    client,
    holder,
    lane_env,
)

from app import publish_delivery
from app import publish_worker as worker_mod
from app.db_pg import close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.publish import PublishLeaseLostError
from app.publish_records import (
    ACCOUNT_GONE_MESSAGE,
    MAX_ATTEMPTS,
    QUARANTINE_MESSAGE,
    PublishWork,
    claim_publish_work,
    finalize_publish_work,
)
from app.publishers.base import PublishResult

__all__ = ["_clear_dependency_overrides", "client", "holder", "lane_env"]

TEST_DB = "publish_records_test"
BASE = "/api/studio/publish/records"
_STORAGE_STATE = {
    "cookies": [{"name": "sessionid", "value": "synthetic-session", "domain": ".douyin.com"}],
    "origins": [],
}


@pytest.fixture(scope="module")
def cw068_dsn() -> Iterator[str]:
    """Own database; the imported ``lane_env`` / ``client`` fixtures resolve to this."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(TEST_DB)


@pytest.fixture()
def pg(cw068_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(cw068_dsn, autocommit=True)
    conn.execute("SET session_replication_role = replica")
    conn.execute(
        "TRUNCATE publish_records, publish_browser_accounts, publish_browser_logins, "
        "assets, users, audit_logs CASCADE"
    )
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("auditor_1", "auditor_1", "Auditor", "auditor"),
            ],
        )
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


# --------------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------------- #


def seed_account(
    pg: psycopg.Connection,
    *,
    user_id: str = "employee_1",
    platform: str = "douyin",
    status: str = "connected",
    storage: dict[str, Any] | None = None,
    username: str = "创作者昵称",
) -> str:
    account_id = str(uuid4())
    enc = Fernet(_TEST_KEY.encode()).encrypt(
        json.dumps(storage if storage is not None else _STORAGE_STATE).encode()
    )
    # 刚连接的账号尚未到 24h 巡检期：与生产 upsert_browser_account 一致把
    # next_probe_at 设为 now+24h，这样发布投递测试走 phase 2，而不会被 worker
    # 的 phase-1b 浏览器登录态探测抢占（探测到期语义在 test_publish_browser.py
    # 专测）。留空 NULL 会被 claim_browser_probe_work 视作「立即到期」。
    pg.execute(
        "INSERT INTO publish_browser_accounts "
        "(id, user_id, platform, platform_user_id, username, storage_state_enc, status, "
        "next_probe_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp() + interval '24 hours')",
        (account_id, user_id, platform, "uid-" + account_id[:8], username, enc.decode(), status),
    )
    return account_id


def seed_asset(
    pg: psycopg.Connection,
    *,
    user_id: str = "employee_1",
    kind: str = "material_video",
    content_type: str = "video/mp4",
    size_bytes: int = 1024,
    sha256: str = "deadbeef",
) -> str:
    asset_id = str(uuid4())
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)",
        (
            asset_id,
            kind,
            f"fake://publish-test/{asset_id}",
            sha256,
            size_bytes,
            content_type,
            user_id,
        ),
    )
    return asset_id


def record_row(pg: psycopg.Connection, record_id: str, columns: str) -> tuple[Any, ...]:
    row = pg.execute(
        f"SELECT {columns} FROM publish_records WHERE id = %s",  # noqa: S608 - fixed literal
        (record_id,),
    ).fetchone()
    assert row is not None, f"record {record_id} disappeared"
    return tuple(row)


def create_payload(account_id: str, video_id: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account_id": account_id,
        "video_material_id": f"asset:{video_id}",
        "title": "乡墅施工日记",
        "description": "主体之外，门窗、水电、防水和庭院，也要提前规划。",
        "tags": ["农村自建房", "#建房预算", "农村自建房"],
    }
    payload.update(extra)
    return payload


def seed_record(
    pg: psycopg.Connection,
    *,
    account_id: str,
    video_id: str,
    user_id: str = "employee_1",
    status: str = "queued",
    scheduled_at: datetime | None = None,
    attempt_count: int = 0,
    stats: dict[str, Any] | None = None,
) -> str:
    record_id = str(uuid4())
    account = pg.execute(
        "SELECT platform FROM publish_browser_accounts WHERE id = %s", (account_id,)
    ).fetchone()
    pg.execute(
        "INSERT INTO publish_records (id, user_id, account_id, platform, video_asset_id, "
        "title, status, scheduled_at, attempt_count, stats) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            record_id,
            user_id,
            account_id,
            account[0] if account else "douyin",
            video_id,
            "seeded",
            status,
            scheduled_at,
            attempt_count,
            json.dumps(stats) if stats is not None else None,
        ),
    )
    return record_id


@contextmanager
def open_txn() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw:
        yield BusinessConnection.postgres(raw)


# --------------------------------------------------------------------------- #
# R1–R8 HTTP lifecycle
# --------------------------------------------------------------------------- #


def test_create_immediate_record_queues_without_exposing_credentials(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account = seed_account(pg)
    video = seed_asset(pg)
    response = client.post(BASE, json=create_payload(account, video))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["platform"] == "douyin"
    assert body["account_id"] == account
    assert body["account_username"] == "创作者昵称"
    assert body["video_asset_id"] == video
    assert body["scheduled_at"] is None
    assert body["tags"] == ["农村自建房", "建房预算"]
    assert "synthetic-session" not in response.text and "storage" not in body
    assert record_row(pg, body["id"], "status, attempt_count") == ("queued", 0)


def test_create_scheduled_record_validates_lead_time(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account = seed_account(pg)
    video = seed_asset(pg)
    later = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    response = client.post(
        BASE, json=create_payload(account, video, scheduled_at=later.isoformat())
    )
    assert response.status_code == 200, response.text
    assert response.json()["scheduled_at"] == later.isoformat()

    other_video = seed_asset(pg)
    soon = datetime.now(UTC) + timedelta(seconds=30)
    response = client.post(
        BASE, json=create_payload(account, other_video, scheduled_at=soon.isoformat())
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PUBLISH_SCHEDULE_TOO_SOON"

    far = datetime.now(UTC) + timedelta(days=45)
    response = client.post(
        BASE, json=create_payload(account, other_video, scheduled_at=far.isoformat())
    )
    assert response.json()["detail"]["code"] == "PUBLISH_SCHEDULE_TOO_FAR"

    naive = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    response = client.post(BASE, json=create_payload(account, other_video, scheduled_at=naive))
    assert response.json()["detail"]["code"] == "PUBLISH_SCHEDULE_TIMEZONE_REQUIRED"


def test_create_rejects_duplicate_active_record_for_same_video_and_account(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account = seed_account(pg)
    video = seed_asset(pg)
    assert client.post(BASE, json=create_payload(account, video)).status_code == 200
    duplicate = client.post(BASE, json=create_payload(account, video))
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "PUBLISH_RECORD_DUPLICATE"
    # A different account for the same video is a separate delivery.
    other_account = seed_account(pg, platform="wechat_channels")
    assert client.post(BASE, json=create_payload(other_account, video)).status_code == 200


def test_create_enforces_account_platform_and_material_rules(
    client: TestClient, pg: psycopg.Connection
) -> None:
    video = seed_asset(pg)
    foreign_account = seed_account(pg, user_id="employee_2")
    response = client.post(BASE, json=create_payload(foreign_account, video))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PUBLISH_ACCOUNT_NOT_FOUND"

    xhs = seed_account(pg, platform="xiaohongshu")
    response = client.post(BASE, json=create_payload(xhs, video))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PUBLISH_PLATFORM_NOT_READY"

    invalid = seed_account(pg, status="invalid")
    response = client.post(BASE, json=create_payload(invalid, video))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PUBLISH_ACCOUNT_INVALID"

    account = seed_account(pg)
    foreign_video = seed_asset(pg, user_id="employee_2")
    response = client.post(BASE, json=create_payload(account, foreign_video))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "MATERIAL_NOT_FOUND"

    uploading = seed_asset(pg, size_bytes=0, sha256="")
    response = client.post(BASE, json=create_payload(account, uploading))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ASSET_UPLOAD_NOT_COMPLETE"

    image = seed_asset(pg, kind="material_image", content_type="image/png")
    response = client.post(BASE, json=create_payload(account, image))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PUBLISH_MATERIAL_TYPE_MISMATCH"

    response = client.post(
        BASE, json=create_payload(account, video, cover_material_id=f"asset:{video}")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PUBLISH_MATERIAL_TYPE_MISMATCH"

    response = client.post(
        BASE, json=create_payload(account, video, cover_material_id=f"asset:{image}")
    )
    assert response.status_code == 200, response.text
    assert response.json()["cover_asset_id"] == image


def test_auditor_cannot_create_records(
    client: TestClient, pg: psycopg.Connection, holder: _PgBusinessDb
) -> None:
    account = seed_account(pg)
    video = seed_asset(pg)
    holder.current_actor = actor("auditor_1", role="auditor")
    response = client.post(BASE, json=create_payload(account, video))
    assert response.status_code == 403
    assert pg.execute("SELECT count(*) FROM publish_records").fetchone()[0] == 0


def test_list_get_and_isolation(
    client: TestClient, pg: psycopg.Connection, holder: _PgBusinessDb
) -> None:
    account = seed_account(pg)
    video = seed_asset(pg)
    mine = seed_record(pg, account_id=account, video_id=video)
    published = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="published")
    other_account = seed_account(pg, user_id="employee_2")
    theirs = seed_record(
        pg,
        account_id=other_account,
        video_id=seed_asset(pg, user_id="employee_2"),
        user_id="employee_2",
    )

    listing = client.get(BASE)
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()["records"]}
    assert ids == {mine, published}
    queued = client.get(BASE, params={"status": "queued"}).json()["records"]
    assert [item["id"] for item in queued] == [mine]
    assert client.get(BASE, params={"status": "bogus"}).status_code == 422

    assert client.get(f"{BASE}/{mine}").json()["title"] == "seeded"
    assert client.get(f"{BASE}/{theirs}").status_code == 404
    holder.current_actor = actor("employee_2")
    assert client.get(f"{BASE}/{theirs}").status_code == 200
    assert client.get(f"{BASE}/{mine}").status_code == 404


def test_cancel_retry_sync_delete_follow_the_state_machine(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account = seed_account(pg)
    queued = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    failed = seed_record(
        pg, account_id=account, video_id=seed_asset(pg), status="failed", attempt_count=3
    )
    pg.execute("UPDATE publish_records SET error_message='boom' WHERE id=%s", (failed,))
    published = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="published")
    publishing = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="publishing")

    cancelled = client.post(f"{BASE}/{queued}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["record"]["status"] == "cancelled"
    assert client.post(f"{BASE}/{queued}/cancel").status_code == 409
    assert client.post(f"{BASE}/{publishing}/cancel").status_code == 409

    retried = client.post(f"{BASE}/{failed}/retry")
    assert retried.status_code == 200
    body = retried.json()["record"]
    assert (body["status"], body["attempt_count"], body["error_message"]) == ("queued", 0, None)
    assert client.post(f"{BASE}/{published}/retry").status_code == 409

    synced = client.post(f"{BASE}/{published}/sync")
    assert synced.status_code == 200
    assert synced.json()["record"]["sync_requested"] is True
    assert client.post(f"{BASE}/{failed}/sync").status_code == 409

    assert client.delete(f"{BASE}/{failed}").status_code == 409  # now queued again
    assert client.delete(f"{BASE}/{publishing}").status_code == 409
    assert client.delete(f"{BASE}/{queued}").json() == {"deleted": True}
    assert client.get(f"{BASE}/{queued}").status_code == 404


def test_summary_counts_and_stat_totals(client: TestClient, pg: psycopg.Connection) -> None:
    account = seed_account(pg)
    seed_record(pg, account_id=account, video_id=seed_asset(pg))
    seed_record(pg, account_id=account, video_id=seed_asset(pg), status="publishing")
    seed_record(pg, account_id=account, video_id=seed_asset(pg), status="failed")
    seed_record(
        pg,
        account_id=account,
        video_id=seed_asset(pg),
        status="published",
        stats={"play_count": 120, "like_count": 7},
    )
    seed_record(
        pg,
        account_id=account,
        video_id=seed_asset(pg),
        status="published",
        stats={"play_count": "n/a"},
    )
    foreign = seed_account(pg, user_id="employee_2")
    seed_record(
        pg,
        account_id=foreign,
        video_id=seed_asset(pg, user_id="employee_2"),
        user_id="employee_2",
        status="published",
        stats={"play_count": 999},
    )
    assert client.get(f"{BASE}/summary").json() == {
        "published_total": 2,
        "queued_total": 2,
        "failed_total": 1,
        "play_total": 120,
        "like_total": 7,
    }


# --------------------------------------------------------------------------- #
# W1–W7 worker claims and finalizers
# --------------------------------------------------------------------------- #


def test_claim_respects_schedule_order_and_lease(pg: psycopg.Connection, lane_env: str) -> None:
    account = seed_account(pg)
    future = seed_record(
        pg,
        account_id=account,
        video_id=seed_asset(pg),
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    due_later = seed_record(
        pg,
        account_id=account,
        video_id=seed_asset(pg),
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    due_first = seed_record(
        pg,
        account_id=account,
        video_id=seed_asset(pg),
        scheduled_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="w1")
    assert work is not None and work.record_id == due_first
    assert work.attempt_count == 1
    assert work.row["video_uri"].startswith("fake://")
    assert "storage_state_enc" in work.row
    status, owner, expires = record_row(pg, due_first, "status, lease_owner, lease_expires_at")
    assert status == "publishing" and owner == work.lease_token and expires is not None
    # Same account is now busy: the second due record waits, the future one never qualifies.
    with open_txn() as conn:
        assert claim_publish_work(conn, worker_id="w2") is None
    assert record_row(pg, due_later, "status") == ("queued",)
    assert record_row(pg, future, "status") == ("queued",)


def test_claim_serializes_per_account_but_parallelizes_across_accounts(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account_a = seed_account(pg)
    account_b = seed_account(pg, platform="wechat_channels")
    first_a = seed_record(pg, account_id=account_a, video_id=seed_asset(pg))
    seed_record(pg, account_id=account_a, video_id=seed_asset(pg))
    only_b = seed_record(pg, account_id=account_b, video_id=seed_asset(pg))
    with open_txn() as conn:
        first = claim_publish_work(conn, worker_id="w1")
    with open_txn() as conn:
        second = claim_publish_work(conn, worker_id="w2")
    with open_txn() as conn:
        third = claim_publish_work(conn, worker_id="w3")
    assert first is not None and first.record_id == first_a
    assert second is not None and second.record_id == only_b
    assert third is None


def test_claim_skips_rows_locked_by_a_concurrent_claimer(
    pg: psycopg.Connection, cw068_dsn: str, lane_env: str
) -> None:
    account = seed_account(pg)
    seed_record(pg, account_id=account, video_id=seed_asset(pg))
    first = psycopg.connect(cw068_dsn)
    second = psycopg.connect(cw068_dsn, options="-c lock_timeout=5000")
    try:
        first_conn = BusinessConnection.postgres(first)
        with first:
            first.execute(
                "SELECT id FROM publish_records WHERE status='queued' FOR UPDATE"
            ).fetchone()
            assert claim_publish_work(BusinessConnection.postgres(second), worker_id="w2") is None
        del first_conn
    finally:
        first.close()
        second.close()
    with open_txn() as conn:
        assert claim_publish_work(conn, worker_id="w3") is not None


def test_finalize_published_writes_platform_result(pg: psycopg.Connection, lane_env: str) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="w1")
    assert work is not None
    with open_txn() as conn:
        outcome = finalize_publish_work(
            conn,
            work=work,
            result=PublishResult(
                platform="douyin",
                status="published",
                item_id="7300000001",
                short_url="https://v.douyin.com/x",
            ),
            delivery_mode="api",
        )
    assert outcome == "published"
    row = record_row(
        pg,
        record,
        "status, platform_item_id, platform_short_url, delivery_mode, published_at, lease_owner",
    )
    assert row[:4] == ("published", "7300000001", "https://v.douyin.com/x", "api")
    assert row[4] is not None and row[5] is None
    # The lease is spent: a replay of the same finalize must not silently succeed.
    with open_txn() as conn, pytest.raises(PublishLeaseLostError):
        finalize_publish_work(
            conn, work=work, result=PublishResult(platform="douyin", status="published")
        )


def test_finalize_transient_failure_requeues_until_attempts_exhausted(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pg.execute("UPDATE publish_records SET scheduled_at = NULL WHERE id = %s", (record,))
        with open_txn() as conn:
            work = claim_publish_work(conn, worker_id="w1")
        assert work is not None and work.attempt_count == attempt
        with open_txn() as conn:
            outcome = finalize_publish_work(
                conn,
                work=work,
                result=PublishResult(platform="douyin", status="failed", message="平台 5xx"),
                delivery_mode="api",
            )
        status, message, scheduled = record_row(pg, record, "status, error_message, scheduled_at")
        if attempt < MAX_ATTEMPTS:
            assert outcome == "queued" and status == "queued"
            assert message == "平台 5xx（将自动重试）"
            assert scheduled is not None and scheduled > datetime.now(UTC) + timedelta(minutes=4)
        else:
            assert outcome == "failed" and status == "failed" and message == "平台 5xx"
            assert scheduled is None
    assert record_row(pg, record, "attempt_count") == (MAX_ATTEMPTS,)


def test_finalize_account_invalid_marks_account_and_fails_record(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    sibling = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="w1")
    assert work is not None
    with open_txn() as conn:
        outcome = finalize_publish_work(
            conn,
            work=work,
            result=PublishResult(
                platform="douyin", status="failed", message="登录已过期", account_invalid=True
            ),
            delivery_mode="api",
        )
    assert outcome == "failed"
    assert record_row(pg, record, "status, error_message") == ("failed", "登录已过期")
    assert pg.execute(
        "SELECT status, error_message FROM publish_browser_accounts WHERE id=%s", (account,)
    ).fetchone() == ("invalid", "登录已过期")
    # The sibling can no longer deliver: the next claim round fails it instead of hanging.
    with open_txn() as conn:
        assert claim_publish_work(conn, worker_id="w2") is None
    assert record_row(pg, sibling, "status, error_message") == ("failed", ACCOUNT_GONE_MESSAGE)


def test_expired_publishing_lease_is_requeued_and_unbound_records_fail(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg)
    stuck = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="publishing")
    pg.execute(
        "UPDATE publish_records SET lease_owner='dead', "
        "lease_expires_at=clock_timestamp() - interval '1 minute' WHERE id=%s",
        (stuck,),
    )
    live = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="publishing")
    pg.execute(
        "UPDATE publish_records SET lease_owner='alive', "
        "lease_expires_at=clock_timestamp() + interval '10 minutes' WHERE id=%s",
        (live,),
    )
    orphan_account = seed_account(pg)
    orphan = seed_record(pg, account_id=orphan_account, video_id=seed_asset(pg))
    pg.execute("DELETE FROM publish_browser_accounts WHERE id=%s", (orphan_account,))
    with open_txn() as conn:
        # The live lease blocks the same account, so nothing is claimable this round.
        assert claim_publish_work(conn, worker_id="w1") is None
    assert record_row(pg, stuck, "status, lease_owner, error_message") == (
        "queued",
        None,
        QUARANTINE_MESSAGE,
    )
    assert record_row(pg, live, "status, lease_owner") == ("publishing", "alive")
    assert record_row(pg, orphan, "status, account_id, error_message") == (
        "failed",
        None,
        ACCOUNT_GONE_MESSAGE,
    )


# --------------------------------------------------------------------------- #
# W8–W10 run_publish_round end to end with a fake delivery
# --------------------------------------------------------------------------- #


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def iter_object(self, key: str, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        self.keys.append(key)
        yield b"\x00\x00\x00\x18ftypmp42"
        yield b"fake-video-bytes"


def test_run_publish_round_delivers_and_finalizes(
    pg: psycopg.Connection, lane_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = seed_account(pg)
    cover = seed_asset(pg, kind="material_image", content_type="image/jpeg")
    video = seed_asset(pg)
    record = seed_record(pg, account_id=account, video_id=video)
    pg.execute(
        "UPDATE publish_records SET cover_asset_id=%s, tags='[\"自建房\"]'::jsonb, "
        "options='{\"visibility\": 2}'::jsonb WHERE id=%s",
        (cover, record),
    )
    storage = _FakeStorage()
    seen: dict[str, Any] = {}

    def fake_storage_for_asset(conn: Any, uri: str) -> _FakeStorage:
        seen["uri"] = uri
        return storage

    def fake_deliver(platform: str, storage_state: dict[str, Any], **kwargs: Any) -> Any:
        seen["platform"] = platform
        seen["state"] = storage_state
        seen["kwargs"] = kwargs
        assert kwargs["video_path"].read_bytes().endswith(b"fake-video-bytes")
        assert kwargs["cover_path"] is not None and kwargs["cover_path"].suffix == ".jpg"
        return publish_delivery.DeliveryOutcome(
            PublishResult(platform=platform, status="published", item_id="item-1"), "api"
        )

    import app.media_routes as media_routes

    monkeypatch.setattr(media_routes, "storage_for_asset", fake_storage_for_asset)
    monkeypatch.setattr(publish_delivery, "deliver", fake_deliver)
    fernet = Fernet(_TEST_KEY.encode())
    assert worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=fernet) == 1
    assert seen["platform"] == "douyin"
    assert seen["state"] == _STORAGE_STATE
    assert seen["kwargs"]["tags"] == ["自建房"]
    assert seen["kwargs"]["options"] == {"visibility": 2}
    assert seen["kwargs"]["title"] == "seeded"
    assert storage.keys == [video, cover]
    assert record_row(pg, record, "status, platform_item_id, delivery_mode") == (
        "published",
        "item-1",
        "api",
    )
    assert not seen["kwargs"]["video_path"].exists()  # temp media cleaned up
    assert worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=fernet) == 0


def test_run_publish_round_unreadable_credentials_invalidate_account(
    pg: psycopg.Connection, lane_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = seed_account(pg)
    pg.execute(
        "UPDATE publish_browser_accounts SET storage_state_enc='not-fernet' WHERE id=%s",
        (account,),
    )
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("delivery must not run without credentials")

    monkeypatch.setattr(publish_delivery, "deliver", never)
    assert (
        worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=Fernet(_TEST_KEY.encode()))
        == 1
    )
    assert record_row(pg, record, "status") == ("failed",)
    assert pg.execute(
        "SELECT status FROM publish_browser_accounts WHERE id=%s", (account,)
    ).fetchone() == ("invalid",)


def test_run_publish_round_storage_outage_requeues(
    pg: psycopg.Connection, lane_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    import app.media_routes as media_routes

    def broken(conn: Any, uri: str) -> Any:
        raise RuntimeError("cos misconfigured")

    monkeypatch.setattr(media_routes, "storage_for_asset", broken)
    assert (
        worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=Fernet(_TEST_KEY.encode()))
        == 1
    )
    status, message, attempts = record_row(pg, record, "status, error_message, attempt_count")
    assert status == "queued" and attempts == 1
    assert message == "存储服务暂不可用，稍后自动重试（将自动重试）"


def test_finalize_after_lease_loss_is_reported_not_written(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="w1")
    assert work is not None
    stale = PublishWork(
        record_id=work.record_id,
        worker_id="w1",
        lease_token="someone-else",
        attempt_count=work.attempt_count,
        row=work.row,
    )
    with open_txn() as conn, pytest.raises(PublishLeaseLostError):
        finalize_publish_work(
            conn, work=stale, result=PublishResult(platform="douyin", status="published")
        )
    assert record_row(pg, record, "status") == ("publishing",)
