"""管理端付费探针入口 —— `POST /api/control/settings/providers/{provider}/paid-test`。

背景（2026-09-18 前后端页面功能与接口匹配梳理 §5 P1）：付费探针原本只挂在旧
管理通道 `/api/admin/settings/providers/{provider}/paid-test`
（`settings_routes.py`），而管理端控制面封装 `client/src/api.admin.ts` 的
`requestControl` 只带 Cookie + CSRF、不发 `Authorization`，在 PG 通道下必然
401——管理端根本够不到它。定案处方即「迁到 /api/control 前缀并接管理端」。

本任务的判定：**付费探针是敏感写**（它可能真实扣费），因此与同文件的
`connection-test`（普通 POST）不同，它必须走仓库既有的管理写契约
（`confirm` + 非空 `reason` + `Idempotency-Key`），并在同一事务里落一条审计。
本文件把该判定钉死：

- 无信封 → 400 `IDEMPOTENCY_KEY_REQUIRED` / `CONFIRMATION_REQUIRED` /
  `REASON_REQUIRED`（三个正交用例）；
- 同一幂等键重放 → 探针**只跑一次**（`X-Idempotent-Replay: true`），这是
  「不会重复扣费」的直接证据；
- 同一幂等键换请求体 → 409 `IDEMPOTENCY_CONFLICT`；
- 成功路径落审计 `provider_settings.paid_test`，含操作原因与 request_id；
- 审计器（auditor）只读 → 403 `AUDITOR_READ_ONLY`；无会话 → 401。

另有一条**如实呈现**断言：对尚未接入真实客户端的服务（本文件以 deepseek 为例；
metaso 已接入真实探针，见 `test_metaso_paid_probe.py`），默认测试器
（`get_provider_tester` → `NoopProviderTester`）对 paid_test 恒抛
501 `PROVIDER_TEST_NOT_IMPLEMENTED`，且**不落审计**（没有任何付费动作发生）。
前端文案必须与这条事实一致，不得对这类服务宣称「会产生真实费用」。

TEST-PG：专属库（module 级建库 → alembic head → 每用例 TRUNCATE），沿用
`test_admin_session_routes` / `test_customer_registration` 的夹具形态。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import ADMIN_CSRF_HEADER, ADMIN_SESSION_HMAC_KEY_ENV
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.settings import ProviderTestResult, get_provider_tester

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
PROBE_DB_NAME = "t04_provider_probe_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
# Fernet key: urlsafe base64 of 32 random bytes, minted per run (never a real key).
TEST_SETTINGS_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _probe_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{PROBE_DB_NAME}"


def _alembic_config(dsn: str) -> Any:
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


# ---------------------------------------------------------------------------
# Static lane (no database) — the route surface and the legacy compatibility
# ---------------------------------------------------------------------------


def _control_route_paths() -> set[tuple[str, str]]:
    from app.control_routes import router

    return {
        (method, str(route.path))
        for route in router.routes
        for method in getattr(route, "methods", set())
    }


def test_paid_probe_is_mounted_on_the_control_router_as_a_post() -> None:
    """控制面新增 `POST .../paid-test`；与免费 connection-test 同段并列。"""
    paths = _control_route_paths()
    assert ("POST", "/api/control/settings/providers/{provider}/paid-test") in paths
    assert ("POST", "/api/control/settings/providers/{provider}/connection-test") in paths


def test_legacy_admin_paid_probe_route_is_kept_for_compatibility() -> None:
    """旧 `/api/admin/settings/.../paid-test` 不得删除（可能是别的通道在用）。

    本任务只做「迁到 control 并接管理端」，取消注册归 CW-041 的物理退出批次。
    """
    from app.settings_routes import router

    paths = {
        (method, str(route.path))
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/api/admin/settings/providers/{provider}/paid-test") in paths


def test_paid_probe_route_declares_the_write_contract_body() -> None:
    """付费探针的请求体必须是写契约信封（confirm + reason），不是空体。"""
    import inspect
    import typing

    from app import control_routes

    hints = typing.get_type_hints(control_routes.paid_test_control_provider)
    assert "payload" in inspect.signature(control_routes.paid_test_control_provider).parameters
    fields = set(hints["payload"].model_fields)
    assert {"confirm", "reason"} <= fields


# ---------------------------------------------------------------------------
# PG lane — the write contract, the idempotency guard and the audit trail
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def probe_dsn() -> Iterator[str]:
    from alembic import command

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{PROBE_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{PROBE_DB_NAME}"')
    command.upgrade(_alembic_config(_probe_dsn()), "head")
    try:
        yield _probe_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{PROBE_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(probe_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(probe_dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE audit_logs, admin_write_idempotency, admin_sessions, "
            "admin_password_credentials, provider_settings, security_rate_limit_counters, "
            "security_auth_failures, h3_provider_accounts, users CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor')"
        )
        conn.execute("UPDATE users SET is_super_admin=1 WHERE id='admin_u'")
    yield probe_dsn
    close_pg_pool()


class RecordingProviderTester:
    """A stand-in real provider client: counts paid probes instead of charging."""

    def __init__(self) -> None:
        self.paid_calls: list[tuple[str, dict[str, str]]] = []

    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return ProviderTestResult(status="ok", provider=provider, test_kind="connection")

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        self.paid_calls.append((provider, dict(config)))
        return ProviderTestResult(
            status="ok",
            provider=provider,
            test_kind="paid_probe",
            account_credit=42,
        )


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.control_routes import router as control_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(control_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    # 客户生产形态：控制面写必须解析 per-operator admin session（CSRF + RBAC +
    # 写契约的幂等快照层都只在这条通道上生效）。
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", TEST_SETTINGS_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture()
def tester(admin_app: FastAPI) -> RecordingProviderTester:
    recording = RecordingProviderTester()
    admin_app.dependency_overrides[get_provider_tester] = lambda: recording
    return recording


@pytest.fixture()
def paid_probe_headers(client: TestClient) -> dict[str, str]:
    response = password_admin_session(client, "admin_u")
    assert response.status_code == 201, response.text
    return {
        ADMIN_CSRF_HEADER: response.json()["csrf_token"],
        "Idempotency-Key": f"paid-probe-{secrets.token_hex(8)}",
    }


def _payload(reason: str = "上线前核对付费通道") -> dict[str, object]:
    return {"confirm": True, "reason": reason}


def _audit_rows(dsn: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT actor_user_id, action, metadata_json FROM audit_logs "
            "WHERE action = 'provider_settings.paid_test'"
        ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def test_paid_probe_requires_the_write_contract(
    client: TestClient, paid_probe_headers: dict[str, str]
) -> None:
    """三件套缺一即拒：幂等键、confirm、非空 reason 各自拥有独立错误码。"""
    url = "/api/control/settings/providers/metaso/paid-test"

    missing_key = client.post(
        url,
        headers={ADMIN_CSRF_HEADER: paid_probe_headers[ADMIN_CSRF_HEADER]},
        json=_payload(),
    )
    assert missing_key.status_code == 400, missing_key.text
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_confirm = client.post(
        url,
        headers=paid_probe_headers,
        json={"reason": "上线前核对付费通道"},
    )
    assert missing_confirm.status_code == 400, missing_confirm.text
    assert missing_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    blank_reason = client.post(
        url,
        headers=paid_probe_headers,
        json=_payload(reason="   "),
    )
    assert blank_reason.status_code == 400, blank_reason.text
    assert blank_reason.json()["detail"]["code"] == "REASON_REQUIRED"


def test_paid_probe_requires_an_operator_session(client: TestClient) -> None:
    response = client.post(
        "/api/control/settings/providers/metaso/paid-test",
        json=_payload(),
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"


def test_paid_probe_is_read_only_for_auditors(
    client: TestClient,
    paid_probe_headers: dict[str, str],
) -> None:
    auditor = password_admin_session(client, "auditor_u")
    assert auditor.status_code == 201, auditor.text
    response = client.post(
        "/api/control/settings/providers/metaso/paid-test",
        headers={
            ADMIN_CSRF_HEADER: auditor.json()["csrf_token"],
            "Idempotency-Key": "paid-probe-auditor",
        },
        json=_payload(),
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


def test_paid_probe_rejects_an_unsupported_provider(
    client: TestClient, paid_probe_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/control/settings/providers/not-a-provider/paid-test",
        headers=paid_probe_headers,
        json=_payload(),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "UNSUPPORTED_PROVIDER"


def test_paid_probe_audits_the_operator_and_their_reason(
    client: TestClient,
    tester: RecordingProviderTester,
    paid_probe_headers: dict[str, str],
    route_state: str,
) -> None:
    response = client.post(
        "/api/control/settings/providers/metaso/paid-test",
        headers=paid_probe_headers,
        json=_payload(reason="上线前核对付费通道"),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "provider": "metaso",
        "test_kind": "paid_probe",
        "account_credit": 42,
    }
    assert [call[0] for call in tester.paid_calls] == ["metaso"]

    rows = _audit_rows(route_state)
    assert len(rows) == 1, rows
    actor_user_id, action, metadata_json = rows[0]
    assert actor_user_id == "admin_u"
    assert action == "provider_settings.paid_test"
    # audit_logs.metadata_json 由 insert_audit 以 ensure_ascii=True 落库，
    # 中文按 \uXXXX 转义——断言解析后的语义值，不匹配转义形态。
    metadata = json.loads(metadata_json)
    assert metadata["reason"] == "上线前核对付费通道"
    assert metadata["provider"] == "metaso"
    assert metadata["test_kind"] == "paid_probe"
    assert isinstance(metadata["request_id"], str) and metadata["request_id"]


def test_paid_probe_hands_the_pool_account_key_to_the_tester(
    client: TestClient,
    tester: RecordingProviderTester,
    paid_probe_headers: dict[str, str],
) -> None:
    """视频生成：启用账号池时，探针拿到的是池里会接任务的账号密钥，不是旧的设置页密钥。"""
    from app.db_pg import pg_transaction
    from app.db_portable import BusinessConnection
    from app.h3_account_pool import save_account
    from app.settings import SettingsRepository

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        # save_account 要先锁共享并发容量行；探针专属库默认没有这一行。
        raw.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 100, 1000, 10000, 1000) ON CONFLICT (id) DO NOTHING"
        )
        save_account(
            conn,
            account_id="pool-a",
            name="pool-a",
            api_key="synthetic-pool-a",
            concurrency_limit=2,
            enabled=True,
            expected_version=0,
        )
        # 建池之后设置页里的旧密钥被改成了别的值：探针不得拿它去测。
        SettingsRepository(conn).save_provider_config(
            "metaso", {"api_key": "synthetic-stale"}, actor_user_id="admin_u"
        )

    response = client.post(
        "/api/control/settings/providers/metaso/paid-test",
        headers=paid_probe_headers,
        json=_payload(),
    )
    assert response.status_code == 200, response.text
    assert [call[0] for call in tester.paid_calls] == ["metaso"]
    assert tester.paid_calls[0][1]["api_key"] == "synthetic-pool-a"


def test_paid_probe_replays_the_snapshot_instead_of_probing_twice(
    client: TestClient,
    tester: RecordingProviderTester,
    paid_probe_headers: dict[str, str],
    route_state: str,
) -> None:
    """同一幂等键重放：探针只跑一次，第二次回放快照。

    这正是把付费探针纳入写契约的核心理由——一次网络歧义重试不得变成第二次扣费。
    """
    url = "/api/control/settings/providers/metaso/paid-test"
    first = client.post(url, headers=paid_probe_headers, json=_payload())
    assert first.status_code == 200, first.text

    replay = client.post(url, headers=paid_probe_headers, json=_payload())
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    assert replay.json() == first.json()
    assert len(tester.paid_calls) == 1, "a replay must never run a second paid probe"
    assert len(_audit_rows(route_state)) == 1


def test_paid_probe_conflicts_when_the_key_is_reused_for_another_request(
    client: TestClient,
    tester: RecordingProviderTester,
    paid_probe_headers: dict[str, str],
) -> None:
    url = "/api/control/settings/providers/metaso/paid-test"
    first = client.post(url, headers=paid_probe_headers, json=_payload())
    assert first.status_code == 200, first.text

    conflict = client.post(
        url,
        headers=paid_probe_headers,
        json=_payload(reason="另一个原因"),
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(tester.paid_calls) == 1


def test_paid_probe_reports_the_unwired_stub_honestly(
    client: TestClient,
    paid_probe_headers: dict[str, str],
    route_state: str,
) -> None:
    """真实 provider 客户端未接入：默认测试器恒 501，且不落审计。

    该断言是前端文案的事实来源——未接入就是未接入，不得提示「会产生真实费用」。
    """
    response = client.post(
        "/api/control/settings/providers/deepseek/paid-test",
        headers=paid_probe_headers,
        json=_payload(),
    )
    assert response.status_code == 501, response.text
    assert response.json()["detail"]["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"
    assert response.json()["detail"]["message"] == (
        "A real provider client is required before paid tests can run."
    )
    # 没有付费动作发生，就没有付费审计行。
    assert _audit_rows(route_state) == []


def test_free_connection_test_contract_is_unchanged(
    client: TestClient, paid_probe_headers: dict[str, str]
) -> None:
    """免费连接测试仍是普通 POST（无写契约）——付费探针的加严不得外溢。"""
    response = client.post(
        "/api/control/settings/providers/metaso/connection-test",
        headers=paid_probe_headers,
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "not_configured",
        "provider": "metaso",
        "test_kind": "connection",
    }
