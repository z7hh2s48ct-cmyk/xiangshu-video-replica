"""Shared PostgreSQL test foundation (CW-007).

Single home for the test-resource contract that every TEST-PG suite shares:

- DSN resolution and an unreachable-database **hard gate** (``pytest.fail``
  by default; skipping requires an explicit developer opt-in via
  ``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1`` and never counts as evidence).
- A recorded test-database allowlist: create/drop helpers refuse any name
  outside the registered prefixes so a mistyped DSN can never truncate or
  drop a developer or production database.
- A repeatable two-customer / three-device seed scenario.
- A file lock that keeps the shared full-suite fixture single-suite.

The kit never falls back to SQLite and never creates non-``*_test``
databases.
"""

from __future__ import annotations

try:
    import fcntl  # Unix-only, used for file locking in tests
except ImportError:
    fcntl = None  # Windows doesn't have this module
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from httpx import Response as ClientResponse


def password_admin_session(client: TestClient, actor_user_id: str) -> ClientResponse:
    """Seed a routine session for business tests, without misusing recovery.

    Authentication-route tests exercise real password verification separately.
    This fixture calls the actual session service on an allowlisted test PG;
    it does not override authorization dependencies or change session rows.
    """
    from app.admin_auth_routes import (
        _exchange_response,
        _set_admin_session_cookie,
        create_password_admin_session,
    )
    from app.db_pg import DATABASE_URL_ENV

    assert_safe_test_database(database_name_of(os.environ[DATABASE_URL_ENV]))
    outgoing = client.build_request("POST", "/api/control/admin/session/password")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": outgoing.url.path,
            "headers": list(outgoing.headers.raw),
            "client": ("testclient", 50000),
            "scheme": outgoing.url.scheme,
            "server": (outgoing.url.host, outgoing.url.port or 80),
        }
    )
    actor, token, csrf, ttl = create_password_admin_session(actor_user_id, request)
    response = Response(status_code=201)
    _set_admin_session_cookie(response, token, ttl)
    result = ClientResponse(
        201,
        json=_exchange_response(actor, csrf).model_dump(),
        headers=dict(response.headers),
        request=outgoing,
    )
    client.cookies.extract_cookies(result)
    return result


# The canonical local fixture (scripts/pg-fixture.sh start) maps host 5433
# to the container's 5432 with trust auth for user postgres.
DEFAULT_TEST_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

# Every database name any suite may create.  Create/drop helpers refuse
# names outside this list so cleanup can never touch a non-test database.
RECORDED_TEST_DATABASES: frozenset[str] = frozenset(
    {
        "t13_customer_activation_test",
        "customer_v3_test",
        # Existing business fixtures using W13 routine admin sessions.
        "t09_admin_session_test",
        "t12_admin_activation_test",
        "t34_admin_audit_test",
        "t23_admin_adjustments_test",
        "w15_dashboard_test",
        "w08_profit_test",
        "w10_rates_test",
        "t34_admin_sessions_test",
        "t16_customer_devices_test",
        "t19_customer_sessions_test",
        "cw027_admin_matrix_test",
        # FIRSTFRAME-RECONCILE admin endpoint suite (dedicated migrated DB).
        "admin_ff_reconcile_test",
        "cw007_kit_alpha_test",
        "cw007_kit_beta_test",
        # CW-010 per-category recovery baselines: each owns a dedicated migrated
        # database created through create_test_database/drop_test_database, so the
        # allowlist guard covers their DROP ... WITH (FORCE).
        "cw010_wallet_billing_test",
        "cw010_independent_test",
        "cw010_oral_test",
        # CW-030 worker PG matrix: dedicated database for the converged
        # worker scheduling/recovery matrix (test_cw030_worker_pg_matrix.py).
        "cw030_worker_matrix_test",
        # CW-054 PG portable contract: dedicated database for executemany /
        # iterdump / set_trace_callback / _NamedRow / type-roundtrip /
        # constraint-exception mapping tests (segment 1/N).
        "cw054_contract_test",
        # CW-056 supported-release-head upgrade matrix: one database per run of
        # the parameterized matrix (empty / 053 / 054 / 055 starting heads) and
        # one dedicated to the failure-state determinism case. Each is created
        # and dropped inside its own test, never shared across tests.
        "cw056_head_matrix_test",
        "cw056_failstate_test",
        # CW-057 maintenance/seed CLI PG entry: dedicated database for the
        # gate1 seed (migrate → seed → pristine-refusal) contract.
        "cw057_gate1_seed_test",
        # CW-058 content/asset domain matrix: one dedicated database for the
        # 内容/版本/工作台/素材/人物/爆款 TEST-PG matrix (truncated per test).
        "cw058_content_asset_test",
        # PROMPT-OPTIMIZE-20260916: the asynchronous「AI 优化提示词」task
        # (test_prompt_optimizer.py), migrated to head and truncated per test.
        "prompt_optimize_route_test",
        # CW-059 billing/task/permission domain matrices: one dedicated database
        # each for the 账务/支付/钱包 matrix (test_cw059_billing_pg_matrix.py),
        # the 任务/Worker/生成 matrix (test_cw059_task_worker_pg_matrix.py) and
        # the 权限/RBAC matrix (test_cw059_rbac_pg_matrix.py), all reset per test
        # and migrated to alembic head so every business table (wallets /
        # wallet_transactions / generation_tasks / image_tasks / script_* /
        # first_frames / source_frames / projects /
        # customer_authorization_evidence ...) is present.
        "cw059_billing_test",
        "cw059_task_test",
        "cw059_rbac_test",
        # CW-043 audit implementation (Segment 4+): one dedicated database each
        # for the 数据看板 analytics matrix (test_cw043_analytics_pg_matrix.py)
        # and the 爆款导入 lease/attempt matrix (test_cw043_viral_import_pg.py).
        # Both fill the ⚠️partial gaps CW043-PG-COVERAGE-MATRIX §5.1.1 #8/#20
        # assigned to CW-043, reset per test and migrated to alembic head.
        "cw043_analytics_test",
        "cw043_viral_import_test",
        # CW-068 C5 发布管理第一阶段（账号授权）：one dedicated database for
        # the 发布账号 TEST-PG suite (test_publish_accounts.py) — accounts CRUD /
        # 凭据不回传 / Fernet 密文落库 / 用户隔离 / verify 租约与 finalize /
        # FOR UPDATE SKIP LOCKED 并发抢占，reset per test and migrated to
        # alembic head so publish_accounts (082_publish_accounts) is present.
        "cw068_publish_accounts_test",
        # CUSTOMER-CENTER-V2 迁移链合并验证（merge revision + cw056 重测基线）。
        "customer_center_v2_merge_test",
        # CUSTOMER-CENTER-V2 子账号 CRUD API 套件（test_sub_account_crud.py）：
        # 独立迁移到 head 的 scratch 库，TRUNCATE 隔离。
        "cw062_sub_account_crud_test",
        # CW-070 WeChat Pay V3 Native settlement: dedicated database for the
        # PG-only wechat_native recharge_order settlement matrix
        # (test_wechat_native_callback_pg.py) — confirm_recharge_payment under
        # WECHAT_NATIVE_SETTLEMENT_SPEC writes the 083 transaction_id column
        # (provider_trade_no stays NULL), credits the wallet once, and stays
        # idempotent on replay. wechat_native orders cannot exist on SQLite
        # (022 provider CHECK + 083 is PostgreSQL-only), so this half of the
        # callback suite only runs on real PostgreSQL, reset per test and
        # migrated to alembic head.
        "cw070_wechat_callback_test",
        # DEDUP-CAS-20260916 content-addressed storage registry: dedicated
        # database for the content_objects reference-count / scope-isolation /
        # reclaim-grace matrix (test_dedup_cas_pg.py), migrated to alembic head
        # so content_objects and assets.content_object_id exist.
        "dedup_cas_test",
        # PUBLISH-DELIVERY-20260917 phase-2 publish records: dedicated database
        # for the records lifecycle / scheduling / per-account claim / finalize
        # matrix (test_publish_records.py), migrated to alembic head so
        # publish_records and the browser-account status columns exist.
        "publish_records_test",
        # Suites still doing their own admin CREATE/DROP with legacy names
        # lacking the _test suffix (rename + kit-helper adoption is owed by a
        # later CW before they may use create_test_database/drop_test_database):
        #   t11_activation_code_service, t34_chain_e2e,
        #   t13c_customer_activation_concurrency, t22r_customer_recharge
        # MATERIAL-PERF-A-20260917 批量素材预览授权：dedicated database for the
        # 批量 download-urls 安全/语义矩阵 (test_material_perf_batch_urls.py),
        # migrated to alembic head and truncated per test.
        "matperf_a_batch_urls_test",
        # MATERIAL-THUMBS-B-20260917 视频素材缩略图：dedicated database for the
        # 缩略图键/批量缩略图签名矩阵 (test_material_thumbs.py), migrated to
        # alembic head and truncated per test.
        "matthumbs_test",
        # S11 ANALYSIS-CANCEL-20260921 取消拆解任务：dedicated database for the
        # 取消语义/计费释放/晚到结果拦截矩阵 (test_analysis_task_cancel.py),
        # migrated to alembic head and truncated per test.
        "analysis_cancel_test",
        # BILLING-OBSERVABILITY-20260922 拆解失败诊断落库：dedicated database for
        # the fail_analysis_task 终态诊断矩阵
        # (test_analysis_failure_diagnostics_pg.py), migrated to alembic head.
        "billing_obs_analysis_test",
        # BILLING-OBSERVABILITY-20260922 管理端拆解可见：dedicated database for
        # the 生成记录 ANALYSIS 分支 + 失败原因聚合矩阵
        # (test_analysis_generation_records_pg.py), migrated to alembic head.
        "billing_obs_records_test",
        # BILLING-OBSERVABILITY-20260922 管理端任务诊断：dedicated database for
        # the 按任务编号/问题编号直查失败历史矩阵
        # (test_analysis_diagnostics_pg.py), migrated to alembic head.
        "billing_obs_diag_test",
        # ADMIN-PROVIDER-PROBES-20260922 管理端付费探针入口：dedicated database
        # for the /api/control/settings/providers/{provider}/paid-test 写契约 /
        # 幂等重放 / 审计矩阵 (test_admin_provider_paid_probe.py), migrated to
        # alembic head and truncated per test.
        "t04_provider_probe_test",
        # ADMIN-P0-20260928 第三方接口调用日志：dedicated database for 原始响应
        # 落库、按第三方任务号/短编号检索生成记录、调用列表与原始响应审计
        # (test_external_calls_pg.py), migrated to alembic head.
        "external_calls_test",
        # CHARACTER-VIEW-APILIO 角色五视图生成链路：dedicated database for the
        # 注册表解析 / 配置缺失回队列 / worker 全链路（任务→质检→计费）矩阵
        # (test_character_image_provider.py), migrated to alembic head and
        # truncated per test.
        "character_generation_test",
    }
)

SHARED_SUITE_DB = "customer_v3_test"

SHARED_SUITE_LOCK_PATH = Path(
    os.environ.get("VIDEO_REPLICA_TEST_SHARED_LOCK", "/tmp/video-replica-pg-shared-suite.lock")
)

ALLOW_PG_SKIP_ENV = "VIDEO_REPLICA_TEST_ALLOW_PG_SKIP"

SEED_USERS = ("cw007-cust-a", "cw007-cust-b")
SEED_DEVICES = (
    # (device_id, owner, code_key, code, slot, fingerprint)
    ("cw007-dev-a1", "cw007-cust-a", "A", "CW07-A-CODE", 1, "fp-cw007-a1"),
    ("cw007-dev-a2", "cw007-cust-a", "A", "CW07-A-CODE", 2, "fp-cw007-a2"),
    ("cw007-dev-b1", "cw007-cust-b", "B", "CW07-B-CODE", 1, "fp-cw007-b1"),
)

_FINGERPRINT_KEY = b"cw007-test-fingerprint-key"
_TOKEN_KEY = b"cw007-test-token-key"


class PgPreflightError(RuntimeError):
    """Raised when the PG test preflight rejects the requested resource."""


def resolve_test_dsn() -> str:
    """Return the test DSN from TEST_POSTGRESQL_URL or the local fixture."""
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_TEST_DSN)


def database_name_of(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def admin_dsn_of(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/postgres"


def assert_safe_test_database(name: str) -> None:
    """Refuse any database outside the recorded test allowlist."""
    if name in {"postgres", "template0", "template1"}:
        raise PgPreflightError(f"refusing to manage system database {name!r}")
    if not name.endswith("_test"):
        raise PgPreflightError(f"refusing to manage {name!r}: test databases must end with '_test'")
    if name not in RECORDED_TEST_DATABASES:
        raise PgPreflightError(
            f"database {name!r} is not in RECORDED_TEST_DATABASES; register it in "
            "server/tests/pg_test_kit.py before use"
        )


def pg_reachable(dsn: str, timeout: float = 3.0) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=int(timeout))
    except Exception:
        return False
    conn.close()
    return True


def require_pg_or_explicit_skip(dsn: str | None = None) -> str:
    """Hard-gate: fail when PG is unreachable unless skipping is explicit.

    Returns the usable DSN.  With ``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1``
    the caller may skip (developer fast-loop only — skipped suites never
    count toward PG acceptance evidence); any other configuration fails.
    """
    target = dsn or resolve_test_dsn()
    # CW-007: a hand-typed TEST_POSTGRESQL_URL must never point the suites
    # (and their admin DROP/CREATE cleanups) at a non-test database.
    assert_safe_test_database(database_name_of(target))
    try:
        conn = psycopg.connect(target, connect_timeout=3)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]
        reason = (
            "PostgreSQL test fixture is not reachable at "
            f"{target}; start it via scripts/pg-fixture.sh start ({detail})"
        )
        if os.environ.get(ALLOW_PG_SKIP_ENV) == "1":
            pytest.skip(reason + " (explicit skip via " + ALLOW_PG_SKIP_ENV + "=1)")
        pytest.fail(reason, pytrace=False)
        raise AssertionError("unreachable")  # pragma: no cover - pytest.fail exits
    conn.close()
    return target


def create_test_database(name: str, *, admin_dsn: str | None = None) -> str:
    """Recreate an allowlisted test database; return its DSN."""
    assert_safe_test_database(name)
    admin = admin_dsn or admin_dsn_of(resolve_test_dsn())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + f"/{name}"


def drop_test_database(name: str, *, admin_dsn: str | None = None) -> None:
    """Drop an allowlisted test database if it exists."""
    assert_safe_test_database(name)
    admin = admin_dsn or admin_dsn_of(resolve_test_dsn())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def upgrade_test_database_to_head(dsn: str) -> None:
    """Run Alembic ``upgrade head`` against a test database."""
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        dsn.replace("postgresql://", "postgresql+psycopg://"),
    )
    command.upgrade(config, "head")


def _digest(payload: str, key: bytes) -> str:
    return hashlib.sha256(key + payload.encode("utf-8")).hexdigest()


def seed_customer_scenario(dsn: str, *, reset: bool = True) -> dict[str, str]:
    """Seed the shared two-customer / three-device scenario, repeatably.

    Creates users ``cw007-cust-a``/``-b`` with one wallet each, one open
    activation batch per customer code, and three BOUND devices
    (A-slot1, A-slot2, B-slot1).  With ``reset=True`` every seeded table is
    truncated first so calling it twice on the same database yields exactly
    the same rows (the CW-007 repeatability contract).
    """
    tables = (
        "customer_session_events, customer_session_state, customer_idempotency_envelopes, "
        "customer_devices, activation_code_events, activation_code_activations, "
        "activation_code_deliveries, activation_code_exports, activation_codes, "
        "activation_code_batches, wallets, users"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        if reset:
            conn.execute(f"TRUNCATE {tables} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")

        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('cw007-cust-a', 'cw007-cust-a', 'Customer A', 'customer'), "
            "('cw007-cust-b', 'cw007-cust-b', 'Customer B', 'customer') "
            "ON CONFLICT (id) DO NOTHING"
        )
        for key, code in (("A", "CW07-A-CODE"), ("B", "CW07-B-CODE")):
            batch_id = f"cw007-batch-{key}"
            conn.execute(
                "INSERT INTO activation_code_batches "
                "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
                "quantity, activation_expires_at, status, created_by_user_id) VALUES "
                "(%s, %s, 1500, 1000, 100, 1, '2099-01-01T00:00:00+00:00', 'OPEN', 'admin_u') "
                "ON CONFLICT (id) DO NOTHING",
                (batch_id, f"cw007-batch-{key}"),
            )
            conn.execute(
                "INSERT INTO activation_codes "
                "(id, batch_id, code_digest, digest_key_version, masked_code, "
                "status, issued_at) VALUES "
                "(%s, %s, %s, 1, 'CW07-****', 'ISSUED', '2026-01-01T00:00:00+00:00') "
                "ON CONFLICT (id) DO NOTHING",
                (f"cw007-code-{key}", batch_id, _digest(code, b"cw007-code-key")),
            )
        for device_id, owner, code_key, code, slot, fingerprint in SEED_DEVICES:
            conn.execute(
                "INSERT INTO customer_devices "
                "(id, activation_code_id, user_id, slot_no, display_name, platform, "
                "fingerprint_hmac, fingerprint_key_version, token_digest, "
                "token_key_version, status) VALUES "
                "(%s, %s, %s, %s, %s, 'windows', %s, 1, %s, 1, 'BOUND') "
                "ON CONFLICT (id) DO NOTHING",
                (
                    device_id,
                    f"cw007-code-{code_key}",
                    owner,
                    slot,
                    f"CW007 {owner} dev{slot}",
                    _digest(fingerprint, _FINGERPRINT_KEY),
                    _digest(f"token-{device_id}", _TOKEN_KEY),
                ),
            )
        for user in SEED_USERS:
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, 100, 0) ON CONFLICT (user_id) DO NOTHING",
                (user,),
            )

        return seed_scenario_facts(dsn)


def seed_scenario_facts(dsn: str) -> dict[str, int]:
    """Return the seeded row counts used by repeatability assertions."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT "
            "(SELECT count(*) FROM users WHERE id LIKE 'cw007-%' OR id = 'admin_u'), "
            "(SELECT count(*) FROM customer_devices WHERE id LIKE 'cw007-dev-%'), "
            "(SELECT count(*) FROM activation_codes WHERE id LIKE 'cw007-code-%'), "
            "(SELECT count(*) FROM wallets WHERE user_id LIKE 'cw007-%')"
        ).fetchone()
        assert row is not None
        return {
            "users": row[0],
            "devices": row[1],
            "codes": row[2],
            "wallets": row[3],
        }


@contextmanager
def shared_suite_lock() -> Iterator[None]:
    """Serialize suites that share the full-suite database (single suite)."""
    if fcntl is None:
        # fcntl 仅 POSIX 可用；单套互斥由 Linux CI 强制执行，Windows 本地开发
        # 退化为无锁（单人串行跑不受影响），避免整个 PG 套件在 setup 阶段崩掉。
        yield
        return
    SHARED_SUITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(SHARED_SUITE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
