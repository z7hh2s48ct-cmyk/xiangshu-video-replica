"""
T03 / DB-01 - PostgreSQL 16 fixture tests (canonical path per V3 frozen file mapping).

Verifies: the local/CI PG fixture is reachable, is PostgreSQL 16, and supports
concurrent independent connections.

Missing PostgreSQL is a **hard failure**, not a skip (CW-007): the module-level
``_require_pg`` autouse fixture calls ``pg_test_kit.require_pg_or_explicit_skip``,
which ``pytest.fail``s unless a developer explicitly opts out with
``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1`` — and an opted-out run never counts as PG
acceptance evidence. The Linux quality gate wires a postgres:16 service and sets
TEST_POSTGRESQL_URL so these tests always run in CI (M0 review H2).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip
from sqlalchemy.engine import make_url

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
# PR#103(df7020c) 引入 pg_test_kit.require_pg_or_explicit_skip 模块级 autouse fixture，
# 取代旧的 SKIP_REASON 常量（已无引用，随 main 基线删除）。
# HEAD_REVISION 取链尾：20260921T0000_merge_wallet_actor_and_billing_metadata 把
# CUSTOMER-CENTER-V2-20260919（sub_accounts → wallet_actor）与 main
# （oral_soft_delete → merge_parallel_heads → add_api_metadata_to_billing_ops）
# 两条并行链线性化，保持 alembic 单头；20260922T1200_recharge_packages（充值套餐）、
# 20260921T1200_sub_account_quotas（Phase 3a 月度额度表）、
# 20260922T1800_sub_account_permissions（Phase 3b 功能权限矩阵表）依次叠加；
# BILLING-OBS-20260922 三个迁移重挂到 sub_account_permissions 之上：
# 20260922T1200_analysis_task_failure_diagnostic 追加 analysis_tasks.upstream_diagnostic_json，
# 20260922T1600_analysis_task_request_id 再追加 analysis_tasks.request_id，
# 20260922T2000_analysis_task_attempts 新建失败历史表 analysis_task_attempts，
# 20260923T0000_open_h3_extended_modes 移除 T2V/R2V/L2V 门禁并 DROP 掉
# runtime_settings.h3_extended_modes_enabled。
# 本分支的 20260922T1500_viral_search_discoveries（爆款视频搜索发现记录表）
# 按手册 §3 重挂于链尾，故链尾（alembic head）为该值。
# 迁移后 alembic 版本头即该值，9 处 assert version == HEAD_REVISION 依赖此值。
HEAD_REVISION = "20260922T1500_viral_search_discoveries"


def test_viral_script_cache_migration_preserves_results_without_task_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    # Reuse the module's isolated migration-rehearsal database, never business data.
    dsn = _rehearsal_dsn()
    _drop_database("t06_migrate_test")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute('CREATE DATABASE "t06_migrate_test"')
    try:
        config = _alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://"))
        command.upgrade(config, "20260914T0000_local_joint_merge")
        command.upgrade(config, HEAD_REVISION)
        with psycopg.connect(dsn, autocommit=True) as conn:
            columns = conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='viral_script_cache' "
                "ORDER BY ordinal_position"
            ).fetchall()
            assert columns == [
                ("platform", "text", "NO"),
                ("video_id", "text", "NO"),
                ("producer_task_id", "text", "YES"),
                ("result_json", "text", "YES"),
                ("created_at", "timestamp with time zone", "NO"),
                ("updated_at", "timestamp with time zone", "NO"),
            ]
            assert conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='viral_script_cache'::regclass "
                "AND contype IN ('f', 'c', 'u')"
            ).fetchone() == (0,)
            result_json = json.dumps({"full_text": "缓存文案", "segments": []}, ensure_ascii=False)
            conn.execute(
                "INSERT INTO viral_script_cache(platform, video_id, producer_task_id, result_json) "
                "VALUES ('douyin', 'same-id', 'deleted-producer', %s)",
                (result_json,),
            )
            conn.execute(
                "INSERT INTO viral_script_cache(platform, video_id) "
                "VALUES ('wechat_channels', 'same-id')"
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO viral_script_cache(platform, video_id) "
                    "VALUES ('douyin', 'same-id')"
                )
        command.upgrade(config, HEAD_REVISION)
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT result_json, created_at IS NOT NULL, updated_at IS NOT NULL "
                "FROM viral_script_cache WHERE platform='douyin' AND video_id='same-id'"
            ).fetchone() == (result_json, True, True)
        command.downgrade(config, "20260914T0000_local_joint_merge")
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT to_regclass('public.viral_script_cache')").fetchone() == (
                None,
            )
    finally:
        _drop_database("t06_migrate_test")


def test_customer_batch_visibility_migration_preserves_generation_and_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    db_name = "customer_batch_visibility_migration_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    config = _alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://"))
    stable_columns = {
        "users": "id, username, display_name",
        "projects": "id, owner_user_id, name",
        "generation_batches": (
            "id, project_id, created_by_user_id, idempotency_key, "
            "request_hash, request_snapshot_json"
        ),
        "generation_tasks": "id, batch_id, provider, model",
        "wallets": "user_id, available_credits, reserved_credits",
        "wallet_transactions": (
            "id, user_id, type, available_delta, reserved_delta, task_id, "
            "billing_round, idempotency_key"
        ),
    }

    def preserved_rows(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: conn.execute(
                f"SELECT {columns} FROM {table} ORDER BY 1"  # noqa: S608 - fixed test schema
            ).fetchall()
            for table, columns in stable_columns.items()
        }

    try:
        command.upgrade(config, "054_admin_free_grant_adjustments")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'Project')"
            )
            conn.execute(
                "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
                "idempotency_key, request_hash, request_snapshot_json) "
                "VALUES ('b1', 'p1', 'u1', 'key', 'hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('t1', 'b1', 'apilio', 'h3')"
            )
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES ('u1', 9, 1)"
            )
            conn.execute(
                "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
                "reserved_delta, task_id, billing_round, idempotency_key) "
                "VALUES ('tx1', 'u1', 'RESERVE', -1, 1, 't1', 1, 'reserve:t1:1')"
            )
            before = preserved_rows(conn)
        command.upgrade(config, "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO customer_batch_visibility (user_id, batch_id) VALUES ('u1', 'b1')"
            )
            assert conn.execute("SELECT hidden_at FROM customer_batch_visibility").fetchone()[0]
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "INSERT INTO customer_batch_visibility (user_id, batch_id) "
                    "VALUES ('missing', 'b1')"
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "INSERT INTO customer_batch_visibility (user_id, batch_id) "
                    "VALUES ('u1', 'missing')"
                )
        command.downgrade(config, "054_admin_free_grant_adjustments")
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute("SELECT to_regclass('customer_batch_visibility')").fetchone()[0]
                is None
            )
            assert preserved_rows(conn) == before
        command.upgrade(config, "head")
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM customer_batch_visibility").fetchone()[0] == 0
            assert preserved_rows(conn) == before
    finally:
        _drop_database(db_name)


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _expected_fixture_identity() -> tuple[str, str]:
    """(role, database) the configured fixture DSN asks to connect as.

    Derived from ``TEST_POSTGRESQL_URL`` instead of hardcoding ``testuser`` /
    ``customer_v3_test``. Those are the CI fixture's names, and pinning them made
    these two smoke tests fail on every other legitimate base — e.g. a developer's
    own postgres:16 container on a different port and role — even though the
    fixture was perfectly healthy. The invariant worth asserting is that the
    connection actually lands on the requested role and database (no redirect, no
    silent default), not that one environment's names are baked into the suite.
    Which DSNs may be used at all is guarded separately by
    ``pg_test_kit.assert_safe_test_database``.
    """
    dsn = _pg_dsn()
    url = make_url(dsn)
    if not url.username or not url.database:
        raise AssertionError(f"fixture DSN must name a role and a database: {dsn!r}")
    return url.username, url.database


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _run(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    return asyncio.run(coro_fn())


def test_database_creation() -> None:
    _, expected_database = _expected_fixture_identity()

    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SELECT current_database();")
            assert result == expected_database, f"unexpected database {result}"
        finally:
            await conn.close()

    _run(case)


def test_user_identity() -> None:
    expected_user, _ = _expected_fixture_identity()

    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SELECT current_user;")
            assert result == expected_user, f"unexpected user {result}"
        finally:
            await conn.close()

    _run(case)


def test_postgres_version() -> None:
    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SHOW server_version;")
            assert str(result).startswith("16."), f"expected PostgreSQL 16.x, got {result}"
        finally:
            await conn.close()

    _run(case)


def test_independent_connections() -> None:
    async def case() -> None:
        conn1 = await asyncpg.connect(_pg_dsn())
        conn2 = await asyncpg.connect(_pg_dsn())
        try:
            assert await conn1.fetchval("SELECT 1;") == 1
            assert await conn2.fetchval("SELECT 2;") == 2
        finally:
            await conn1.close()
            await conn2.close()

    _run(case)


# ---------------------------------------------------------------------------
# T06 / DB-04 — full upgrade/downgrade/re-upgrade rehearsal on PostgreSQL
# ---------------------------------------------------------------------------


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _rehearsal_dsn() -> str:
    """Dedicated database for the rehearsal (downgrade drops every table)."""
    return _pg_dsn().rsplit("/", 1)[0] + "/t06_migrate_test"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_wallet_ledger_sequence_migration_is_reversible_on_postgres() -> None:
    from alembic import command

    database_name = "wallet_ledger_sequence_migration_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{database_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(database_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database_name}"')

    try:
        config = _alembic_config(sqlalchemy_dsn)
        command.upgrade(config, "062_activation_initial_free_seconds")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) "
                "VALUES ('ledger-user', 'ledger-user', 'Ledger User')"
            )
            conn.execute("INSERT INTO wallets (user_id) VALUES ('ledger-user')")
            conn.execute(
                "INSERT INTO recharge_orders "
                "(id, user_id, merchant_order_no, provider, provider_trade_no, channel, status, "
                "pricing_scope, "
                "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
                "paid_at) "
                "VALUES ('ledger-order-historical', 'ledger-user', 'ledger-merchant-historical', "
                "'zpay', 'ledger-trade-historical', 'alipay', 'PAID', 'INTERNAL', "
                "1000, 1000, 10000, 1000, 10000, 10, "
                "'2026-09-05T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                "idempotency_key) VALUES ('ledger-tx-historical', 'ledger-user', 'CHARGE', 10, 0, "
                "'ledger-order-historical', 'ledger-key-historical')"
            )
        command.upgrade(config, "063_wallet_ledger_sequence")
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'wallet_transactions' AND column_name = 'ledger_sequence'"
            ).fetchone()
            assert conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = "
                "'trg_wallet_transactions_assign_ledger_sequence' AND NOT tgisinternal"
            ).fetchone()
            assert conn.execute(
                "SELECT 1 FROM pg_indexes WHERE tablename='wallet_transactions' "
                "AND indexname='idx_wallet_transactions_user_ledger_sequence'"
            ).fetchone()
            assert conn.execute(
                "SELECT ledger_sequence FROM wallet_transactions WHERE id='ledger-tx-historical'"
            ).fetchone() == (None,)

        with psycopg.connect(dsn, autocommit=True) as conn:
            for suffix in ("one", "two"):
                conn.execute(
                    "INSERT INTO recharge_orders "
                    "(id, user_id, merchant_order_no, provider, provider_trade_no, channel, "
                    "status, pricing_scope, "
                    "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                    "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
                    "paid_at) "
                    "VALUES (%s, 'ledger-user', %s, 'zpay', %s, 'alipay', 'PAID', 'INTERNAL', "
                    "1000, 1000, 10000, 1000, 10000, 10, '2026-09-05T00:00:00+00:00')",
                    (
                        f"ledger-order-{suffix}",
                        f"ledger-merchant-{suffix}",
                        f"ledger-trade-{suffix}",
                    ),
                )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                "idempotency_key) VALUES ('ledger-tx-one', 'ledger-user', 'CHARGE', 10, 0, "
                "'ledger-order-one', 'ledger-key-one')"
            )
            sequence = conn.execute(
                "SELECT ledger_sequence FROM wallet_transactions WHERE id='ledger-tx-one'"
            ).fetchone()[0]
            assert sequence is not None
            with pytest.raises(psycopg.Error, match="database assigned"):
                conn.execute(
                    "INSERT INTO wallet_transactions "
                    "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                    "idempotency_key, ledger_sequence) VALUES "
                    "('ledger-tx-two', 'ledger-user', 'CHARGE', 10, 0, "
                    "'ledger-order-two', 'ledger-key-two', 999999)"
                )
            with pytest.raises(psycopg.Error, match="immutable"):
                conn.execute(
                    "UPDATE wallet_transactions SET ledger_sequence=%s WHERE id='ledger-tx-one'",
                    (sequence + 1,),
                )

        command.downgrade(config, "062_activation_initial_free_seconds")
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'wallet_transactions' "
                    "AND column_name = 'ledger_sequence'"
                ).fetchone()
                is None
            )
            assert (
                conn.execute(
                    "SELECT 1 FROM pg_indexes WHERE tablename='wallet_transactions' "
                    "AND indexname='idx_wallet_transactions_user_ledger_sequence'"
                ).fetchone()
                is None
            )
            assert (
                conn.execute(
                    "SELECT 1 FROM pg_trigger WHERE tgname = "
                    "'trg_wallet_transactions_assign_ledger_sequence' AND NOT tgisinternal"
                ).fetchone()
                is None
            )

        command.upgrade(config, "063_wallet_ledger_sequence")
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'wallet_transactions' AND column_name = 'ledger_sequence'"
            ).fetchone()
    finally:
        _drop_database(database_name)


def test_hifly_migration_preserves_existing_zpay_settings() -> None:
    """Production databases at 055 already contain zpay credentials."""
    from alembic import command

    database_name = "hifly_zpay_compatibility_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{database_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(database_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database_name}"')

    try:
        config = _alembic_config(sqlalchemy_dsn)
        command.upgrade(config, "063_wallet_ledger_sequence")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO provider_settings(provider,encrypted_config) VALUES ('zpay','test')"
            )
        command.upgrade(config, "069_provider_whitelist_widen")
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT encrypted_config FROM provider_settings WHERE provider='zpay'"
            ).fetchone() == ("test",)
            conn.execute(
                "INSERT INTO provider_settings(provider,encrypted_config) "
                "VALUES ('hifly','test'),('tikhub','test')"
            )
    finally:
        _drop_database(database_name)


def test_empty_customer_bootstrap_runs_on_a_fresh_migrated_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T36: prove the documented first-install path against the real PG schema."""
    from alembic import command
    from cryptography.fernet import Fernet

    from app import bootstrap
    from app.db_pg import close_pg_pool

    # CW-025: reset the process-wide pool singleton so this case re-resolves its
    # own t36 DSN instead of inheriting a pool cached by an earlier test in the
    # same pytest process (get_pg_pool() caches the DSN on first use). Matches
    # the standalone-test convention in test_customer_devices.py.
    close_pg_pool()

    database_name = "t36_empty_customer_bootstrap_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{database_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    settings_key = Fernet.generate_key().decode("ascii")
    _drop_database(database_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database_name}"')

    command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_key)
    monkeypatch.setenv("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_V2", "k" * 64)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    # The disposable postgres:16 fixture has no server certificate. Dedicated
    # db_pg tests cover the production TLS validator; this integration case
    # keeps the real schema, pool and transaction while bypassing only TLS.
    monkeypatch.setattr(bootstrap, "validate_customer_production", lambda _config: None)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)

    try:
        result = bootstrap.provision_empty_customer(
            admin_username="first-admin",
            admin_display_name="First Admin",
            cos_config={
                "access_key_id": "placeholder-access-id",
                "secret_access_key": "placeholder-secret",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
            },
            confirm_empty_database=True,
        )

        with psycopg.connect(dsn) as conn:
            user = conn.execute(
                "SELECT username, display_name, role, is_active FROM users WHERE id = %s",
                (result.admin_user_id,),
            ).fetchone()
            assert user == ("first-admin", "First Admin", "admin", 1)
            assert conn.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
                (result.admin_user_id,),
            ).fetchone() == (0, 0)
            encrypted_cos = conn.execute(
                "SELECT encrypted_config FROM provider_settings WHERE provider = 'cos'"
            ).fetchone()[0]
            assert "placeholder-secret" not in encrypted_cos
            decrypted_cos = Fernet(settings_key.encode("ascii")).decrypt(
                encrypted_cos.encode("ascii")
            )
            assert json.loads(decrypted_cos) == {
                "access_key_id": "placeholder-access-id",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
                "secret_access_key": "placeholder-secret",
            }
            assert conn.execute(
                "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
            ).fetchone() == ("cos",)
            audit = conn.execute(
                "SELECT metadata_json FROM audit_logs WHERE action = 'customer_bootstrap.provision'"
            ).fetchone()[0]
            assert "placeholder-secret" not in audit

        with pytest.raises(RuntimeError, match="pristine, fully migrated PostgreSQL database"):
            bootstrap.provision_empty_customer(
                admin_username="second-admin",
                admin_display_name="Second Admin",
                cos_config={
                    "access_key_id": "other-access-id",
                    "secret_access_key": "other-secret",
                    "bucket": "other-private-bucket",
                    "region": "ap-shanghai",
                },
                confirm_empty_database=True,
            )
    finally:
        bootstrap.close_pg_pool()
        _drop_database(database_name)


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def test_pg_upgrade_from_published_040_head_applies_fair_queue() -> None:
    """M5 review P1-3: a database already stamped at the published 040 head
    (the pre-M5 chain, where 032 descends from 029) must APPLY the fair-queue
    revision on ``upgrade head`` — the earlier 032 re-pointing made 030 an
    ancestor of 032 and silently skipped the schema on such databases."""
    from alembic import command

    dsn = _rehearsal_dsn()
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database("t06_migrate_test")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute('CREATE DATABASE "t06_migrate_test"')

    try:
        # Stage 1: bring the database to the released 040 head — the exact
        # state of a production DB upgraded before the M5 branch landed.
        command.upgrade(_alembic_config(sqlalchemy_dsn), "040_fix_provider_settings_constraint")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == "040_fix_provider_settings_constraint"
            fair_queue_column = conn.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'runtime_settings' AND column_name = 'fair_queue_enabled'"
            ).fetchone()[0]
            assert int(fair_queue_column) == 0  # no fair-queue schema yet

        # Stage 2: upgrade head — the 041 revision must run (it sits at the
        # tail, so it is not on the stamped database's ancestor path).
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION
            fair_queue_column = conn.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'runtime_settings' AND column_name = 'fair_queue_enabled'"
            ).fetchone()[0]
            assert int(fair_queue_column) == 1
            cursor_table = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name = 'user_queue_cursors'"
            ).fetchone()[0]
            assert int(cursor_table) == 1
    finally:
        _drop_database("t06_migrate_test")


def test_pg_full_upgrade_downgrade_reupgrade_and_indexes() -> None:
    """DB-04 rehearsal: empty PG database, upgrade to head, verify key tables/
    constraints, downgrade to base, then re-upgrade to head. Historical
    revisions keep their SQLite behaviour; PG-only branches are dialect-guarded
    inside the revisions and env.py."""
    from alembic import command

    dsn = _rehearsal_dsn()
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database("t06_migrate_test")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute('CREATE DATABASE "t06_migrate_test"')

    try:
        # Stage 1: upgrade to head on the empty database.
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")

        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION, f"unexpected head revision: {version}"

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                ).fetchall()
            }
            for required in (
                "users",
                "projects",
                "characters",
                "generation_batches",
                "generation_tasks",
                "wallets",
                "wallet_transactions",
                "recharge_orders",
                "admin_sessions",
                "character_generation_tasks",
                "external_call_logs",
                "analysis_tasks",
                "first_frame_tasks",
                "character_sheet_tasks",
                "source_frame_tasks",
                "script_rewrite_tasks",
                "studio_material_preferences",
            ):
                assert required in tables, f"missing table {required} after upgrade head"

            constraints = {
                row[0]
                for row in conn.execute(
                    "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                ).fetchall()
            }
            assert "uq_generation_batches_user_project_key" in constraints
            assert "generation_tasks_batch_id_fkey" in constraints, (
                "009 must re-attach the FK on PG"
            )
            scene_operation_constraint = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_character_sheet_tasks_operation'"
            ).fetchone()[0]
            assert "'SCENE'" in scene_operation_constraint

            # Partial unique indexes must exist on PG with their WHERE clauses
            # (sqlite_where is silently ignored by PG — DB-04/P1 review).
            index_defs = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                ).fetchall()
            }
            assert any(
                name.startswith("idx_generation_tasks_prompt_version") for name in index_defs
            )
            for name in (
                "idx_first_frame_tasks_created_by",
                "idx_first_frame_tasks_result_version",
                "idx_character_sheet_tasks_project",
                "idx_character_sheet_tasks_identity",
                "idx_character_sheet_tasks_result_identity",
                "idx_character_sheet_tasks_result_version",
                "idx_source_frame_tasks_result_version",
                "idx_generation_task_operations_reconcile_claim",
                "idx_script_rewrite_tasks_project_identity_created",
            ):
                assert name in index_defs, f"foreign-key support index {name} missing on PG"
            expected_partial = {
                "uq_character_assets_published_view": "is_published_selection = 1",
                "uq_generation_task_operations_pending": "result_status = 'PENDING'",
                "uq_recharge_orders_provider_trade_no": "provider_trade_no IS NOT NULL",
                "uq_wallet_transactions_charge_order": "type = 'CHARGE'",
                "uq_wallet_transactions_reserve_round": "type = 'RESERVE'",
                # C1 regression lock: 022 shipped sqlite_where-only; the
                # predicate is restored append-only by 025 on PG.
                "uq_wallet_transactions_terminal_round": (
                    "ANY (ARRAY['SETTLE'::text, 'RELEASE'::text])"
                ),
                "uq_analysis_tasks_active_asset": (
                    "status = ANY (ARRAY['PENDING'::text, 'RUNNING'::text])"
                ),
                "uq_first_frame_tasks_active_request": (
                    "status = ANY (ARRAY['PENDING'::text, 'RUNNING'::text])"
                ),
                "uq_character_sheet_tasks_active_request": (
                    "status = ANY (ARRAY['PENDING'::text, 'RUNNING'::text])"
                ),
                "uq_source_frame_tasks_active_asset": (
                    "status = ANY (ARRAY['PENDING'::text, 'RUNNING'::text])"
                ),
                "uq_script_rewrite_tasks_active_project": (
                    "status = ANY (ARRAY['PENDING'::text, 'RUNNING'::text])"
                ),
            }
            for name, predicate in expected_partial.items():
                assert name in index_defs, f"partial unique index {name} missing on PG"
                assert predicate in index_defs[name], (
                    f"{name} must be a partial index with WHERE {predicate}, "
                    f"got: {index_defs[name]}"
                )

        # Stage 2: downgrade all the way to base.
        command.downgrade(_alembic_config(sqlalchemy_dsn), "base")
        with psycopg.connect(dsn) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                ).fetchall()
            }
            assert "users" not in tables, "downgrade to base must drop business tables"

        # Stage 3: re-upgrade to head (rehearsal of a rolled-back deployment).
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION
    finally:
        _drop_database("t06_migrate_test")


def test_pg_wallet_terminal_round_row_level() -> None:
    """C1 row-level regression: the billing lifecycle must work on PG.

    internal_billing writes RESERVE first and the terminal SETTLE (or
    RELEASE) afterwards with the *same* (task_id, billing_round). With the
    degraded table-wide unique index from 022 the terminal insert was
    rejected; with the 025 partial index it must succeed, while a second
    terminal row on the same key must still be rejected.
    """
    from alembic import command

    db_name = "m0_c1_row_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    def insert_tx(
        conn: psycopg.Connection, tx_id: str, tx_type: str, avail: int, reserved: int
    ) -> None:
        conn.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (tx_id, "u1", tx_type, avail, reserved, "t1", 1, f"{tx_type.lower()}:t1:1"),
        )

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User One')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'C1 Repro')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json) "
                "VALUES ('b1', 'p1', 'u1', 'b1-idem', 'b1-hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('t1', 'b1', 'apilio', 'test-model')"
            )
            conn.execute("INSERT INTO wallets (user_id) VALUES ('u1')")

            # RESERVE then SETTLE on the same (task_id, billing_round):
            # the exact sequence that failed with 022's degraded index.
            insert_tx(conn, "tx1", "RESERVE", -1, 1)
            insert_tx(conn, "tx2", "SETTLE", 0, -1)

            # A second terminal row on the same key must still be rejected
            # by the partial unique index (idempotency of the terminal side).
            with pytest.raises(psycopg.errors.UniqueViolation):
                insert_tx(conn, "tx3", "RELEASE", 1, -1)
    finally:
        _drop_database(db_name)


def test_pg_wallet_downgrade_blocked_when_ledger_has_settled_rounds() -> None:
    """Review P1 regression: 025 downgrade must refuse (loudly, with the
    recovery path) once the ledger legitimately holds a RESERVE row plus a
    terminal row sharing (task_id, billing_round) — recreating the 022
    table-wide unique index would raise a uniqueness violation and brick the
    rollback. An empty ledger still downgrades symmetrically (Stage 2 of the
    rehearsal above)."""
    from alembic import command

    db_name = "m0_p1_downgrade_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User One')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'P1 Repro')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json) "
                "VALUES ('b1', 'p1', 'u1', 'b1-idem', 'b1-hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('t1', 'b1', 'apilio', 'test-model')"
            )
            conn.execute("INSERT INTO wallets (user_id) VALUES ('u1')")
            for tx_id, tx_type, avail, reserved in (
                ("tx1", "RESERVE", -1, 1),
                ("tx2", "SETTLE", 0, -1),
            ):
                conn.execute(
                    "INSERT INTO wallet_transactions "
                    "(id, user_id, type, available_delta, reserved_delta, "
                    " task_id, billing_round, idempotency_key) "
                    "VALUES (%s, 'u1', %s, %s, %s, 't1', 1, %s)",
                    (tx_id, tx_type, avail, reserved, f"{tx_type.lower()}:t1:1"),
                )

        with pytest.raises(RuntimeError, match="cannot downgrade 025"):
            command.downgrade(_alembic_config(sqlalchemy_dsn), "024_wallet_backfill")

        # The database must be left exactly at head (no partial rollback).
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == HEAD_REVISION
    finally:
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# T08 / DB-07 — billing provider / pricing_scope conditional constraints
# ---------------------------------------------------------------------------

_T08_BASELINE_ORDER = {
    "id": "o-t08",
    "user_id": "u-t08",
    "merchant_order_no": "T08-ORDER",
    "provider": "zpay",
    "provider_trade_no": None,
    "channel": "alipay",
    "status": "PENDING",
    "pricing_scope": "INTERNAL",
    "base_unit_price_fen_snapshot": 1000,
    "charged_unit_price_fen_snapshot": 1000,
    "min_recharge_fen_snapshot": 10000,
    "recharge_step_fen_snapshot": 1000,
    "amount_fen": 10000,
    "credits": 10,
    "paid_at": None,
}


def _t08_database(db_name: str) -> str:
    from alembic import command

    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    command.upgrade(_alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://")), "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('u-t08', 'u-t08', 'T08')"
        )
    return dsn


def _insert_t08_order(conn: psycopg.Connection, seq: int, **overrides: object) -> None:
    row = {
        **_T08_BASELINE_ORDER,
        "id": f"o-t08-{seq}",
        "merchant_order_no": f"T08-ORDER-{seq}",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO recharge_orders ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


@pytest.mark.parametrize(
    "amount_fen, refusal",
    [(0, "zero-amount FREE_GRANT adjustment orders"), (1000, "FREE_GRANT audit rows")],
)
def test_pg_free_grant_downgrade_preserves_ledger(
    monkeypatch: pytest.MonkeyPatch, amount_fen: int, refusal: str
) -> None:
    from alembic import command

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    database_name = "t54_free_grant_downgrade_guard"
    tables = ("recharge_orders", "wallet_transactions", "wallets", "admin_adjustments")
    try:
        dsn = _t08_database(database_name)
        config = _alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://"))
        # Exercise the historic 054 guard before any new funding facts exist.
        command.downgrade(config, "20260913T0630_account_credit_operations")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('t54_admin', 't54_admin', 'Admin', 'admin')"
            )
            _insert_t08_order(
                conn,
                54,
                provider="admin_adjustment",
                status="PAID",
                channel=None,
                amount_fen=amount_fen,
                credits=1,
                paid_at="2026-09-03T00:00:00+00:00",
            )
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES ('u-t08', 1, 0)"
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, "
                "recharge_order_id, idempotency_key) "
                "VALUES ('t54_charge', 'u-t08', 'CHARGE', 1, 0, 'o-t08-54', 't54_charge')"
            )
            # A positive amount is synthetic data for the second schema guard;
            # the API always records FREE_GRANT with a zero payment amount.
            conn.execute(
                "INSERT INTO admin_adjustments "
                "(id, recharge_order_id, target_user_id, admin_user_id, "
                "source_document_type, source_document_ref, reason, request_id) "
                "VALUES ('t54_audit', 'o-t08-54', 'u-t08', 't54_admin', "
                "'FREE_GRANT', 'GRANT-54', 'rollback rehearsal', 't54_request')"
            )
            before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}

        with pytest.raises(RuntimeError, match=f"cannot downgrade 054.*{refusal}"):
            command.downgrade(config, "053_activation_code_archive")

        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "20260913T0630_account_credit_operations",
            )
            for table in tables:
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == before[table]
    finally:
        _drop_database(database_name)


def test_pg_billing_provider_shapes_accepted_and_rejected() -> None:
    """DB-07 exit gate: zpay / activation_code / admin_adjustment legal shapes
    pass, illegal shapes are rejected by PostgreSQL check constraints — not by
    application code (No-Go rule)."""

    db_name = "t08_billing_shapes"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            # --- legal shapes -------------------------------------------------
            # zpay + INTERNAL + PENDING: the existing internal recharge flow.
            _insert_t08_order(conn, 1)
            # zpay + CUSTOMER_STANDARD + PAID with trade number: customer
            # top-up through ZPay (T22) with a frozen customer sale price.
            _insert_t08_order(
                conn,
                2,
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=15000,
                credits=10,
                provider_trade_no="ZPAY-TRADE-2",
                paid_at="2026-08-22T00:00:00+00:00",
            )
            # activation_code + CUSTOMER_STANDARD + PAID, no third-party trade
            # number, face value below the internal minimum recharge (the
            # batch face value decides, min/step ladders do not apply).
            _insert_t08_order(
                conn,
                3,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
                channel=None,
            )
            # admin_adjustment + INTERNAL + PAID, no trade number, amount below
            # the minimum recharge and off the step ladder (adjustments are
            # defined by their audited source document).
            _insert_t08_order(
                conn,
                4,
                provider="admin_adjustment",
                status="PAID",
                amount_fen=5000,
                credits=5,
                paid_at="2026-08-22T00:00:00+00:00",
                channel=None,
            )

            # A CHARGE transaction referencing the activation_code order keeps
            # the existing append-only ledger shape (provider-agnostic).
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, "
                " recharge_order_id, idempotency_key) "
                "VALUES ('tx-t08-3', 'u-t08', 'CHARGE', 1, 0, 'o-t08-3', 'charge:o-t08-3')"
            )

            # --- illegal shapes ----------------------------------------------
            def rejected(seq: int, **overrides: object) -> None:
                with pytest.raises(psycopg.errors.CheckViolation):
                    _insert_t08_order(conn, seq, **overrides)

            rejected(10, provider="wechat")  # unknown provider
            rejected(11, pricing_scope="CHANNEL_A")  # scope frozen until PRICE-01
            rejected(
                12,
                provider="activation_code",
                status="PAID",
                paid_at="2026-08-22T00:00:00+00:00",
            )  # scope pairing: activation_code is customer-only
            rejected(
                13,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PENDING",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
            )  # activation must land PAID atomically
            rejected(
                14,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                provider_trade_no="X",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
            )  # no third-party trade number for activation codes
            rejected(
                15,
                provider="admin_adjustment",
                amount_fen=5000,
                credits=5,
            )  # adjustments must land PAID atomically (default PENDING here)
            rejected(
                16,
                status="PAID",
                provider_trade_no=None,
                paid_at="2026-08-22T00:00:00+00:00",
            )  # a paid ZPay order must carry its provider trade number
            rejected(22, provider_trade_no="ZPAY-SQUAT")  # a non-PAID ZPay order must not
            #   reserve a globally unique third-party trade number (review P2)
            _insert_t08_order(
                conn,
                17,
                pricing_scope="CUSTOMER_STANDARD",
                charged_unit_price_fen_snapshot=500,
                amount_fen=10000,
                credits=20,
            )  # customer sale price is independent from the internal base snapshot
            rejected(18, amount_fen=9000, credits=9)  # zpay: below min recharge
            rejected(19, amount_fen=10500)  # zpay: off the recharge step ladder
            rejected(20, credits=9)  # credits * price != amount (all providers)
            rejected(21, status="REFUNDED")  # unknown status (022 regression)
    finally:
        _drop_database(db_name)


def test_pg_customer_unit_price_schema_and_constraints() -> None:
    db_name = "customer_unit_prices"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('admin-price', 'admin-price', 'Pricing Admin', 'admin')"
            )
            conn.execute(
                "INSERT INTO customer_unit_prices "
                "(user_id, unit_price_fen, updated_by_user_id) "
                "VALUES ('u-t08', 500, 'admin-price')"
            )
            row = conn.execute(
                "SELECT unit_price_fen, updated_by_user_id "
                "FROM customer_unit_prices WHERE user_id = 'u-t08'"
            ).fetchone()
            assert row == (500, "admin-price")

            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE customer_unit_prices SET unit_price_fen = 0 WHERE user_id = 'u-t08'"
                )
    finally:
        _drop_database(db_name)


def test_pg_admin_sessions_schema_and_invariants() -> None:
    """026 must carry the full frozen topic (review P1): the admin_sessions
    data layer for T09/DB-08 — digests only, unique session digest, expiry
    ordering, actor FK — lands in the same revision as the billing
    constraints so T09 is never left without a compliant schema home."""

    db_name = "t08_admin_sessions"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('u-admin', 'u-admin', 'Admin', 'admin')"
            )
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('as1', 'u-admin', 'digest-1', 'csrf-1', "
                " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                " 'ip-digest', 'ua-digest')"
            )
            # A second session for the same actor is fine (session rotation),
            # but the session digest is globally unique.
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('as2', 'u-admin', 'digest-2', 'csrf-2', "
                " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                " 'ip-digest', 'ua-digest')"
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as3', 'u-admin', 'digest-1', 'csrf-3', "
                    " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as4', 'u-admin', 'digest-4', 'csrf-4', "
                    " '2026-08-22T00:00:01+00:00', '2026-08-21T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )  # expires_at must be after created_at
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as5', 'u-missing', 'digest-5', 'csrf-5', "
                    " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )  # every session must trace back to a real actor

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'admin_sessions'"
                ).fetchall()
            }
            assert any(name.startswith("idx_admin_sessions_actor_status") for name in indexes)
    finally:
        _drop_database(db_name)


def test_pg_billing_constraints_downgrade_guard() -> None:
    """026 downgrade refuses (loudly) once non-zpay / non-INTERNAL orders exist;
    an empty ledger downgrades symmetrically back to the 022 constraint set."""

    from alembic import command

    db_name = "t08_downgrade_guard"
    dsn = _t08_database(db_name)
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _insert_t08_order(
                conn,
                1,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
                channel=None,
            )

        with pytest.raises(RuntimeError, match="cannot downgrade 026"):
            # Nineteen steps from head: 1400->090->089->083->086->081 (empty
            # registration-credential + discount + api-key + multi-provider +
            # device-slot layer, symmetric) then 054->053 (empty free-grant layer,
            # symmetric on a fresh database) then 039->038 (empty admin adjustments
            # layer, symmetric) then 038->037 (empty admin device operations
            # layer, symmetric) then 037->036 (empty device pairing layer,
            # symmetric) then 036->035 (guards drop symmetrically on an empty
            # guard layer) then 034->033 (empty cross-version probe key layer,
            # symmetric) then 033->032 (empty batch-creation audit layer,
            # symmetric) then 032->029 (empty security rate-limit layer,
            # symmetric) then 029->028 (empty session/envelope layer, symmetric)
            # then 028->031 (empty device layer, symmetric) then
            # 031->027 (empty idempotency ledger, symmetric) then
            # 027->026 (empty catalog, symmetric) then 026->025,
            # which the guard refuses. Alembic runs multi-step downgrades in
            # one transactional-DDL transaction, so the whole attempt rolls
            # back and the database stays atomically at head (025-guard
            # precedent in this file).
            command.downgrade(_alembic_config(sqlalchemy_dsn), "025_postgres_runtime_compatibility")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == HEAD_REVISION

        # Remove the customer order (test data only — confirmed production rows
        # are never deleted, which is exactly why the guard exists) and the
        # downgrade becomes possible again. Seventeen steps
        # (20260912T1400->20260912T1353->089->083->086->081->038->037->036->035->034->
        #  033->032->029->028->031->027->026->025)
        # restore the 022 constraint set the final assertion exercises.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM recharge_orders WHERE provider != 'zpay'")
        command.downgrade(_alembic_config(sqlalchemy_dsn), "025_postgres_runtime_compatibility")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "025_postgres_runtime_compatibility"
        # Back on 022 constraints, a customer-scope order is rejected again.
        with psycopg.connect(dsn, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_t08_order(conn, 2, pricing_scope="CUSTOMER_STANDARD")
    finally:
        _drop_database(db_name)


def test_pg_low_review_constraint_guards() -> None:
    """M1/M2 review LOW: channel / paid_at couplings, timezone-safe session
    expiry ordering, and TRUNCATE-refusing guards on the three append-only
    audit tables — all enforced by PostgreSQL, not application code."""

    db_name = "t_low_review_guards"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            # channel is the legacy internal payment rail; a customer order
            # (activation_code) carrying one is a constraint-matrix hole (M1 LOW).
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_t08_order(
                    conn,
                    20,
                    provider="activation_code",
                    pricing_scope="CUSTOMER_STANDARD",
                    status="PAID",
                    charged_unit_price_fen_snapshot=1500,
                    amount_fen=1500,
                    credits=1,
                    paid_at="2026-08-22T00:00:00+00:00",
                    channel="alipay",
                )
            # A PAID order without a payment timestamp is not a complete fact.
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_t08_order(
                    conn,
                    21,
                    provider="activation_code",
                    pricing_scope="CUSTOMER_STANDARD",
                    status="PAID",
                    charged_unit_price_fen_snapshot=1500,
                    amount_fen=1500,
                    credits=1,
                )
            # The internal rail keeps its channel (baseline row already proves it).

            # Mixed timezone offsets defeat the textual expires_at > created_at
            # comparison: 03:00Z is 11:00+08, strictly after 10:00+08, but
            # sorts before it as text. The CHECK must compare as timestamptz.
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, created_at, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('sess-mixed-tz', 'u-t08', 'd1', 'c1', "
                "'2026-08-23T10:00:00+08:00', '2026-08-23T10:00:00+08:00', "
                "'2026-08-23T03:00:00Z', 'ip', 'ua')"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, created_at, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('sess-expired', 'u-t08', 'd2', 'c2', "
                    "'2026-08-23T10:00:00+08:00', '2026-08-23T10:00:00+08:00', "
                    "'2026-08-23T02:00:00Z', 'ip', 'ua')"
                )

            # Statement-level TRUNCATE must not be a silent mass-delete around
            # the row-level append-only triggers (M2 LOW).
            for table in (
                "activation_code_events",
                "customer_session_events",
                "security_auth_failures",
            ):
                with pytest.raises(psycopg.errors.RaiseException):
                    conn.execute(f"TRUNCATE {table}")
    finally:
        _drop_database(db_name)


def test_t37_observability_indexes_and_fencing_audit_dimension() -> None:
    """T37's probe has index-backed, append-only rejection and committed-write facts."""
    from alembic import command

    db_name = "t37_observability_schema"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
                ).fetchall()
            }
            assert {
                "idx_customer_session_events_event_occurred_at",
                "idx_customer_session_events_user_event_occurred_at_id",
                "idx_recharge_orders_status_paid_at",
                "idx_wallet_transactions_user_created_at",
                "idx_wallets_updated_at_user",
                "idx_audit_logs_action_occurred_at",
                "idx_generation_tasks_created_at_utc_status_batch",
                "idx_customer_fencing_write_mismatch",
            } <= indexes

            for table_name, expected_column in (
                ("customer_session_events", "occurred_at"),
                ("audit_logs", "occurred_at"),
                ("generation_tasks", "created_at_utc"),
            ):
                columns = {
                    row[0]
                    for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = %s",
                        (table_name,),
                    ).fetchall()
                }
                assert expected_column in columns

            conn.execute("SET enable_seqscan TO off")
            event_plan = "\n".join(
                row[0]
                for row in conn.execute(
                    "EXPLAIN (COSTS OFF) SELECT user_id FROM customer_session_events "
                    "WHERE event = 'HEARTBEAT' "
                    "AND occurred_at >= clock_timestamp() - interval '2 minutes'"
                ).fetchall()
            )
            successor_plan = "\n".join(
                row[0]
                for row in conn.execute(
                    "EXPLAIN (COSTS OFF) SELECT session_epoch FROM customer_session_events "
                    "WHERE user_id = 'missing' AND event = 'LOGIN' "
                    "AND occurred_at <= clock_timestamp() "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 1"
                ).fetchall()
            )
            audit_plan = "\n".join(
                row[0]
                for row in conn.execute(
                    "EXPLAIN (COSTS OFF) SELECT id FROM audit_logs "
                    "WHERE action = 'security.project_denied' "
                    "AND occurred_at >= clock_timestamp() - interval '5 minutes'"
                ).fetchall()
            )
            task_plan = "\n".join(
                row[0]
                for row in conn.execute(
                    "EXPLAIN (COSTS OFF) SELECT id FROM generation_tasks "
                    "WHERE created_at_utc >= clock_timestamp() - interval '10 minutes' "
                    "AND created_at_utc < clock_timestamp() - interval '5 minutes' "
                    "ORDER BY created_at_utc LIMIT 1"
                ).fetchall()
            )
            assert "idx_customer_session_events_event_occurred_at" in event_plan
            assert "idx_customer_session_events_user_event_occurred_at_id" in successor_plan
            assert "idx_audit_logs_action_occurred_at" in audit_plan
            assert "idx_generation_tasks_created_at_utc_status_batch" in task_plan

            evidence_columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'customer_fencing_write_evidence'"
                ).fetchall()
            }
            assert {
                "id",
                "request_id",
                "subject_digest",
                "expected_session_epoch",
                "verified_session_epoch",
                "committed_at",
            } <= evidence_columns

            alert_state_columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'ops_alert_state'"
                ).fetchall()
            }
            assert {"alert_name", "active", "updated_at"} <= alert_state_columns
            conn.execute(
                "INSERT INTO ops_alert_state (alert_name, active) VALUES ('double_online', false)"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO ops_alert_state (alert_name, active) "
                    "VALUES ('unbounded_dynamic_name', true)"
                )

            authorization_columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'customer_authorization_evidence'"
                ).fetchall()
            }
            assert {
                "id",
                "request_id",
                "resource_type",
                "actor_digest",
                "owner_digest",
                "observed_at",
            } <= authorization_columns
            conn.execute(
                "INSERT INTO customer_authorization_evidence "
                "(id, request_id, resource_type, actor_digest, owner_digest) "
                "VALUES ('authz-evidence-t37', 'authz-request-t37', 'asset', %s, %s)",
                ("a" * 64, "b" * 64),
            )
            for mutation in (
                "UPDATE customer_authorization_evidence SET owner_digest = actor_digest",
                "DELETE FROM customer_authorization_evidence",
                "TRUNCATE customer_authorization_evidence",
            ):
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    conn.execute(mutation)

            conn.execute(
                "INSERT INTO customer_fencing_write_evidence "
                "(id, request_id, subject_digest, expected_session_epoch, "
                "verified_session_epoch) VALUES "
                "('t37-write-1', 'request-write-t37', %s, 1, 2)",
                ("a" * 64,),
            )
            for mutation in (
                "UPDATE customer_fencing_write_evidence SET verified_session_epoch = 1",
                "DELETE FROM customer_fencing_write_evidence",
                "TRUNCATE customer_fencing_write_evidence",
            ):
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    conn.execute(mutation)

            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('t37-fence-1', 'session:fencing', 'sha256-digest', "
                "'request-t37', '2026-08-26 12:00:00+00')"
            )
            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('t37-admin-1', 'admin:exchange:ip', 'sha256-ip-digest', "
                "'request-t37-admin', '2026-08-26 12:00:00+00')"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO security_auth_failures "
                    "(id, dimension, identifier, occurred_at) "
                    "VALUES ('t37-invalid', 'unknown:dimension', 'digest', "
                    "'2026-08-26 12:00:00+00')"
                )

        sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
        with pytest.raises(RuntimeError, match="cannot downgrade 042"):
            command.downgrade(_alembic_config(sqlalchemy_dsn), "041_user_fair_queue")

        # Transactional DDL leaves the database at head with all observability
        # indexes intact when the append-only evidence guard refuses rollback.
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION
            index_count = conn.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname = 'idx_wallets_updated_at_user'"
            ).fetchone()[0]
            assert int(index_count) == 1
    finally:
        _drop_database(db_name)


def test_t44_backfills_identity_owners_and_makes_owner_required() -> None:
    """T44: deployed NULL owners are deterministically repaired before isolation."""
    from alembic import command

    db_name = "t44_identity_owner_backfill"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "050_activation_license_zero_credit")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES "
                "('owner-t44', 'owner-t44', 'Owner', 'employee'), "
                "('fallback-t44', 'fallback-t44', 'Fallback', 'customer'), "
                "('admin-t44', 'admin-t44', 'Admin', 'admin')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) "
                "VALUES ('project-t44', 'owner-t44', 'T44 Project')"
            )
            conn.execute(
                "INSERT INTO assets "
                "(id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
                "created_by_user_id) VALUES "
                "('source-t44', 'project-t44', 'image', 'local://source-t44.png', "
                "'sha-source-t44', 1, 'image/png', 'admin-t44')"
            )
            conn.execute(
                "INSERT INTO person_identities "
                "(id, owner_user_id, display_name, authorization_status, authorization_scope, "
                "source_asset_id, source_quality_status, status, created_by) VALUES "
                "('identity-project-t44', NULL, 'Project Identity', 'AUTHORIZED', '[]', "
                "'source-t44', 'PASSED', 'ACTIVE', 'admin-t44'), "
                "('identity-fallback-t44', NULL, 'Fallback Identity', 'AUTHORIZED', '[]', "
                "NULL, 'PASSED', 'ACTIVE', 'fallback-t44')"
            )

        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn) as conn:
            owners = dict(
                conn.execute(
                    "SELECT id, owner_user_id FROM person_identities "
                    "WHERE id LIKE 'identity-%-t44' ORDER BY id"
                ).fetchall()
            )
            nullable = conn.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'person_identities' "
                "AND column_name = 'owner_user_id'"
            ).fetchone()
        assert owners == {
            "identity-fallback-t44": "fallback-t44",
            "identity-project-t44": "owner-t44",
        }
        assert nullable == ("NO",)

        command.downgrade(_alembic_config(sqlalchemy_dsn), "050_activation_license_zero_credit")
        with psycopg.connect(dsn) as conn:
            nullable_after_downgrade = conn.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'person_identities' "
                "AND column_name = 'owner_user_id'"
            ).fetchone()
        assert nullable_after_downgrade == ("YES",)
    finally:
        _drop_database(db_name)


def test_t44_refuses_ambiguous_identity_project_owners() -> None:
    """T44: conflicting tenant evidence must block deployment, not guess."""
    from alembic import command

    db_name = "t44_identity_owner_conflict"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "050_activation_license_zero_credit")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES "
                "('owner-a-t44', 'owner-a-t44', 'Owner A', 'employee'), "
                "('owner-b-t44', 'owner-b-t44', 'Owner B', 'employee')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES "
                "('project-a-t44', 'owner-a-t44', 'Project A'), "
                "('project-b-t44', 'owner-b-t44', 'Project B')"
            )
            conn.execute(
                "INSERT INTO assets "
                "(id, project_id, kind, storage_uri, sha256, size_bytes, content_type) VALUES "
                "('source-a-t44', 'project-a-t44', 'image', 'local://source-a-t44.png', "
                "'sha-source-a-t44', 1, 'image/png'), "
                "('authorization-b-t44', 'project-b-t44', 'authorization', "
                "'local://authorization-b-t44.pdf', 'sha-authorization-b-t44', 1, "
                "'application/pdf')"
            )
            conn.execute(
                "INSERT INTO person_identities "
                "(id, owner_user_id, display_name, authorization_status, authorization_scope, "
                "authorization_asset_id, source_asset_id, source_quality_status, status) VALUES "
                "('identity-conflict-t44', NULL, 'Conflict', 'AUTHORIZED', '[]', "
                "'authorization-b-t44', 'source-a-t44', 'PASSED', 'ACTIVE')"
            )

        with pytest.raises(RuntimeError, match="multiple owners"):
            command.upgrade(_alembic_config(sqlalchemy_dsn), "head")

        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            owner = conn.execute(
                "SELECT owner_user_id FROM person_identities WHERE id = 'identity-conflict-t44'"
            ).fetchone()
        assert version == "050_activation_license_zero_credit"
        assert owner == (None,)
    finally:
        _drop_database(db_name)


def test_t46_scene_task_constraint_and_downgrade_guard() -> None:
    """Scene task history must remain valid across deployment and rollback."""
    from alembic import command

    db_name = "t46_scene_task_constraint"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('owner-t46', 'owner-t46', 'Owner', 'employee')"
            )
            conn.execute(
                "INSERT INTO character_sheet_tasks "
                "(id, created_by_user_id, idempotency_key, request_hash, request_json, "
                "operation, source_storage_uri, source_content_type, source_sha256, "
                "source_size_bytes) VALUES "
                "('scene-task-t46', 'owner-t46', 'scene:t46', 'scene-hash-t46', '{}', "
                "'SCENE', 'local://scene-source-t46.png', 'image/png', 'sha-t46', 1)"
            )

        with pytest.raises(RuntimeError, match="cannot downgrade scene-look"):
            command.downgrade(
                _alembic_config(sqlalchemy_dsn),
                "051_identity_owner_backfill",
            )

        with psycopg.connect(dsn, autocommit=True) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == HEAD_REVISION
            conn.execute("DELETE FROM character_sheet_tasks WHERE id = 'scene-task-t46'")

        command.downgrade(
            _alembic_config(sqlalchemy_dsn),
            "051_identity_owner_backfill",
        )
        with psycopg.connect(dsn) as conn:
            constraint = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_character_sheet_tasks_operation'"
            ).fetchone()[0]
            assert "'SCENE'" not in constraint
    finally:
        _drop_database(db_name)


def test_oral_submit_claim_shares_postgres_user_and_global_generation_limits() -> None:
    from alembic import command

    from app.db_portable import BusinessConnection
    from app.generation import acquire_generation_task_lease
    from app.oral_worker import claim_oral_work

    db_name = "oral_shared_queue_limits"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES "
                "('oral-owner', 'oral-owner', 'Oral Owner', 'employee'), "
                "('other-owner', 'other-owner', 'Other Owner', 'employee')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES "
                "('generation-project', 'other-owner', 'Generation')"
            )
            conn.execute(
                "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
                "VALUES ('oral-identity', 'oral-owner', 'Oral', 'ACTIVE')"
            )
            conn.execute(
                "INSERT INTO oral_avatars "
                "(id, identity_id, owner_user_id, title, status, source_kind, source_asset_id) "
                "VALUES ('oral-avatar', 'oral-identity', 'oral-owner', 'Avatar', "
                "'READY', 'VIDEO', 'source')"
            )
            conn.execute(
                "INSERT INTO oral_tasks "
                "(id, owner_user_id, identity_id, avatar_id, mode, title, status, "
                "estimated_cost_fen, idempotency_key, billing_round) VALUES "
                "('oral-one', 'oral-owner', 'oral-identity', 'oral-avatar', 'AUDIO', "
                "'One', 'QUEUED', 1000, 'oral-one-key', 1), "
                "('oral-two', 'oral-owner', 'oral-identity', 'oral-avatar', 'AUDIO', "
                "'Two', 'QUEUED', 1000, 'oral-two-key', 1)"
            )
            conn.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) "
                "VALUES ('oral-owner', now(), 0), ('other-owner', now(), 0)"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
                "request_snapshot_json) VALUES "
                "('generation-batch', 'generation-project', 'oral-owner', 'batch-key', "
                "'batch-hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model, status) "
                "VALUES ('generation-running', 'generation-batch', 'metaso', 'h3', 'PENDING')"
            )
            conn.execute("UPDATE runtime_settings SET max_concurrent_h3_tasks = 2 WHERE id = 1")

        first_raw = psycopg.connect(dsn)
        second_raw = psycopg.connect(dsn)
        try:
            first = claim_oral_work(
                BusinessConnection.postgres(first_raw),
                worker_id="oral-pg-a",
            )
            first_raw.commit()
            second = claim_oral_work(
                BusinessConnection.postgres(second_raw),
                worker_id="oral-pg-b",
            )
            second_raw.commit()
        finally:
            first_raw.close()
            second_raw.close()
        assert first is not None and first.record_id == "oral-one"
        assert second is None

        with psycopg.connect(dsn) as raw:
            generation_blocked = acquire_generation_task_lease(
                BusinessConnection.postgres(raw),
                worker_id="generation-pg-global-limit",
            )
            raw.commit()
        assert generation_blocked is None

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status = 'FAILED', queue_slot_acquired = 0 "
                "WHERE owner_user_id = 'oral-owner'"
            )
            conn.execute(
                "UPDATE user_queue_cursors SET running_tasks_count = 0 WHERE user_id = 'oral-owner'"
            )
            conn.execute(
                "UPDATE generation_tasks SET status = 'RUNNING' WHERE id = 'generation-running'"
            )
            conn.execute(
                "UPDATE generation_batches SET created_by_user_id = 'other-owner' "
                "WHERE id = 'generation-batch'"
            )
            conn.execute(
                "UPDATE oral_tasks SET status = 'QUEUED', submission_state = 'LOCAL_PENDING' "
                "WHERE id = 'oral-two'"
            )
            conn.execute("UPDATE runtime_settings SET max_concurrent_h3_tasks = 1 WHERE id = 1")
        with psycopg.connect(dsn) as raw:
            blocked = claim_oral_work(
                BusinessConnection.postgres(raw),
                worker_id="oral-pg-global-limit",
            )
            raw.commit()
        assert blocked is None
    finally:
        _drop_database(db_name)


def test_generation_capacity_claim_is_atomic_across_oral_and_generation_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command

    import app.generation as generation
    import app.oral_worker as oral_worker
    from app.db_portable import BusinessConnection
    from app.internal_billing import release_oral_queue_slot
    from app.oral_worker import OralLeaseLostError, OralWorkLease, OralWorkResult

    db_name = "oral_atomic_shared_capacity"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    def claim_oral(worker_id: str) -> str | None:
        with psycopg.connect(dsn) as raw:
            if claim_start_barrier is not None:
                claim_start_barrier.wait(timeout=10)
            lease = oral_worker.claim_oral_work(
                BusinessConnection.postgres(raw), worker_id=worker_id
            )
            return None if lease is None else lease.record_id

    def claim_generation(worker_id: str) -> str | None:
        with psycopg.connect(dsn) as raw:
            if claim_start_barrier is not None:
                claim_start_barrier.wait(timeout=10)
            task = generation.acquire_generation_task_lease(
                BusinessConnection.postgres(raw), worker_id=worker_id
            )
            return None if task is None else str(task["id"])

    claim_start_barrier: threading.Barrier | None = None
    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES "
                "('capacity-u1','capacity-u1','U1','employee'),"
                "('capacity-u2','capacity-u2','U2','employee')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES "
                "('capacity-p1','capacity-u1','P1'),('capacity-p2','capacity-u2','P2')"
            )
            conn.execute(
                "INSERT INTO person_identities (id, owner_user_id, display_name, status) VALUES "
                "('capacity-i1','capacity-u1','I1','ACTIVE'),"
                "('capacity-i2','capacity-u2','I2','ACTIVE')"
            )
            conn.execute(
                "INSERT INTO oral_avatars "
                "(id, identity_id, owner_user_id, title, status, source_kind, source_asset_id) "
                "VALUES ('capacity-a1','capacity-i1','capacity-u1','A1','READY','VIDEO','s1'),"
                "('capacity-a2','capacity-i2','capacity-u2','A2','READY','VIDEO','s2')"
            )
            conn.execute(
                "INSERT INTO oral_tasks "
                "(id, owner_user_id, identity_id, avatar_id, mode, title, status, "
                "estimated_cost_fen, idempotency_key) VALUES "
                "('capacity-o1','capacity-u1','capacity-i1','capacity-a1','AUDIO','O1',"
                "'QUEUED',1000,'capacity-o1-key'),"
                "('capacity-o2','capacity-u2','capacity-i2','capacity-a2','AUDIO','O2',"
                "'QUEUED',1000,'capacity-o2-key')"
            )
            conn.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id,last_dispatched_at,running_tasks_count) VALUES "
                "('capacity-u1',now(),0),('capacity-u2',now(),0)"
            )
            conn.execute(
                "INSERT INTO wallets (user_id,available_credits,reserved_credits) VALUES "
                "('capacity-u1',19,1),('capacity-u2',20,0)"
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id,user_id,type,available_delta,reserved_delta,oral_task_id,billing_round,"
                "idempotency_key) VALUES "
                "('capacity-reserve','capacity-u1','RESERVE',-1,1,'capacity-o1',1,"
                "'capacity-reserve-key')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id,project_id,created_by_user_id,idempotency_key,request_hash,"
                "request_snapshot_json) VALUES "
                "('capacity-b1','capacity-p2','capacity-u2','capacity-b-key',"
                "'capacity-b-hash',"
                '\'{"output_duration_seconds":4,"resolution":"480P","ratio":"adaptive"}\')'
            )
            # 完整快照：赛跑中无论口播还是生成赢下唯一容量槽，两个 claim
            # 路径都必须能走完（load_worker_task 解析快照），否则测试随机
            # 在生成侧赢锁的分支上以 JSONDecodeError 收场。
            conn.execute(
                "INSERT INTO generation_tasks "
                "(id,batch_id,provider,model,status,prompt_snapshot_json) VALUES "
                "('capacity-g1','capacity-b1','metaso','h3','PENDING',"
                '\'{"prompt_text":"capacity","first_frame_uri":null,'
                '"generation_mode":"I2V"}\')'
            )
            conn.execute("UPDATE runtime_settings SET max_concurrent_h3_tasks=1 WHERE id=1")

        original_oral_limits = oral_worker.read_runtime_limits
        oral_barrier = threading.Barrier(2)

        def synchronized_oral_limits(conn: BusinessConnection) -> dict[str, int]:
            limits = original_oral_limits(conn)
            oral_barrier.wait(timeout=10)
            return limits

        monkeypatch.setattr(oral_worker, "read_runtime_limits", synchronized_oral_limits)
        with ThreadPoolExecutor(max_workers=2) as executor:
            oral_results = list(executor.map(claim_oral, ("oral-a", "oral-b")))
        assert sum(result is not None for result in oral_results) == 1

        monkeypatch.setattr(oral_worker, "read_runtime_limits", original_oral_limits)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status='FAILED', queue_slot_acquired=0, "
                "lease_owner=NULL, lease_expires_at=NULL"
            )
            conn.execute(
                "UPDATE oral_tasks SET status='QUEUED', submission_state='LOCAL_PENDING' "
                "WHERE id='capacity-o1'"
            )
            conn.execute("UPDATE user_queue_cursors SET running_tasks_count=0")

        # Synchronize before either worker acquires the capacity row. Waiting
        # inside read_runtime_limits deadlocks when generation holds that row
        # while oral is still trying to enter its quarantine transaction.
        claim_start_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            oral_future = executor.submit(claim_oral, "mixed-oral")
            generation_future = executor.submit(claim_generation, "mixed-generation")
            mixed_results = [oral_future.result(), generation_future.result()]
        assert sum(result is not None for result in mixed_results) == 1
        claim_start_barrier = None
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status='FAILED', queue_slot_acquired=0, "
                "lease_owner=NULL, lease_expires_at=NULL"
            )
            conn.execute(
                "UPDATE oral_tasks SET status='ARCHIVE_FAILED', "
                "provider_result_url='https://provider.invalid/result.mp4' "
                "WHERE id='capacity-o2'"
            )
            conn.execute(
                "UPDATE generation_tasks SET status='RUNNING', locked_by=NULL, locked_until=NULL "
                "WHERE id='capacity-g1'"
            )
            conn.execute("UPDATE user_queue_cursors SET running_tasks_count=0")
        with psycopg.connect(dsn) as raw:
            conn = BusinessConnection.postgres(raw)
            with pytest.raises(ValueError):
                oral_worker.request_oral_archive_retry(
                    conn,
                    task_id="capacity-o2",
                    owner_user_id="capacity-u2",
                )
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE generation_tasks SET status='FAILED' WHERE id='capacity-g1'")
        with psycopg.connect(dsn) as raw:
            conn = BusinessConnection.postgres(raw)
            retried = oral_worker.request_oral_archive_retry(
                conn,
                task_id="capacity-o2",
                owner_user_id="capacity-u2",
            )
            assert retried["status"] == "ARCHIVING"
            assert retried["queue_slot_acquired"] == 1
            release_oral_queue_slot(conn, oral_task_id="capacity-o2")
            release_oral_queue_slot(conn, oral_task_id="capacity-o2")
            raw.commit()
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id='capacity-u2'"
                ).fetchone()[0]
                == 0
            )

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status='SUBMITTING', "
                "submission_state='SUBMITTING', provider_charge_state='NOT_SUBMITTED', "
                "queue_slot_acquired=1, lease_owner='uncertain-token', "
                "lease_expires_at=now() + interval '1 minute', attempt_count=7 "
                "WHERE id='capacity-o1'"
            )
            conn.execute(
                "UPDATE user_queue_cursors SET running_tasks_count=1 WHERE user_id='capacity-u1'"
            )
        uncertain_lease = OralWorkLease(
            kind="task_submit",
            record_id="capacity-o1",
            worker_id="uncertain-worker",
            lease_token="uncertain-token",
            attempt_count=7,
            row={},
        )
        with psycopg.connect(dsn) as raw:
            oral_worker.finalize_oral_work(
                BusinessConnection.postgres(raw),
                lease=uncertain_lease,
                result=OralWorkResult(outcome="uncertain", message="provider response unknown"),
            )
            raw.commit()
        with psycopg.connect(dsn) as conn:
            task_row = conn.execute(
                "SELECT status,submission_state,provider_charge_state,queue_slot_acquired "
                "FROM oral_tasks WHERE id='capacity-o1'"
            ).fetchone()
            cursor_count = conn.execute(
                "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id='capacity-u1'"
            ).fetchone()[0]
            wallet = conn.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id='capacity-u1'"
            ).fetchone()
            transaction_types = conn.execute(
                "SELECT type FROM wallet_transactions WHERE oral_task_id='capacity-o1' "
                "ORDER BY created_at,id"
            ).fetchall()
            assert task_row == ("SUBMISSION_UNCERTAIN", "SUBMISSION_UNKNOWN", "UNKNOWN", 0)
            assert cursor_count == 0
            assert wallet == (19, 1)
            assert transaction_types == [("RESERVE",)]

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status='SUBMITTING', "
                "submission_state='SUBMITTING', provider_charge_state='NOT_SUBMITTED', "
                "queue_slot_acquired=1, lease_owner='winner-token', "
                "lease_expires_at=now() + interval '1 minute', attempt_count=8 "
                "WHERE id='capacity-o1'"
            )
            conn.execute(
                "UPDATE user_queue_cursors SET running_tasks_count=1 WHERE user_id='capacity-u1'"
            )
        stale_lease = OralWorkLease(
            kind="task_submit",
            record_id="capacity-o1",
            worker_id="stale-worker",
            lease_token="stale-token",
            attempt_count=8,
            row={},
        )
        with psycopg.connect(dsn) as raw:
            with pytest.raises(OralLeaseLostError):
                oral_worker.finalize_oral_work(
                    BusinessConnection.postgres(raw),
                    lease=stale_lease,
                    result=OralWorkResult(outcome="uncertain", message="stale response"),
                )
            raw.commit()
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT status,lease_owner,queue_slot_acquired FROM oral_tasks "
                "WHERE id='capacity-o1'"
            ).fetchone() == ("SUBMITTING", "winner-token", 1)
            assert (
                conn.execute(
                    "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id='capacity-u1'"
                ).fetchone()[0]
                == 1
            )
            assert conn.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id='capacity-u1'"
            ).fetchone() == (19, 1)
            assert conn.execute(
                "SELECT type FROM wallet_transactions WHERE oral_task_id='capacity-o1' "
                "ORDER BY created_at,id"
            ).fetchall() == [("RESERVE",)]
    finally:
        _drop_database(db_name)
