"""M4 exit gate — the full customer chain E2E on the PostgreSQL lane.

The T34 E2E exit gate deferred to M5 (task list): 激活 → 任务 → 第二设备 →
冲突 → 切换 → 续充 must run end-to-end over the real customer API surface
on the migrated PostgreSQL fixture — component tests or single-endpoint
smokes are not acceptable evidence.

Two independent tests each own their whole chain (fresh TRUNCATE state):

1. ``test_customer_chain_activation_to_direct_task`` — activate (201),
   business-login, create the project through the fenced write route,
   script → compile → lock the prompt over the API, submit a batch
   (provider=fake_h3), drain the queue with a real worker
   (``run_next_generation_task`` inside fenced ``pg_transaction`` rounds —
   the ``run_pg_worker_once`` shape) and prove the task lands SUCCEEDED /
   DIRECT with the fair-queue cursor slot released;
2. ``test_customer_chain_second_device_conflict_switch_recharge`` —
   activate, enroll a second device (202 → first-device approve → same-key
   consume 201), the second device's login answers 409
   OTHER_DEVICE_ONLINE (conflict), the explicit switch displaces the first
   device atomically (epoch bump), the old session token now fails the
   fenced write gate (401 SESSION_REPLACED), and the top-up chain closes:
   recharge order 201 PENDING → signed ZPay callback → PAID + wallet credit,
   idempotent on replay.

Every write rides the T21 fenced path: ``BusinessDbDep`` resolves the
session snapshot and ``fenced_pg_transaction`` re-verifies it under the
row lock inside the business transaction.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
    generate_activation_code,
    mask_activation_code,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import FakeH3Provider, run_next_generation_task
from app.storage import FakeStorageAdapter

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

TEST_KEY = secrets.token_urlsafe(48)  # activation-code HMAC key, never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

CHAIN_DB_NAME = "t34_chain_e2e"

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
ACTIVATE_PATH = "/api/customer/activate"
LOGIN_PATH = "/api/customer/sessions/login"
SWITCH_PATH = "/api/customer/sessions/switch"
ENROLL_PATH = "/api/customer/devices/enroll"
APPROVE_PATH = "/api/customer/device-pairings"
PROJECTS_PATH = "/api/projects"
RECHARGE_PATH = "/api/customer/recharge-orders"
NOTIFY_PATH = "/api/payments/zpay/notify"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

_SCRIPT_TEXT = "开场说明产品价值。结尾给出行动建议。"


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _chain_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{CHAIN_DB_NAME}"


# ---------------------------------------------------------------------------
# Fixtures: dedicated migrated database, clean state, full customer app
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CHAIN_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CHAIN_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _chain_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _chain_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CHAIN_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(chain_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """TRUNCATE to a clean slate, seed the operator + production-like runtime
    settings (fair queue on) and the ZPay provider configuration."""
    from cryptography.fernet import Fernet

    settings_fernet_key = Fernet.generate_key()
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_fernet_key.decode("ascii"))
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://callback.example.com")

    close_pg_pool()
    with psycopg.connect(_chain_dsn(), autocommit=True) as conn:
        # TRUNCATE users CASCADE also clears its rate-configuration dependants.
        # Keep the migrated defaults so this chain exercises real cost snapshots.
        default_rates = conn.execute(
            "SELECT subject, kind, unit, resolution, unit_price_fen FROM operation_cost_rates"
        ).fetchall()
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "projects, versions, assets, generation_batches, generation_tasks, "
            "generation_task_operations, external_call_logs, "
            "user_queue_cursors, runtime_settings, provider_settings, "
            "security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO operation_cost_rates "
                "(subject, kind, unit, resolution, unit_price_fen) VALUES (%s, %s, %s, %s, %s)",
                default_rates,
            )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
        # The paid scenario explicitly publishes this feature tariff.
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('video_768p',true,1,2)"
        )
        # Production-like runtime: fair queue on, generous batch ceiling.
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 100, 1000, 1000, 10000, 1000, true)"
        )
        # ZPay merchant config (same shape as the T22 fixture).
        encrypted = (
            Fernet(settings_fernet_key)
            .encrypt(
                b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
            )
            .decode("ascii")
        )
        conn.execute(
            "INSERT INTO provider_settings "
            "(provider, encrypted_config, updated_by_user_id, created_at, updated_at) "
            "VALUES (%s, %s, 'admin_u', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("zpay", encrypted),
        )
    yield _chain_dsn()
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.analysis_routes import router as analysis_router
    from app.character_reference_routes import router as character_reference_router
    from app.character_routes import router as character_router
    from app.customer_device_routes import router as customer_device_router
    from app.customer_session_routes import router as customer_session_router
    from app.first_frame_routes import router as first_frame_router
    from app.generation_routes import router as generation_router
    from app.media_routes import router as media_router
    from app.payment_routes import router as payment_router
    from app.rbac_routes import router as rbac_router
    from app.recharge_routes import router as recharge_router
    from app.simple_character_routes import router as simple_character_router
    from app.source_frame_routes import router as source_frame_router

    app = FastAPI()
    for r in (
        activation_code_router,
        customer_session_router,
        customer_device_router,
        rbac_router,
        media_router,
        recharge_router,
        payment_router,
        analysis_router,
        first_frame_router,
        source_frame_router,
        simple_character_router,
        character_router,
        character_reference_router,
        generation_router,
    ):
        app.include_router(r)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        _b64key(TEST_ENVELOPE_AEAD_KEY),
    )
    # The rate limiter guards activate/login; raise the budgets far above
    # anything the E2E can generate so the chain itself is what is tested.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    # Storage/provider/extractor deps read get_database (a SQLite path) that
    # does not exist on the PG lane; the chain routes never reach them
    # (provider=fake_h3, worker storage injected directly), so inert fakes
    # are enough for dependency resolution.
    from app.character_identity_routes import get_character_storage
    from app.first_frame_routes import get_image_provider
    from app.media_routes import get_media_storage, get_video_probe
    from app.source_frame_routes import get_source_frame_extractor

    for dep in (
        get_media_storage,
        get_image_provider,
        get_character_storage,
        get_source_frame_extractor,
        get_video_probe,
    ):
        app.dependency_overrides[dep] = lambda: object()

    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    with psycopg.connect(_chain_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES (%s, %s, 1500, 1000, 100, 1, %s, 'OPEN', 'admin_u')",
            (batch_id, f"batch-{batch_id}", FUTURE_EXPIRY),
        )
        digest = compute_code_digest(code, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            "VALUES (%s, %s, %s, 1, %s, 'ISSUED', '2026-01-01T00:00:00+00:00')",
            (code_id, batch_id, digest, mask_activation_code(code)),
        )


def _activated_customer(
    client: TestClient, *, code: str, fingerprint: str, suffix: str
) -> dict[str, Any]:
    _seed_issuable_code(code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"Device {suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-activate-{suffix}"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "device_token", "session_token"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    return cast(dict[str, Any], payload)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _business_login(client: TestClient, customer: dict[str, Any], key: str) -> str:
    resp = client.post(
        LOGIN_PATH,
        json={},
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_token"])


def _seed_chain_assets(*, project_id: str, user_id: str) -> tuple[str, str]:
    """Insert the shot-card version + first-frame asset the prompt chain
    needs. These rows model what the analysis/upload lanes would produce
    (outside this gate's scope) so the API chain itself stays real: the
    script, compile, lock and batch routes all run with the customer's
    fenced session."""
    with psycopg.connect(_chain_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO assets "
            "(id, project_id, kind, storage_uri, sha256, size_bytes, "
            " content_type, created_by_user_id) "
            "VALUES ('ff-1', %s, 'image', 'fake://generation-results/first-frame.png', "
            "'sha-ff', 12, 'image/png', %s)",
            (project_id, user_id),
        )
        conn.execute(
            "INSERT INTO versions "
            "(id, project_id, asset_id, kind, version_number, payload_json, "
            " created_by_user_id) "
            "VALUES ('sc-1', %s, NULL, 'shot_card', 1, %s, %s)",
            (
                project_id,
                (
                    '{"schema_version": 1, "duration_seconds": 10, "shots": ['
                    '{"shot_id": "S01", "start_time": 0, "end_time": 5, '
                    '"shot_type": "近景", "composition": "人物居中", '
                    '"camera_motion": "固定", "subject": "主讲人", '
                    '"action": "看镜头口播", "scene": "室内", '
                    '"spoken_text": "原始第一句", "transition": "硬切"},'
                    '{"shot_id": "S02", "start_time": 5, "end_time": 10, '
                    '"shot_type": "中景", "composition": "三分法", '
                    '"camera_motion": "轻微推进", "subject": "主讲人", '
                    '"action": "继续讲解", "scene": "室内", '
                    '"spoken_text": "原始第二句", "transition": "硬切"}'
                    "]}"
                ),
                user_id,
            ),
        )
        # The first-frame confirmation the compile route requires: a candidate
        # set plus the selection that pins it to the requested asset.
        conn.execute(
            "INSERT INTO versions "
            "(id, project_id, asset_id, kind, version_number, payload_json, "
            " created_by_user_id) "
            "VALUES ('ff-candidates-1', %s, 'ff-1', 'first_frame_candidates', 1, %s, %s)",
            (
                project_id,
                '{"candidates": [{"asset_id": "ff-1", "quality": {"passed": true}}]}',
                user_id,
            ),
        )
        conn.execute(
            "INSERT INTO versions "
            "(id, project_id, asset_id, kind, version_number, payload_json, "
            " created_by_user_id) "
            "VALUES ('ff-selection-1', %s, 'ff-1', 'first_frame_selection', 1, %s, %s)",
            (
                project_id,
                (
                    '{"first_frame_candidates_version_id": "ff-candidates-1", '
                    '"first_frame_asset_id": "ff-1"}'
                ),
                user_id,
            ),
        )
    return "sc-1", "ff-1"


def _create_locked_prompt(
    client: TestClient,
    *,
    session_token: str,
    project_id: str,
    shot_card_version_id: str,
    first_frame_asset_id: str,
) -> str:
    """Script → compile → lock over the customer API (all fenced writes)."""
    script = client.post(
        f"{PROJECTS_PATH}/{project_id}/scripts",
        headers=_bearer(session_token),
        json={
            "source": "custom",
            "text": _SCRIPT_TEXT,
            "shot_card_version_id": shot_card_version_id,
        },
    )
    assert script.status_code == 200, script.text
    script_version_id = str(script.json()["id"])

    compiled = client.post(
        f"{PROJECTS_PATH}/{project_id}/prompts/compile",
        headers=_bearer(session_token),
        json={
            "script_version_id": script_version_id,
            "shot_card_version_id": shot_card_version_id,
            "first_frame_asset_id": first_frame_asset_id,
            "output_duration_seconds": 4,
            "resolution": "768P",
            "timeline_policy": "scale_confirmed",
            "opening_action": "保持首帧姿态看镜头口播，再自然继续讲解。",
        },
    )
    assert compiled.status_code == 200, compiled.text
    prompt_version_id = str(compiled.json()["id"])

    locked = client.post(
        f"{PROJECTS_PATH}/{project_id}/prompts/{prompt_version_id}/lock",
        headers=_bearer(session_token),
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["payload"]["status"] == "LOCKED", locked.text
    return prompt_version_id


def _drain_pg_worker(dsn: str, *, worker_id: str = "e2e-worker") -> int:
    """Process every eligible task in fenced per-task transactions — the
    ``run_pg_worker_once`` shape — and return how many tasks completed."""
    processed = 0
    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    while True:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            result = run_next_generation_task(
                conn,
                worker_id=worker_id,
                provider=FakeH3Provider(),
                storage=storage,
                first_frame_storage=storage,
            )
        if result is None:
            return processed
        processed += 1


def _signed_notify_params(
    order_no: str,
    *,
    trade_no: str,
    amount_yuan: str = "100.00",
) -> dict[str, str]:
    from app.zpay import sign_zpay_params

    params = {
        "pid": "merchant-123",
        "name": "内部视频生成条数充值 10 条",
        "money": amount_yuan,
        "out_trade_no": order_no,
        "trade_no": trade_no,
        "trade_status": "TRADE_SUCCESS",
        "type": "alipay",
    }
    params["sign"] = sign_zpay_params(params, "merchant-secret")
    params["sign_type"] = "MD5"
    return params


# ---------------------------------------------------------------------------
# Chain 1: 激活 → 任务 → 直链交付
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("inline_prompt", [False, True])
def test_customer_chain_activation_to_direct_task(
    client: TestClient, chain_dsn: str, inline_prompt: bool
) -> None:
    """Activate, create the project, lock the prompt and submit the batch
    through the customer API, then drain the queue with the real worker:
    the task lands SUCCEEDED/DIRECT, the batch closes, and the fair-queue
    cursor slot is released for the next round."""
    code = generate_activation_code()
    customer = _activated_customer(client, code=code, fingerprint="fp-chain-a", suffix="chain-a")
    session_token = _business_login(client, customer, "idem-login-chain-a")

    project = client.post(
        PROJECTS_PATH,
        json={"name": "E2E 项目"},
        headers=_bearer(session_token),
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    assert project.json()["owner_user_id"] == customer["user_id"]

    # The desktop workspace reads through the same customer session token as
    # its writes.  This is the actual GUI entry gate: accepting POST while
    # rejecting GET would let activation succeed but leave the customer on an
    # unusable empty/error workspace.
    projects = client.get(PROJECTS_PATH, headers=_bearer(session_token))
    assert projects.status_code == 200, projects.text
    assert [item["id"] for item in projects.json()] == [project_id]

    project_detail = client.get(f"{PROJECTS_PATH}/{project_id}", headers=_bearer(session_token))
    assert project_detail.status_code == 200, project_detail.text
    assert project_detail.json()["owner_user_id"] == customer["user_id"]

    shot_card_id, first_frame_id = _seed_chain_assets(
        project_id=project_id, user_id=customer["user_id"]
    )
    prompt_version_id = _create_locked_prompt(
        client,
        session_token=session_token,
        project_id=project_id,
        shot_card_version_id=shot_card_id,
        first_frame_asset_id=first_frame_id,
    )

    read_matrix = (
        (f"{PROJECTS_PATH}/{project_id}/shot-cards/latest", 200),
        # The compact fixture intentionally seeds only the confirmed asset,
        # not a full candidates payload; 409 proves the customer session was
        # accepted and the domain validator (not auth) rejected the fixture.
        (f"{PROJECTS_PATH}/{project_id}/first-frames/latest", 409),
        (f"{PROJECTS_PATH}/{project_id}/first-frames/history", 200),
        (f"{PROJECTS_PATH}/{project_id}/source-frames/latest", 200),
        (f"{PROJECTS_PATH}/{project_id}/scripts/latest", 200),
        (f"{PROJECTS_PATH}/{project_id}/prompts/latest", 200),
        ("/api/simple-characters/library", 200),
    )
    for path, expected_status in read_matrix:
        response = client.get(path, headers=_bearer(session_token))
        assert response.status_code == expected_status, f"{path}: {response.text}"

    batch = client.post(
        f"{PROJECTS_PATH}/{project_id}/generation-batches",
        headers=_bearer(session_token),
        json={
            "quantity": 1,
            **(
                {
                    "prompt_text": "用户当前编辑的最终提示词",
                    "prompt_context": {"source": "manual", "shot_card_version_id": shot_card_id},
                }
                if inline_prompt
                else {"prompt_version_id": prompt_version_id}
            ),
            "first_frame_asset_id": first_frame_id,
            "output_duration_seconds": 4,
            "resolution": "768P",
            "idempotency_key": "e2e-batch-chain-a",
            "provider": "fake_h3",
        },
    )
    assert batch.status_code == 200, batch.text
    payload = batch.json()
    assert payload["status"] == "QUEUED", payload
    assert payload["tasks"][0]["status"] == "PENDING", payload
    assert payload["tasks"][0]["prompt_snapshot"]["status"] == "LOCKED", payload
    if inline_prompt:
        assert payload["tasks"][0]["prompt_snapshot"]["prompt_text"] == "用户当前编辑的最终提示词"
        assert payload["prompt_version_id"] != prompt_version_id
        replay = client.post(
            f"{PROJECTS_PATH}/{project_id}/generation-batches",
            headers=_bearer(session_token),
            json={
                "quantity": 1,
                "prompt_text": "用户当前编辑的最终提示词",
                "prompt_context": {"source": "manual", "shot_card_version_id": shot_card_id},
                "first_frame_asset_id": first_frame_id,
                "output_duration_seconds": 4,
                "resolution": "768P",
                "idempotency_key": "e2e-batch-chain-a",
                "provider": "fake_h3",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == payload["id"]
    batch_id = str(payload["id"])

    batch_detail = client.get(f"/api/generation-batches/{batch_id}", headers=_bearer(session_token))
    assert batch_detail.status_code == 200, batch_detail.text
    assert batch_detail.json()["id"] == batch_id

    batch_list = client.get(
        f"/api/generation-batches?project_id={project_id}",
        headers=_bearer(session_token),
    )
    assert batch_list.status_code == 200, batch_list.text
    assert batch_list.json()["items"][0]["id"] == batch_id

    processed = _drain_pg_worker(chain_dsn)
    assert processed == 1, f"worker must process the one submitted task, got {processed}"

    with psycopg.connect(chain_dsn, autocommit=True) as conn:
        task = conn.execute(
            "SELECT status, archive_status, result_asset_id, locked_by "
            "FROM generation_tasks WHERE batch_id = %s",
            (batch_id,),
        ).fetchone()
        assert task is not None
        assert task[0] == "SUCCEEDED" and task[1] == "DIRECT", task
        assert task[2] is None, "direct success must not copy the result into an asset"
        assert task[3] is None, "lease must be cleared after completion"
        billing = conn.execute(
            "SELECT type, available_delta, reserved_delta FROM wallet_transactions "
            "WHERE task_id = (SELECT id FROM generation_tasks WHERE batch_id = %s) "
            "ORDER BY ledger_sequence",
            (batch_id,),
        ).fetchall()
        assert billing == [("RESERVE", -4, 4), ("SETTLE", 0, -4)]
        batch_row = conn.execute(
            "SELECT status FROM generation_batches WHERE id = %s", (batch_id,)
        ).fetchone()
        assert batch_row is not None
        assert batch_row[0] == "SUCCEEDED", batch_row
        cursor_row = conn.execute(
            "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        assert cursor_row is not None and int(cursor_row[0]) == 0, cursor_row


# ---------------------------------------------------------------------------
# Chain 2: 第二设备 → 冲突 → 切换 → 续充
# ---------------------------------------------------------------------------


def test_customer_chain_second_device_conflict_switch_recharge(
    client: TestClient,
    chain_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both devices stay online and share the same wallet. Recovery replaces
    only the caller's old session; a verified callback credits the account once."""
    code = generate_activation_code()
    customer = _activated_customer(client, code=code, fingerprint="fp-chain-b", suffix="chain-b")
    first_session = _business_login(client, customer, "idem-login-chain-b")

    # --- Second device: enroll (202) → first-device approve → same-key 201 ---
    enroll = client.post(
        ENROLL_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": "fp-chain-b-second",
            "device_name": "第二台设备",
            "device_platform": "macos",
        },
        headers={IDEMPOTENCY_KEY_HEADER: "idem-pair-chain-b"},
    )
    assert enroll.status_code == 202, enroll.text
    pairing_id = str(enroll.json()["pairing_request_id"])

    approval = client.post(
        f"{APPROVE_PATH}/{pairing_id}/approve",
        headers=_bearer(customer["device_token"]),
    )
    assert approval.status_code == 200, approval.text

    consume = client.post(
        ENROLL_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": "fp-chain-b-second",
            "device_name": "第二台设备",
            "device_platform": "macos",
        },
        headers={IDEMPOTENCY_KEY_HEADER: "idem-pair-chain-b"},
    )
    assert consume.status_code == 201, consume.text
    second = consume.json()
    assert second["slot_no"] == 2 and second["device_token"], second
    second_device_token = str(second["device_token"])

    # --- A second device can log in while the first remains online ---
    conflict = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_device_token),
            IDEMPOTENCY_KEY_HEADER: "idem-login-second-chain-b",
        },
    )
    assert conflict.status_code == 201, conflict.text
    assert conflict.json()["session_epoch"] == 1
    previous_second_session = conflict.json()["session_token"]

    # --- Legacy switch recovers only the caller device ---
    switched = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_device_token),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-chain-b",
        },
    )
    assert switched.status_code == 201, switched.text
    switched_payload = switched.json()
    assert switched_payload["session_epoch"] == 2, switched_payload
    assert switched_payload["device_id"] == second["device_id"], switched_payload
    new_session = str(switched_payload["session_token"])

    # --- The first device remains valid; only the old second token is fenced ---
    stale_write = client.post(
        PROJECTS_PATH,
        json={"name": "Stale Session Project"},
        headers=_bearer(first_session),
    )
    assert stale_write.status_code == 201, stale_write.text
    replaced_write = client.post(
        PROJECTS_PATH,
        json={"name": "Expired second session"},
        headers=_bearer(previous_second_session),
    )
    assert replaced_write.status_code == 401
    assert replaced_write.json()["detail"]["code"] == "SESSION_REPLACED"

    # --- Controlled five-yuan acceptance: one named customer only ---
    monkeypatch.setenv(
        "VIDEO_REPLICA_ACCEPTANCE_PAYMENT_USER_ID",
        customer["user_id"],
    )
    monkeypatch.setenv("VIDEO_REPLICA_ACCEPTANCE_PAYMENT_AMOUNT_FEN", "500")
    with psycopg.connect(chain_dsn, autocommit=True) as conn:
        conn.execute("UPDATE runtime_settings SET internal_base_unit_price_fen = 500 WHERE id = 1")

    wallet_view = client.get("/api/customer/wallet", headers=_bearer(new_session))
    assert wallet_view.status_code == 200, wallet_view.text
    assert wallet_view.json()["min_recharge_fen"] == 500
    assert wallet_view.json()["recharge_step_fen"] == 500

    # --- Recharge on the new session: order then signed ZPay callback ---
    order = client.post(
        RECHARGE_PATH,
        json={"amount_fen": 500},
        headers={**_bearer(new_session), IDEMPOTENCY_KEY_HEADER: "idem-recharge-chain-b"},
    )
    assert order.status_code == 201, order.text
    order_payload = order.json()
    assert order_payload["status"] == "PENDING", order_payload
    assert order_payload["credits"] == 1, order_payload
    order_no = str(order_payload["order_no"])

    params = _signed_notify_params(
        order_no,
        trade_no="zpay-chain-b-trade-1",
        amount_yuan="5.00",
    )
    notify = client.get(NOTIFY_PATH, params=params)
    assert notify.status_code == 200, notify.text
    assert notify.text == "success"

    with psycopg.connect(chain_dsn, autocommit=True) as conn:
        order_row = conn.execute(
            "SELECT status, provider_trade_no, paid_at FROM recharge_orders "
            "WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
        assert order_row is not None
        assert order_row[0] == "PAID", "callback must settle the order"
        assert order_row[1] == "zpay-chain-b-trade-1"
        assert order_row[2] is not None
        wallet = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        assert wallet is not None and int(wallet[0]) >= 1
        charge = conn.execute(
            "SELECT available_delta FROM wallet_transactions "
            "WHERE user_id = %s AND type = 'CHARGE' "
            "AND idempotency_key LIKE 'zpay:charge:%%'",
            (customer["user_id"],),
        ).fetchone()
        assert charge is not None and int(charge[0]) == 1
        sessions = conn.execute(
            "SELECT device_id, session_epoch FROM customer_session_state"
        ).fetchall()
        assert dict(sessions) == {customer["device_id"]: 2, second["device_id"]: 2}
        switch_event = conn.execute(
            "SELECT 1 FROM customer_session_events WHERE reason = 'explicit_switch' LIMIT 1"
        ).fetchone()
        assert switch_event is None, "independent login never displaces another device"

    # --- Idempotent callback replay: still success, still one CHARGE ---
    replay = client.get(NOTIFY_PATH, params=params)
    assert replay.status_code == 200 and replay.text == "success"
    with psycopg.connect(chain_dsn, autocommit=True) as conn:
        charges = conn.execute(
            "SELECT count(*) FROM wallet_transactions "
            "WHERE user_id = %s AND type = 'CHARGE' "
            "AND idempotency_key LIKE 'zpay:charge:%%'",
            (customer["user_id"],),
        ).fetchone()
        assert charges is not None and int(charges[0]) == 1
