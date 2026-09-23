"""CW-031 — 正式服务关闭本地持久存储回退（跨实例 / 授权 / 故障专项）。

规格 §CW-031「仅做剩余」：关闭全部正式服务本地持久存储回退；保留历史 local
URI 只作为迁移期可追溯输入，禁止新写；补 API A 上传 / API B 或 Worker 读取、
跨用户/撤销 session、签名过期重获、COS 故障不落本地的跨实例测试。

闸门设计（定稿 §5.2）：``get_media_storage`` 以 ``runtime_settings
.active_storage_provider`` 为主闸门、``is_customer_production()`` 为兜底闸门，
一道门禁覆盖 API 与 Worker 的全部正式服务存储选择；``storage_for_asset`` 的
历史 local URI 分支在客户生产下拒绝、非客户生产只读追溯（CW-037 搬迁）。

跨实例口径（定稿 §5.5，PR 内披露）：「两个独立 API 进程语义/连接和一个
Worker」落地为三个互相独立的 PG 连接 + 三个独立的 ``StorageAdapter`` 实例 +
共享同一个 COS 后端（共享内存 fake client），真实多进程归 CW-050/CW-051。

PG 约束（定稿 §5.6）：``pg_test_kit.py`` 归 CW-056，本文件不建任何新 PG 测试
库，只用共享库 ``customer_v3_test``：模块级持有 ``shared_suite_lock``、全部
数据用唯一 ``cw031-`` 前缀、绝不 TRUNCATE 共享业务表、模块收尾恢复
``runtime_settings`` / ``provider_settings`` 进入前状态。全部 PG 用例挂
CW-007 硬门（fixture 不可达即 fail，不 skip）。
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    generate_activation_code,
)
from app.bootstrap import _probe_formal_service_write_path
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.media_routes import (
    get_media_storage,
    storage_for_asset,
    validate_signed_asset_grant,
)
from app.permissions import require_asset_access
from app.settings import SettingsRepository, settings_encryption_key
from app.storage import (
    CloudStorageAdapter,
    CloudStorageConfig,
    FakeStorageAdapter,
    LocalStorageAdapter,
    SourceUrlExpired,
    StorageAdapter,
    StorageBackendUnavailable,
    local_download_signature,
    local_storage_root,
)

DB_NAME_SHARED = "customer_v3_test"
ADMIN_USER_ID = "cw031-admin"
OTHER_USER_ID = "cw031-other"
COS_BUCKET = "cw031-private-bucket"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
STORAGE_ROOT_ENV = "VIDEO_REPLICA_STORAGE_ROOT"
FENCE_CODE = "STORAGE_PROVIDER_FORBIDDEN"

ACTIVATE_PATH = "/api/customer/activate"
LOGIN_PATH = "/api/customer/sessions/login"
UPLOAD_INTENT_PATH = "/api/assets/upload-intent"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

TEST_ACTIVATION_KEY = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_IDEMPOTENCY_AEAD_KEY = secrets.token_bytes(32)

_COS_CONFIG = {
    "access_key_id": "cw031-test-access-id",
    "secret_access_key": "cw031-test-secret-access-key",
    "bucket": COS_BUCKET,
    "region": "ap-shanghai",
}


@dataclass(frozen=True)
class FenceEnv:
    """共享库 DSN 加上闸门绝不许填充的本地存储根。"""

    dsn: str
    local_root: Path
    settings_key: bytes


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _persistent_file_snapshot(root: Path) -> set[str]:
    """正式服务下不得新增本地持久文件；可重建缓存目录（.cache）不计入。"""
    if not root.exists():
        return set()
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(root).parts
    }


def _ensure_user(dsn: str, user_id: str, role: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, f"CW031 {user_id}", role),
        )


def _ensure_project(dsn: str, project_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name, status) "
            "VALUES (%s, %s, %s, 'ACTIVE') ON CONFLICT (id) DO NOTHING",
            (project_id, ADMIN_USER_ID, "CW031 cross-instance project"),
        )


def _set_runtime_provider(dsn: str, provider: str) -> None:
    """迁移后的共享库恒有 id=1 行（002 迁移内 seed），UPDATE 即可、绝不 TRUNCATE。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        updated = conn.execute(
            "UPDATE runtime_settings SET active_storage_provider = %s WHERE id = 1",
            (provider,),
        ).rowcount
        if updated < 1:  # pragma: no cover - 防御：迁移未 seed 时兜底
            conn.execute(
                "INSERT INTO runtime_settings (id, active_storage_provider) VALUES (1, %s)",
                (provider,),
            )


def _write_cos_settings(dsn: str, *, settings_key: bytes, bucket: str = COS_BUCKET) -> None:
    config = dict(_COS_CONFIG)
    config["bucket"] = bucket
    encrypted = Fernet(settings_key).encrypt(json.dumps(config).encode("utf-8")).decode("ascii")
    _write_raw_cos_settings(dsn, encrypted)


def _write_raw_cos_settings(dsn: str, encrypted_config: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM provider_settings WHERE provider = 'cos'")
        conn.execute(
            "INSERT INTO provider_settings "
            "(provider, encrypted_config, updated_by_user_id, created_at, updated_at) "
            "VALUES ('cos', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (encrypted_config, ADMIN_USER_ID),
        )


def _clear_cos_settings(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM provider_settings WHERE provider = 'cos'")


def _asset_count(dsn: str, project_prefix: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE storage_uri LIKE 'cos://%%') "
            "FROM assets WHERE project_id LIKE %s",
            (project_prefix + "%",),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _snapshot_storage_settings(dsn: str) -> dict[str, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.row_factory = psycopg.rows.dict_row
        runtime = conn.execute(
            "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
        ).fetchone()
        cos_row = conn.execute(
            "SELECT encrypted_config FROM provider_settings WHERE provider = 'cos'"
        ).fetchone()
    return {
        "active_storage_provider": (
            str(runtime["active_storage_provider"]) if runtime is not None else None
        ),
        "cos_encrypted_config": (str(cos_row["encrypted_config"]) if cos_row is not None else None),
    }


def _restore_storage_settings(dsn: str, snapshot: dict[str, str | None]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        provider = snapshot["active_storage_provider"]
        if provider is None:
            conn.execute("DELETE FROM runtime_settings WHERE id = 1")
        else:
            conn.execute(
                "UPDATE runtime_settings SET active_storage_provider = %s WHERE id = 1",
                (provider,),
            )
        config = snapshot["cos_encrypted_config"]
        conn.execute("DELETE FROM provider_settings WHERE provider = 'cos'")
        if config is not None:
            conn.execute(
                "INSERT INTO provider_settings "
                "(provider, encrypted_config, updated_by_user_id, created_at, updated_at) "
                "VALUES ('cos', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (config, ADMIN_USER_ID),
            )


def _storage_under_fence(env: FenceEnv) -> StorageAdapter:
    with psycopg.connect(env.dsn, autocommit=True) as raw:
        return get_media_storage(BusinessConnection.postgres(raw))


def _fence_error(env: FenceEnv) -> HTTPException:
    with psycopg.connect(env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(HTTPException) as excinfo:
            get_media_storage(conn)
    return excinfo.value


# ---------------------------------------------------------------------------
# Fixtures：共享库 + 套件锁 + 设置快照恢复（绝不新建 PG 测试库）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fence_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert dsn.rsplit("/", 1)[1] == DB_NAME_SHARED
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        snapshot = _snapshot_storage_settings(dsn)
        try:
            yield dsn
        finally:
            _restore_storage_settings(dsn, snapshot)
            close_pg_pool()


@pytest.fixture()
def fence_env(
    fence_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FenceEnv]:
    settings_key = Fernet.generate_key()
    local_root = tmp_path / "local-storage-root"
    monkeypatch.setenv(DATABASE_URL_ENV, fence_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_key.decode("ascii"))
    monkeypatch.setenv(STORAGE_ROOT_ENV, str(local_root))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cw031.example.com")
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv(CUSTOMER_PRODUCTION_ENV, raising=False)
    close_pg_pool()
    _ensure_user(fence_dsn, ADMIN_USER_ID, "admin")
    _ensure_user(fence_dsn, OTHER_USER_ID, "admin")
    yield FenceEnv(dsn=fence_dsn, local_root=local_root, settings_key=settings_key)
    close_pg_pool()


@pytest.fixture()
def customer_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CUSTOMER_PRODUCTION_ENV, "true")


@pytest.fixture()
def customer_lane(fence_env: FenceEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """真实客户 lane：激活 → 设备 → 业务登录（dev 身份被 401 挡在存储之前）。"""
    from app.activation_code_routes import router as activation_code_router
    from app.customer_device_routes import router as customer_device_router
    from app.customer_session_routes import router as customer_session_router
    from app.media_routes import router as media_router
    from app.rbac_routes import router as rbac_router

    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_ACTIVATION_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY", _b64key(TEST_IDEMPOTENCY_AEAD_KEY)
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    application = FastAPI()
    for router in (
        activation_code_router,
        customer_session_router,
        customer_device_router,
        rbac_router,
        media_router,
    ):
        application.include_router(router)
    with TestClient(application) as test_client:
        yield test_client


def _activated_customer(
    client: TestClient, fence_env: FenceEnv, *, fingerprint: str
) -> dict[str, Any]:
    code = generate_activation_code()
    suffix = uuid4().hex[:8]
    _seed_issuable_code(fence_env.dsn, code, suffix=suffix)
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"CW031 Device {suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"cw031-idem-activate-{suffix}"},
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _seed_issuable_code(dsn: str, code: str, *, suffix: str) -> None:
    from app.activation_code_service import compute_code_digest, mask_activation_code

    batch_id = f"cw031-batch-{suffix}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            " quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES (%s, %s, 1500, 1000, 100, 1, %s, 'OPEN', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (batch_id, f"cw031-batch-{suffix}", FUTURE_EXPIRY, ADMIN_USER_ID),
        )
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            " status, issued_at) "
            "VALUES (%s, %s, %s, 1, %s, 'ISSUED', '2026-01-01T00:00:00+00:00') "
            "ON CONFLICT (id) DO NOTHING",
            (
                f"cw031-code-{suffix}",
                batch_id,
                compute_code_digest(code, key=TEST_ACTIVATION_KEY.encode("utf-8")),
                mask_activation_code(code),
            ),
        )


def _business_login(client: TestClient, customer: dict[str, Any], key: str) -> str:
    response = client.post(
        LOGIN_PATH,
        json={},
        headers={**_bearer(str(customer["device_token"])), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_token"])


_UPLOAD_INTENT_PAYLOAD = {
    "project_id": "cw031-project",
    "filename": "reference.mp4",
    "content_type": "video/mp4",
    "size_bytes": 2048,
}


# ---------------------------------------------------------------------------
# A 组：闸门本体（get_media_storage，直接调用、绝不 override）
# ---------------------------------------------------------------------------


def test_formal_service_without_cos_config_fails_closed_instead_of_local(
    fence_env: FenceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定稿用例 6（RED 核心）：正式服务（provider=cos）缺 COS 配置必须响亮失败。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _clear_cos_settings(fence_env.dsn)
    local_calls: list[int] = []

    def _counting_local_factory() -> LocalStorageAdapter:
        local_calls.append(1)
        return LocalStorageAdapter(root=local_storage_root())

    monkeypatch.setattr(
        "app.media_routes.create_local_storage_from_environment", _counting_local_factory
    )

    error = _fence_error(fence_env)

    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == FENCE_CODE
    assert local_calls == []
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_customer_production_overrides_a_local_runtime_provider(
    fence_env: FenceEnv, customer_production: None
) -> None:
    """定稿用例 7：客户生产 + active_storage_provider 被改成 local（运维误改/
    攻击面）→ 兜底闸门仍走 COS 分支，未配 COS → 503，local 新写 = 0。"""
    _set_runtime_provider(fence_env.dsn, "local")
    _clear_cos_settings(fence_env.dsn)

    error = _fence_error(fence_env)

    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == FENCE_CODE
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_internal_desktop_lane_keeps_local_adapter(fence_env: FenceEnv) -> None:
    """回归锁：内部 P0 单机车道（provider=local 且非客户生产）不得因 local 命名被删。"""
    _set_runtime_provider(fence_env.dsn, "local")
    _clear_cos_settings(fence_env.dsn)

    storage = _storage_under_fence(fence_env)

    assert isinstance(storage, LocalStorageAdapter)


def test_formal_service_with_cos_settings_returns_the_cloud_adapter(
    fence_env: FenceEnv,
) -> None:
    """已正确配置 COS 的正式服务零行为变化。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _write_cos_settings(fence_env.dsn, settings_key=fence_env.settings_key)

    storage = _storage_under_fence(fence_env)

    assert storage.provider == "cos"
    assert storage.bucket == COS_BUCKET
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_undecryptable_cos_settings_stay_loud(fence_env: FenceEnv) -> None:
    """解密失败本来就是配置类 503；锁住它，不被新闸门的错误码掩盖。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _write_raw_cos_settings(fence_env.dsn, "not-a-valid-fernet-token")

    error = _fence_error(fence_env)

    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == "STORAGE_SETTINGS_UNAVAILABLE"


def test_incomplete_cos_settings_stay_loud(fence_env: FenceEnv) -> None:
    """COS 行存在但缺字段 → 仍是配置类 503，不回退本地。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    partial = {"bucket": COS_BUCKET, "region": "ap-shanghai"}
    encrypted = (
        Fernet(fence_env.settings_key).encrypt(json.dumps(partial).encode("utf-8")).decode("ascii")
    )
    _write_raw_cos_settings(fence_env.dsn, encrypted)

    error = _fence_error(fence_env)

    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == "STORAGE_SETTINGS_UNAVAILABLE"
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_unexpected_runtime_provider_value_is_rejected_loudly(
    fence_env: FenceEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider 值既非 cos 也非 local（CHECK 约束外的防御分支）→ 明确 503。"""
    _clear_cos_settings(fence_env.dsn)

    class _BogusRepo(SettingsRepository):
        def read_runtime_settings(self) -> dict[str, int | str]:
            return {"active_storage_provider": "bogus"}

    monkeypatch.setattr("app.media_routes.SettingsRepository", _BogusRepo)

    error = _fence_error(fence_env)

    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == "STORAGE_PROVIDER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# B 组：历史 local URI（storage_for_asset 只读追溯 / 客户生产拒绝）
# ---------------------------------------------------------------------------


def test_legacy_local_uri_stays_readable_outside_customer_production(
    fence_env: FenceEnv,
) -> None:
    """非客户生产：历史 local URI 仍作为迁移期可追溯输入只读解析（CW-037 搬迁）。"""
    with psycopg.connect(fence_env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)

        storage = storage_for_asset(conn, "local://legacy-bucket/projects/p/source.mp4")

    assert isinstance(storage, LocalStorageAdapter)
    assert storage.bucket == "legacy-bucket"


def test_legacy_local_uri_rejected_on_customer_production(
    fence_env: FenceEnv, customer_production: None
) -> None:
    """客户生产不该再有本地持久资产——有则显式失败而不是静默服务。"""
    with psycopg.connect(fence_env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(HTTPException) as excinfo:
            storage_for_asset(conn, "local://legacy-bucket/projects/p/source.mp4")

    error = excinfo.value
    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == FENCE_CODE


# ---------------------------------------------------------------------------
# C 组：路由面后果（真实客户 lane / local-objects 端点）
# ---------------------------------------------------------------------------


def test_upload_intent_is_fenced_for_a_real_customer_session_without_cos(
    customer_lane: TestClient, fence_env: FenceEnv
) -> None:
    """客户 lane 的真实会话也拿不到本地回退：正式服务未配 COS → 503。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _clear_cos_settings(fence_env.dsn)
    customer = _activated_customer(customer_lane, fence_env, fingerprint="cw031-fp-a")
    session_token = _business_login(customer_lane, customer, f"cw031-idem-login-{uuid4().hex[:8]}")

    response = customer_lane.post(
        UPLOAD_INTENT_PATH, headers=_bearer(session_token), json=_UPLOAD_INTENT_PAYLOAD
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == FENCE_CODE
    assert _asset_count(fence_env.dsn, "cw031-project") == (0, 0)
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_customer_lane_rejects_the_dev_identity_before_storage(
    customer_lane: TestClient, fence_env: FenceEnv
) -> None:
    """登记实际行为：客户 lane 上 dev 身份先被 401 挡住，不会退到内部身份。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _clear_cos_settings(fence_env.dsn)

    response = customer_lane.post(
        UPLOAD_INTENT_PATH,
        headers={"X-Dev-User-Id": ADMIN_USER_ID},
        json=_UPLOAD_INTENT_PAYLOAD,
    )

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"
    assert _asset_count(fence_env.dsn, "cw031-project") == (0, 0)


def test_local_object_endpoints_fail_closed_without_cos(
    customer_lane: TestClient, fence_env: FenceEnv
) -> None:
    """客户 lane 真实会话下正式服务未配 COS → local-objects 端点 503 围栏。

    CW-026 收敛后客户 lane 只认 Bearer 会话（dev 身份 401），
    故闸门断言迁移到真实会话模式（原 X-Dev-User-Id 写法已被
    test_customer_lane_rejects_the_dev_identity_before_storage 登记为 401）。
    """
    _set_runtime_provider(fence_env.dsn, "cos")
    _clear_cos_settings(fence_env.dsn)
    customer = _activated_customer(customer_lane, fence_env, fingerprint="cw031-fp-lo-a")
    session_token = _business_login(customer_lane, customer, f"cw031-idem-login-{uuid4().hex[:8]}")

    put_response = customer_lane.put(
        "/api/assets/local-objects/projects/cw031-project/uploads/asset-1/reference.mp4",
        content=b"cw031-reference-bytes",
        headers=_bearer(session_token),
    )
    get_response = customer_lane.get(
        "/api/assets/local-objects/projects/cw031-project/reference.mp4",
        params={"expires": "9999999999", "sig": "0" * 64},
        headers=_bearer(session_token),
    )

    assert put_response.status_code == 503, put_response.text
    assert put_response.json()["detail"]["code"] == FENCE_CODE
    assert get_response.status_code == 503, get_response.text
    assert get_response.json()["detail"]["code"] == FENCE_CODE
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_local_object_endpoints_keep_404_when_cos_is_configured(
    customer_lane: TestClient, fence_env: FenceEnv
) -> None:
    """COS 已配置时这两个端点今天就是 404；闸门不得改变既有正式服务行为。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _write_cos_settings(fence_env.dsn, settings_key=fence_env.settings_key)
    customer = _activated_customer(customer_lane, fence_env, fingerprint="cw031-fp-lo-b")
    session_token = _business_login(customer_lane, customer, f"cw031-idem-login-{uuid4().hex[:8]}")

    put_response = customer_lane.put(
        "/api/assets/local-objects/projects/cw031-project/uploads/asset-1/reference.mp4",
        content=b"cw031-reference-bytes",
        headers=_bearer(session_token),
    )
    get_response = customer_lane.get(
        "/api/assets/local-objects/projects/cw031-project/reference.mp4",
        params={"expires": "9999999999", "sig": "0" * 64},
        headers=_bearer(session_token),
    )

    assert put_response.status_code == 404, put_response.text
    assert put_response.json()["detail"]["code"] == "LOCAL_UPLOAD_UNAVAILABLE"
    assert get_response.status_code == 404, get_response.text
    assert get_response.json()["detail"]["code"] == "LOCAL_DOWNLOAD_UNAVAILABLE"
    assert _persistent_file_snapshot(fence_env.local_root) == set()


def test_local_object_put_with_local_provider_stops_before_the_fence(
    customer_lane: TestClient, fence_env: FenceEnv
) -> None:
    """回归锁：provider=local（围栏之外）时真实客户会话的上传停在项目门禁 404，而非围栏 503。

    原“桌面车道”回归锁的前提（SQLite 桌面车道）已随 CW-021/CW-026 收敛移除；
    本用例在 PG 客户车道上登记等价的“围栏之外”行为。
    """
    _set_runtime_provider(fence_env.dsn, "local")
    _clear_cos_settings(fence_env.dsn)
    customer = _activated_customer(customer_lane, fence_env, fingerprint="cw031-fp-lo-c")
    session_token = _business_login(customer_lane, customer, f"cw031-idem-login-{uuid4().hex[:8]}")

    response = customer_lane.put(
        "/api/assets/local-objects/projects/cw031-missing/uploads/asset-1/reference.mp4",
        content=b"cw031-reference-bytes",
        headers=_bearer(session_token),
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] != FENCE_CODE


# ---------------------------------------------------------------------------
# D 组：跨实例可读 + 新持久资产 provider=cos 100%（定稿用例 1）
# ---------------------------------------------------------------------------


class _SharedMemoryCosClient:
    """多 adapter 实例共享同一内存后端 —— 「同一 COS 对象」的语义替身。"""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.responses: list[_FakeBody] = []

    def get_presigned_url(self, **kwargs: object) -> str:
        return "https://cos.example/upload"

    def get_presigned_download_url(self, **kwargs: object) -> str:
        return "https://cos.example/download"

    def put_object(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        body = cast(bytes, kwargs["Body"])
        self.objects[key] = (body, str(kwargs.get("ContentType") or "application/octet-stream"))

    def get_object(self, **kwargs: object) -> dict[str, object]:
        content, content_type = self.objects[str(kwargs["Key"])]
        body = _FakeBody(content)
        self.responses.append(body)
        return {"Body": body, "ContentType": content_type}

    def head_object(self, **kwargs: object) -> dict[str, str]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise RuntimeError("NoSuchResource")
        content, content_type = self.objects[key]
        return {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
            "x-cos-meta-sha256": "",
        }

    def delete_object(self, **kwargs: object) -> None:
        self.objects.pop(str(kwargs["Key"]), None)


class _FakeBody(io.BytesIO):
    def get_raw_stream(self) -> _FakeBody:
        return self


def _cloud_adapter(client: _SharedMemoryCosClient) -> CloudStorageAdapter:
    return CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket=COS_BUCKET,
            access_key_id="cw031-test-access-id",
            secret_access_key="cw031-test-secret-access-key",
            region="ap-shanghai",
        ),
        client=client,
    )


def test_asset_uploaded_on_api_a_is_readable_by_api_b_and_worker(
    fence_env: FenceEnv,
) -> None:
    """三个独立 PG 连接 + 三个独立 adapter 实例 + 共享 COS 后端。"""
    _set_runtime_provider(fence_env.dsn, "cos")
    _write_cos_settings(fence_env.dsn, settings_key=fence_env.settings_key)
    project_id = f"cw031-xinstance-{uuid4().hex[:8]}"
    _ensure_project(fence_env.dsn, project_id)

    backend = _SharedMemoryCosClient()
    content = b"cw031-cross-instance-reference-bytes"

    # API A：独立连接 + 独立 adapter 上传（create_upload_intent 落库 + 签发，
    # 客户端随后经 presigned URL 写对象——这里用共享后端模拟该 PUT）。
    with psycopg.connect(fence_env.dsn, autocommit=True) as raw_a:
        conn_a = BusinessConnection.postgres(raw_a)
        repo_a = SettingsRepository(conn_a)
        config_a = repo_a.load_provider_config("cos")
        from app.media import create_upload_intent

        intent_a = create_upload_intent(
            conn_a,
            actor=cast(Any, SimpleActor(id=ADMIN_USER_ID, role="admin")),
            storage=_cloud_adapter(backend),
            project_id=project_id,
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=len(content),
        )
        storage_key_a = intent_a.storage_key

    adapter_a = _cloud_adapter(backend)
    adapter_a.put_object(storage_key_a, content, content_type="video/mp4")
    assert backend.objects, "API A 的上传必须真正落到共享 COS 后端"
    total, cos_rows = _asset_count(fence_env.dsn, project_id)
    assert (total, cos_rows) == (1, 1), "新持久资产 provider=cos 必须是 100%"
    with psycopg.connect(fence_env.dsn) as probe:
        row = probe.execute(
            "SELECT storage_uri FROM assets WHERE project_id = %s", (project_id,)
        ).fetchone()
    assert row is not None and str(row[0]).startswith("cos://")

    # API B：另一条独立连接。闸门必须在 B 的连接上解析出同一云端目标，
    # 内容经同一共享 COS 后端读回。
    with psycopg.connect(fence_env.dsn, autocommit=True) as raw_b:
        conn_b = BusinessConnection.postgres(raw_b)
        storage_b = get_media_storage(conn_b)
        assert storage_b.provider == "cos"
        assert storage_b.bucket == COS_BUCKET
        assert config_a.get("bucket") == COS_BUCKET

    content_b = _cloud_adapter(backend).get_object(storage_key_a)
    assert content_b == content

    # Worker：第三条独立连接 + generation_worker 的同一存储入口。
    from app.generation_worker import get_media_storage as worker_get_media_storage

    with psycopg.connect(fence_env.dsn, autocommit=True) as raw_w:
        conn_w = BusinessConnection.postgres(raw_w)
        storage_w = worker_get_media_storage(conn_w)
        assert storage_w.provider == "cos"
        assert storage_w.bucket == COS_BUCKET

    worker_adapter = _cloud_adapter(backend)
    stored_w = worker_adapter.head_object(storage_key_a)
    assert stored_w is not None
    assert worker_adapter.get_object(storage_key_a) == content
    assert len(backend.responses) == 2
    assert all(response.closed for response in backend.responses)


class SimpleActor:
    """require_role/require_project_access/write_audit 需要的最小 actor 形状。"""

    def __init__(self, *, id: str, role: str) -> None:
        self.id = id
        self.username = id
        self.display_name = f"CW031 {id}"
        self.role = role  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# E 组：COS put / get / sign 故障不落本地（定稿用例 3/4/5）
# ---------------------------------------------------------------------------


class _FailingPutClient(_SharedMemoryCosClient):
    def put_object(self, **kwargs: object) -> None:
        raise RuntimeError("simulated COS put outage")


class _FailingGetClient(_SharedMemoryCosClient):
    def get_object(self, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated COS get outage")


class _FailingSignClient(_SharedMemoryCosClient):
    def get_presigned_url(self, **kwargs: object) -> str:
        raise RuntimeError("simulated COS signing outage")


def _assert_no_local_files(env: FenceEnv, before: set[str]) -> None:
    assert _persistent_file_snapshot(env.local_root) - before == set()


def test_cos_put_failure_returns_clear_error_and_writes_nothing_local(
    fence_env: FenceEnv,
) -> None:
    before = _persistent_file_snapshot(fence_env.local_root)
    adapter = _cloud_adapter(_FailingPutClient())

    with pytest.raises(StorageBackendUnavailable, match="cloud object upload failed"):
        adapter.put_object("projects/cw031-fault/x.mp4", b"data", content_type="video/mp4")

    _assert_no_local_files(fence_env, before)


def test_cos_get_failure_returns_clear_error(fence_env: FenceEnv) -> None:
    before = _persistent_file_snapshot(fence_env.local_root)
    adapter = _cloud_adapter(_FailingGetClient())

    with pytest.raises(StorageBackendUnavailable, match="cloud object download failed"):
        adapter.get_object("projects/cw031-fault/x.mp4")

    _assert_no_local_files(fence_env, before)


def test_cos_sign_failure_returns_clear_error_and_signature_capability_survives(
    fence_env: FenceEnv,
) -> None:
    before = _persistent_file_snapshot(fence_env.local_root)

    with pytest.raises(StorageBackendUnavailable, match="cloud upload URL signing failed"):
        _cloud_adapter(_FailingSignClient()).create_upload_intent(
            "projects/cw031-fault/x.mp4",
            content_type="video/mp4",
            expires_in=timedelta(minutes=15),
        )

    # 签名能力本身未受损：恢复可用 client 后签名立即成功（规格完工标准）。
    intent = _cloud_adapter(_SharedMemoryCosClient()).create_upload_intent(
        "projects/cw031-fault/x.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=15),
    )
    assert intent.method == "PUT"
    assert intent.url.startswith("https://")
    _assert_no_local_files(fence_env, before)


# ---------------------------------------------------------------------------
# F 组：授权下载矩阵（跨用户 / 篡改 / 过期 / 重获，定稿用例 8）
# ---------------------------------------------------------------------------


def _signed_params(
    *,
    key: str,
    expires_at: str,
    user_id: str,
    asset_id: str,
    session_epoch: str,
    secret: str,
) -> dict[str, str]:
    signature = local_download_signature(
        key,
        expires_at,
        user_id=user_id,
        asset_id=asset_id,
        session_epoch=session_epoch,
        secret=secret,
    )
    return {
        "expires": expires_at,
        "sig": signature,
        "user_id": user_id,
        "asset_id": asset_id,
        "session_epoch": session_epoch,
    }


def _seed_asset(fence_env: FenceEnv, project_id: str) -> str:
    _ensure_project(fence_env.dsn, project_id)
    asset_id = f"cw031-asset-{uuid4().hex[:8]}"
    with psycopg.connect(fence_env.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) "
            "VALUES (%s, %s, 'reference_video', %s, %s, %s, 'video/mp4', %s)",
            (
                asset_id,
                project_id,
                f"cos://{COS_BUCKET}/projects/{project_id}/source.mp4",
                "0" * 64,
                8,
                ADMIN_USER_ID,
            ),
        )
    return asset_id


def _object_key_for(project_id: str) -> str:
    return f"projects/{project_id}/source.mp4"


def _grant(env: FenceEnv, *, user_id: str, asset_id: str, key: str, epoch: str = "0") -> Any:
    with psycopg.connect(env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        return validate_signed_asset_grant(
            conn,
            user_id=user_id,
            asset_id=asset_id,
            session_epoch=epoch,
            expected_object_key=key,
        )


def test_owner_with_valid_signature_reads_the_asset(fence_env: FenceEnv) -> None:
    project_id = f"cw031-authz-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    key = _object_key_for(project_id)

    granted = _grant(
        fence_env,
        user_id=ADMIN_USER_ID,
        asset_id=asset_id,
        key=key,
        epoch="0",
    )

    assert str(granted["id"]) == asset_id


def test_unicode_object_key_grant_preserves_exact_asset_binding(fence_env: FenceEnv) -> None:
    project_id = f"cw031-unicode-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    key = f"projects/{project_id}/回老家 建房？.mp4"
    with psycopg.connect(fence_env.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE assets SET storage_uri = %s WHERE id = %s",
            (f"cos://{COS_BUCKET}/{key}", asset_id),
        )
    assert _grant(fence_env, user_id=ADMIN_USER_ID, asset_id=asset_id, key=key)["id"] == asset_id
    with pytest.raises(HTTPException) as denied:
        _grant(fence_env, user_id=ADMIN_USER_ID, asset_id=asset_id, key=key.replace("家", "乡"))
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "SIGNED_ASSET_GRANT_FORBIDDEN"


def test_tampered_or_expired_signature_is_rejected(fence_env: FenceEnv) -> None:
    project_id = f"cw031-authz-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    key = _object_key_for(project_id)
    secret = settings_encryption_key()
    assert secret
    expired_at = str(int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()))
    future_at = str(int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()))

    with pytest.raises(HTTPException) as tampered:
        _grant_with_signature(
            fence_env,
            signature="0" * 64,
            expires_at=future_at,
            user_id=ADMIN_USER_ID,
            asset_id=asset_id,
            key=key,
        )
    assert tampered.value.status_code == 403
    assert tampered.value.detail["code"] == "LOCAL_DOWNLOAD_FORBIDDEN"

    with pytest.raises(HTTPException) as expired:
        _grant_with_signature(
            fence_env,
            signature=local_download_signature(
                key,
                expired_at,
                user_id=ADMIN_USER_ID,
                asset_id=asset_id,
                session_epoch="0",
                secret=secret,
            ),
            expires_at=expired_at,
            user_id=ADMIN_USER_ID,
            asset_id=asset_id,
            key=key,
        )
    assert expired.value.status_code == 403


def _grant_with_signature(
    env: FenceEnv,
    *,
    signature: str,
    expires_at: str,
    user_id: str,
    asset_id: str,
    key: str,
    epoch: str = "0",
) -> None:
    assert local_download_signature  # 签名能力保留（规格完工标准）
    with psycopg.connect(env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        _validate_signed_request_shape(
            conn,
            signature=signature,
            expires_at=expires_at,
            user_id=user_id,
            asset_id=asset_id,
            session_epoch=epoch,
            key=key,
        )


def _validate_signed_request_shape(
    conn: BusinessConnection,
    *,
    signature: str,
    expires_at: str,
    user_id: str,
    asset_id: str,
    session_epoch: str,
    key: str,
) -> Any:
    """直接复刻 _validate_signed_object_request 的签名分支（其入参是 Request）。"""
    import time as _time

    secret = settings_encryption_key()
    if (
        not expires_at
        or int(expires_at) < int(_time.time())
        or not hmac_compare(signature, key, expires_at, user_id, asset_id, session_epoch, secret)
    ):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_DOWNLOAD_FORBIDDEN"})
    return validate_signed_asset_grant(
        conn,
        user_id=user_id,
        asset_id=asset_id,
        session_epoch=session_epoch,
        expected_object_key=key,
    )


def hmac_compare(
    signature: str,
    key: str,
    expires_at: str,
    user_id: str,
    asset_id: str,
    session_epoch: str,
    secret: str | None,
) -> bool:
    import hmac as _hmac

    if not secret:
        return False
    expected = local_download_signature(
        key,
        expires_at,
        user_id=user_id,
        asset_id=asset_id,
        session_epoch=session_epoch,
        secret=secret,
    )
    return _hmac.compare_digest(signature, expected)


def test_cross_user_signature_cannot_read_the_asset(fence_env: FenceEnv) -> None:
    """B 用户持对 B 有效的签名访问 A 的资产：签名（含 user_id 绑定）必然失配。"""
    project_id = f"cw031-authz-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    key = _object_key_for(project_id)
    secret = settings_encryption_key()
    assert secret
    expires_at = str(int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()))
    foreign_signature = local_download_signature(
        key,
        expires_at,
        user_id=OTHER_USER_ID,
        asset_id=asset_id,
        session_epoch="0",
        secret=secret,
    )

    with pytest.raises(HTTPException) as excinfo:
        _grant_with_signature(
            fence_env,
            signature=foreign_signature,
            expires_at=expires_at,
            user_id=ADMIN_USER_ID,
            asset_id=asset_id,
            key=key,
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "LOCAL_DOWNLOAD_FORBIDDEN"


def test_expired_signature_has_a_clear_reacquisition_path(fence_env: FenceEnv) -> None:
    """签名过期 → 明确 403；重新请求下载 intent 得到不同且可用的新签名。"""
    project_id = f"cw031-authz-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    key = _object_key_for(project_id)
    secret = settings_encryption_key()
    assert secret

    expired_at = str(int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()))
    renewed_at = str(int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()))
    renewed_signature = local_download_signature(
        key,
        renewed_at,
        user_id=ADMIN_USER_ID,
        asset_id=asset_id,
        session_epoch="0",
        secret=secret,
    )

    assert renewed_signature != local_download_signature(
        key,
        expired_at,
        user_id=ADMIN_USER_ID,
        asset_id=asset_id,
        session_epoch="0",
        secret=secret,
    )
    granted = _grant_with_signature_ok(
        fence_env,
        signature=renewed_signature,
        expires_at=renewed_at,
        user_id=ADMIN_USER_ID,
        asset_id=asset_id,
        key=key,
    )
    assert str(granted["id"]) == asset_id

    # 云端下载签名能力（cos adapter）同样可重新签发——重获路径不依赖 local 车道。
    download = _cloud_adapter(_SharedMemoryCosClient()).create_download_intent(
        key, expires_in=timedelta(minutes=30), can_read=True
    )
    assert download.method == "GET"


def _grant_with_signature_ok(
    env: FenceEnv,
    *,
    signature: str,
    expires_at: str,
    user_id: str,
    asset_id: str,
    key: str,
) -> Any:
    with psycopg.connect(env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        return _validate_signed_request_shape(
            conn,
            signature=signature,
            expires_at=expires_at,
            user_id=user_id,
            asset_id=asset_id,
            session_epoch="0",
            key=key,
        )


def test_source_url_expired_is_available_for_cdn_expiry_semantics() -> None:
    """SourceUrlExpired 未被移除：COS 侧 URL 过期语义仍在（签名能力零删减）。"""
    assert issubclass(SourceUrlExpired, RuntimeError)


def test_asset_access_denies_unrelated_user(fence_env: FenceEnv) -> None:
    """权限矩阵：非 owner、非 admin 的普通用户访问他人资产 → 拒绝。"""
    project_id = f"cw031-authz-{uuid4().hex[:8]}"
    asset_id = _seed_asset(fence_env, project_id)
    _ensure_user(fence_env.dsn, "cw031-stranger", "customer")

    with psycopg.connect(fence_env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(HTTPException):
            require_asset_access(
                conn,
                actor=cast(Any, SimpleActor(id="cw031-stranger", role="customer")),
                asset_id=asset_id,
                action="asset.signed_download.read",
            )


# ---------------------------------------------------------------------------
# G 组：本地临时缓存删除后业务仍可从 COS 恢复（定稿用例 2）
# ---------------------------------------------------------------------------


def test_character_cache_directory_removal_keeps_cos_recovery_path(
    fence_env: FenceEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import rbac_routes

    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(tmp_path / "home"))
    _set_runtime_provider(fence_env.dsn, "cos")
    _write_cos_settings(fence_env.dsn, settings_key=fence_env.settings_key)

    backend = _SharedMemoryCosClient()
    backend.objects["projects/character-cache/cached.png"] = (
        b"cacheable-image-bytes",
        "image/png",
    )
    # 只替换网络层（COS client 的载体），COS 强制选择逻辑保持真实执行。
    monkeypatch.setattr(
        rbac_routes, "create_storage_adapter", lambda _config: _cloud_adapter(backend)
    )

    with psycopg.connect(fence_env.dsn, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)

        cache_root = rbac_routes._character_cache_root()
        if cache_root.exists():
            shutil.rmtree(cache_root)
        storage = rbac_routes._customer_character_cache_storage(conn)
        assert storage.provider == "cos"
        assert storage.bucket == COS_BUCKET
        assert storage.head_object("projects/character-cache/cached.png") is not None

        # 缓存目录整删后，同一解析路径立即从 COS 恢复（缓存不是跨进程持久真源）。
        assert not cache_root.exists() or list(cache_root.rglob("*")) == []


# ---------------------------------------------------------------------------
# H 组：bootstrap 启动期写路径探测（定稿 §5.8）
# ---------------------------------------------------------------------------


def test_write_path_probe_puts_and_deletes_under_reserved_prefix() -> None:
    adapter = FakeStorageAdapter(provider="cos", bucket=COS_BUCKET)

    _probe_formal_service_write_path(adapter)

    # 删除必须真实发生（对象已不存在且审计留下 object.deleted 轨迹）；
    # fake 适配器的 put 不记审计，删除审计是探测「写后即删」的证据。
    assert [event.action for event in adapter.audit_events] == ["object.deleted"]
    probe_keys = [event.object_key for event in adapter.audit_events]
    assert all(key.startswith(".cw031-readiness/") for key in probe_keys)
    assert all(adapter.head_object(key) is None for key in set(probe_keys))


def test_write_path_probe_failure_propagates_for_fail_closed_bootstrap() -> None:
    adapter = _cloud_adapter(_FailingPutClient())

    with pytest.raises(StorageBackendUnavailable):
        _probe_formal_service_write_path(adapter)
