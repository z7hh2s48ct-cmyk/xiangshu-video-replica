"""
T05 / DB-03 - PostgreSQL runtime entry tests.

Covers the DSN resolution layer, the customer-production fail-closed matrix,
the psycopg3 connection pool, transaction semantics (including the
SERIALIZABLE replacement for SQLite BEGIN IMMEDIATE), and PG server time.
PG-dependent cases skip automatically when the fixture is not reachable.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import psycopg
import pytest
from fastapi import HTTPException, Request
from pg_test_kit import require_pg_or_explicit_skip
from psycopg_pool import ConnectionPool
from psycopg_pool.errors import PoolTimeout

from app.db_pg import (
    DATABASE_URL_ENV,
    DEFAULT_POOL_TIMEOUT,
    PG_IDLE_IN_TRANSACTION_TIMEOUT_MS,
    PG_STATEMENT_TIMEOUT_MS,
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    pg_server_now,
    pg_transaction,
    redact_postgres_dsn,
    resolve_database_config,
    validate_customer_production,
)
from app.db_portable import BusinessConnection

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


PG_DSN = os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Temporarily set/unset environment variables."""
    saved: dict[str, str | None] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# DSN resolution (pure, no PG required)
# ---------------------------------------------------------------------------


def test_resolve_pg_url_selects_postgres_mode() -> None:
    with _env(**{DATABASE_URL_ENV: "postgresql://u:p@host:5432/db"}):
        config = resolve_database_config()
    assert config.mode is DatabaseMode.POSTGRESQL
    assert config.dsn == "postgresql://u:p@host:5432/db"


def test_resolve_postgres_scheme_alias() -> None:
    with _env(**{DATABASE_URL_ENV: "postgres://u:p@host:5432/db"}):
        config = resolve_database_config()
    assert config.mode is DatabaseMode.POSTGRESQL


def test_resolve_rejects_db_path_all_environments() -> None:
    """CW-025: VIDEO_REPLICA_DB_PATH 在线入口全环境拒绝。

    历史 SQLite 工具（backup/sqlite_to_postgres/gate1_*）走 CW-060 独立白名单，
    不经过 resolve_database_config() 的在线通道。
    """
    with _env(**{DATABASE_URL_ENV: "", "VIDEO_REPLICA_DB_PATH": "/tmp/app.db"}):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            resolve_database_config()


# CW-025: customer_fence SQLite 通道测试已删除。
# PG 回归版本见 test_business_write_without_snapshot_never_falls_back_from_pg、
# test_customer_snapshot_pg_pool_failure_is_fail_closed、
# test_business_read_pg_failure_does_not_fall_back_to_sqlite。
# customer_fence.py 的 internal lane（DB_PATH 通道）保留，归 CW-030/CW-040 后续处理；
# customer production 环境下 SQLite URL 被拒绝的逻辑由入口（bootstrap/main/worker）
# 的 resolve_database_config() 全环境 fail-closed 保证，不由 customer_fence 重复检查。


def test_business_write_without_snapshot_never_falls_back_from_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CW-042-b: the SQLite fallback lane is gone — the fence refuses a
    snapshot-less write with 503 regardless of any leftover DB_PATH."""
    from app import customer_fence

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "legacy.db"))

    with pytest.raises(HTTPException) as error:
        with customer_fence.BusinessDb(None, None, None).write():
            pytest.fail("A PG writer requires a customer session snapshot")
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "DATABASE_NOT_CONFIGURED"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_customer_snapshot_pg_pool_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    from app import customer_fence

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setattr(customer_fence, "get_pg_pool", Mock(side_effect=error_type("PG failed")))
    request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as error:
        customer_fence.get_business_db(request)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "SESSION_SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_business_read_pg_failure_does_not_fall_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[Exception]
) -> None:
    """CW-042-b: the SQLite read lane is gone — a PG pool failure propagates
    instead of falling back, no matter what DB_PATH says."""
    from app import customer_fence, db_pg

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "internal.db"))
    unavailable_pool = Mock(side_effect=error_type("PG failed"))
    monkeypatch.setattr(customer_fence, "get_pg_pool", unavailable_pool)
    monkeypatch.setattr(db_pg, "get_pg_pool", unavailable_pool)

    with pytest.raises(error_type, match="PG failed"):
        next(customer_fence.get_business_read_conn())


def test_resolve_rejects_unsupported_scheme() -> None:
    with _env(**{DATABASE_URL_ENV: "mysql://u:p@host/db"}):
        with pytest.raises(ValueError, match="unsupported database URL scheme"):
            resolve_database_config()


def test_resolve_missing_dsn_raises_runtime_error_all_environments() -> None:
    """CW-025: 缺 DSN 全环境 fail-closed（不再区分 internal/customer lane）。

    历史内部 P0 单机版走 CW-060 独立白名单工具，不经过在线入口。
    MissingDatabaseConfigError 类保留（避免破坏 import），但 resolve_database_config()
    不再抛它——所有缺配置场景统一抛 RuntimeError。
    """
    with _env(**{DATABASE_URL_ENV: "", "VIDEO_REPLICA_DB_PATH": ""}):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            resolve_database_config()


# ---------------------------------------------------------------------------
# Customer-production fail-closed matrix (pure, no PG required)
# ---------------------------------------------------------------------------


def test_production_requires_database_url() -> None:
    with _env(**{"VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true", DATABASE_URL_ENV: ""}):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            validate_customer_production(resolve_database_config())


def test_production_rejects_sqlite() -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: "sqlite:////data/app.db",
        }
    ):
        with pytest.raises(RuntimeError, match="SQLite"):
            validate_customer_production(resolve_database_config())


def test_production_rejects_leftover_db_path() -> None:
    """Customer production + PG URL + legacy DB_PATH is ambiguous and must
    fail closed instead of silently preferring the URL (PR #32 review P1)."""
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: "postgresql://u:p@host:5432/db?sslmode=verify-full",
            "VIDEO_REPLICA_DB_PATH": "/leftover/app.db",
        }
    ):
        with pytest.raises(RuntimeError, match="VIDEO_REPLICA_DB_PATH"):
            validate_customer_production(resolve_database_config())


@pytest.mark.parametrize("sslmode", ["", "disable", "allow", "prefer"])
def test_production_rejects_postgres_without_enforced_tls(sslmode: str) -> None:
    dsn = "postgresql://u:secret@host:5432/db"
    if sslmode:
        dsn = f"{dsn}?sslmode={sslmode}"

    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: dsn,
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        with pytest.raises(RuntimeError, match="must enforce TLS") as exc_info:
            validate_customer_production(resolve_database_config())

    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_postgres_tls_enforcing_modes(sslmode: str) -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: f"postgresql://u:p@host:5432/db?sslmode={sslmode}",
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        validate_customer_production(resolve_database_config())


def test_production_rejects_ambiguous_postgres_sslmode() -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: ("postgresql://u:p@host:5432/db?sslmode=require&sslmode=disable"),
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        with pytest.raises(RuntimeError, match="must enforce TLS"):
            validate_customer_production(resolve_database_config())


def test_non_production_rejects_sqlite_url() -> None:
    """CW-025: sqlite:// URL 全环境拒绝（不再区分 internal/customer lane）。

    历史 SQLite 只读迁移工具走 CW-060 独立白名单，不经过 resolve_database_config()。
    """
    with _env(**{"VIDEO_REPLICA_CUSTOMER_PRODUCTION": "", DATABASE_URL_ENV: "sqlite:////tmp/x.db"}):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            resolve_database_config()


# ---------------------------------------------------------------------------
# CW-025: 五环境启动拒绝矩阵（dev/test/CI/staging/production × 5 场景）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_label",
    ["dev", "test", "ci", "staging", "production"],
    ids=["env-dev", "env-test", "env-ci", "env-staging", "env-production"],
)
@pytest.mark.parametrize(
    "scenario",
    ["missing_dsn", "sqlite_url", "db_path_only", "db_path_with_pg", "unsupported_scheme"],
    ids=["scn-missing", "scn-sqlite-url", "scn-db-path", "scn-mixed", "scn-unsupported"],
)
def test_all_environment_startup_rejection_matrix(
    env_label: str, scenario: str, tmp_path: Path
) -> None:
    """CW-025 增量验收：全部环境对非 PG 配置 fail-closed 且无 SQLite 文件副作用。

    账本要求："新增 dev/test/CI/staging/production 启动拒绝矩阵：有效 PG 成功，
    其余配置全部非零且无 SQLite 文件副作用"。

    环境标识通过 VIDEO_REPLICA_CUSTOMER_PRODUCTION 区分：
    - production: "true"
    - staging/dev/test/ci: "" (非 customer_production，但 CW-025 后同样 fail-closed)

    5 种拒绝场景：
    1. missing_dsn: 缺 DATABASE_URL 和 DB_PATH
    2. sqlite_url: DATABASE_URL=sqlite://...
    3. db_path_only: 只有 DB_PATH
    4. db_path_with_pg: PG URL + DB_PATH 混配（歧义配置）
    5. unsupported_scheme: DATABASE_URL=mysql://... 等不支持的 scheme
    """
    # 环境标识设置
    customer_production = "true" if env_label == "production" else ""

    # 场景配置
    db_path_value = str(tmp_path / "rejected.db")
    if scenario == "missing_dsn":
        database_url = ""
        db_path = ""
    elif scenario == "sqlite_url":
        database_url = f"sqlite:///{db_path_value}"
        db_path = ""
    elif scenario == "db_path_only":
        database_url = ""
        db_path = db_path_value
    elif scenario == "db_path_with_pg":
        database_url = "postgresql://u:p@host:5432/db?sslmode=require"
        db_path = db_path_value
    elif scenario == "unsupported_scheme":
        database_url = "mysql://u:p@host/db"
        db_path = ""
    else:  # pragma: no cover
        raise AssertionError(f"unknown scenario {scenario}")

    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": customer_production,
            DATABASE_URL_ENV: database_url,
            "VIDEO_REPLICA_DB_PATH": db_path,
        }
    ):
        # CW-025: 全环境 fail-closed。unsupported_scheme 保留原有 ValueError
        # （Codex P1 review），其余场景抛 RuntimeError。两者都是非零退出。
        with pytest.raises((RuntimeError, ValueError)):
            resolve_database_config()

    # 断言：不生成任何 SQLite 文件副作用
    assert not (tmp_path / "rejected.db").exists(), f"{env_label}/{scenario} 不得创建 .db 文件"
    assert not (tmp_path / "rejected.db-wal").exists(), f"{env_label}/{scenario} 不得创建 .db-wal"
    assert not (tmp_path / "rejected.db-shm").exists(), f"{env_label}/{scenario} 不得创建 .db-shm"


@pytest.mark.parametrize("env_label", ["dev", "test", "ci", "staging", "production"])
def test_all_environment_accepts_valid_pg_dsn(env_label: str) -> None:
    """CW-025 增量验收：有效 PG DSN 在全部环境成功解析。

    与拒绝矩阵配对的正向对照：证明 fail-closed 不是"全部拒绝"，
    而是"只接受 PG"。
    """
    customer_production = "true" if env_label == "production" else ""
    dsn = "postgresql://u:p@host:5432/db"
    if env_label == "production":
        # customer_production 额外要求 TLS
        dsn = f"{dsn}?sslmode=require"

    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": customer_production,
            DATABASE_URL_ENV: dsn,
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        config = resolve_database_config()
        assert config.mode is DatabaseMode.POSTGRESQL
        assert config.dsn == dsn
        # validate_customer_production 在 production 时额外校验 TLS，非 production 时 no-op
        validate_customer_production(config)


# ---------------------------------------------------------------------------
# Pool / transactions / server time (require the PG fixture)
# ---------------------------------------------------------------------------

pytestmark_pg = pytest.mark.usefixtures("pg_hard_gate")


@pytest.fixture(scope="module")
def pg_hard_gate() -> None:
    """CW-007 hard gate: unreachable PG fails the suite (explicit opt-in may skip)."""
    require_pg_or_explicit_skip()


@pytestmark_pg
def test_check_pg_ready_uses_pool_and_server_time() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        ready = check_pg_ready()
    # The DSN is redacted at the PgReadyInfo boundary (M1 review LOW).
    assert ready.dsn != PG_DSN
    assert "testpass" not in ready.dsn
    assert isinstance(ready.server_now, datetime)
    assert ready.pool_size >= 1


def test_check_pg_ready_rejects_a_read_only_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import db_pg

    statements: list[str] = []

    class ScalarCursor:
        def __init__(self, value: object) -> None:
            self._value = value

        def fetchone(self) -> tuple[object]:
            return (self._value,)

    class ReadOnlyConnection:
        def execute(self, sql: str) -> ScalarCursor:
            statements.append(sql)
            values: dict[str, object] = {
                "SELECT now()": datetime.now(),
                "SELECT 1": 1,
                "SHOW transaction_read_only": "on",
            }
            return ScalarCursor(values[sql])

    class ReadOnlyPool:
        max_size = 4

        @contextmanager
        def connection(self) -> Iterator[ReadOnlyConnection]:
            yield ReadOnlyConnection()

    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://app@pg-ha/customer")
    monkeypatch.setattr(db_pg, "get_pg_pool", lambda: ReadOnlyPool())

    with pytest.raises(RuntimeError, match="read-only"):
        db_pg.check_pg_ready()

    assert "SHOW transaction_read_only" in statements


@pytestmark_pg
def test_pg_transaction_commits() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t05_tx ("
                "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value TEXT)"
            )
            conn.execute("TRUNCATE t05_tx")
            conn.execute("INSERT INTO t05_tx (value) VALUES (%s)", ("committed",))
        with pg_transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM t05_tx WHERE value = %s", ("committed",)
            ).fetchone()[0]
            assert count == 1


@pytestmark_pg
def test_pg_pool_sets_leaked_transaction_guardrails() -> None:
    """2026-09-07 评审 §7-0：泄漏的未提交 claim 事务曾在共享容量行锁上
    挂住并发 worker 19 分钟。池连接必须携带 statement / idle-in-transaction
    超时，把这类故障从无限排队变成数据库侧快速失败。"""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        close_pg_pool()
        try:
            with pg_transaction() as conn:
                # pg_settings.setting 是 GUC 基础单位（ms）的原始值；SHOW /
                # current_setting 会把它单位化成 "5min" 这类显示串。
                rows = conn.execute(
                    "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
                    (["statement_timeout", "idle_in_transaction_session_timeout"],),
                ).fetchall()
        finally:
            close_pg_pool()
    values = {str(row[0]): int(row[1]) for row in rows}
    assert values["statement_timeout"] == PG_STATEMENT_TIMEOUT_MS
    assert values["idle_in_transaction_session_timeout"] == (PG_IDLE_IN_TRANSACTION_TIMEOUT_MS)


@pytestmark_pg
def test_pg_transaction_rolls_back_on_error() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pytest.raises(RuntimeError, match="boom"):
            with pg_transaction() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS t05_tx ("
                    "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value TEXT)"
                )
                conn.execute("INSERT INTO t05_tx (value) VALUES (%s)", ("rolled-back",))
                raise RuntimeError("boom")
        with pg_transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM t05_tx WHERE value = %s", ("rolled-back",)
            ).fetchone()[0]
            assert count == 0


@pytestmark_pg
def test_pg_transaction_serializable_write_conflict() -> None:
    """SERIALIZABLE replaces SQLite BEGIN IMMEDIATE: a transaction whose snapshot
    is invalidated by a committed concurrent write to the same key must fail
    with SerializationFailure instead of silently overwriting."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t05_conflict (id INTEGER PRIMARY KEY, v INTEGER)"
            )
            conn.execute("TRUNCATE t05_conflict")
            conn.execute("INSERT INTO t05_conflict (id, v) VALUES (1, 0)")

        # Writer B opens a SERIALIZABLE transaction and reads the key (snapshot v=0),
        # then keeps the transaction open while writer A commits on another
        # pooled connection (read-then-write on the same key).
        with pytest.raises(psycopg.errors.SerializationFailure):
            with pg_transaction(isolation="SERIALIZABLE") as conn_b:
                conn_b.execute("SELECT v FROM t05_conflict WHERE id = 1").fetchone()
                # Concurrent committer A on a second pooled connection.
                with pg_transaction() as conn_a:
                    conn_a.execute("SELECT v FROM t05_conflict WHERE id = 1").fetchone()
                    conn_a.execute("UPDATE t05_conflict SET v = 1 WHERE id = 1")
                # B writes the same key based on its stale snapshot.
                conn_b.execute("UPDATE t05_conflict SET v = 2 WHERE id = 1")
                # Leaving the block commits B → rejected with SQLSTATE 40001.


@pytestmark_pg
def test_pg_server_now_is_monotonic_and_not_client_clock() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        first = pg_server_now()
        second = pg_server_now()
    assert isinstance(first, datetime)
    assert second >= first


@pytestmark_pg
def test_pool_returns_connections_and_is_observable() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        from app.db_pg import get_pg_pool

        pool = get_pg_pool()
        assert isinstance(pool, ConnectionPool)
        with pool.connection() as conn:
            value = conn.execute("SELECT 1").fetchone()[0]
            assert value == 1
        assert not pool.closed


@pytestmark_pg
def test_pool_applies_hygiene_parameters() -> None:
    """M0 review M1: the pool must check connections and recycle them by
    lifetime/idle bounds instead of handing out possibly-dead sockets."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        from app.db_pg import (
            DEFAULT_POOL_MAX_IDLE,
            DEFAULT_POOL_MAX_LIFETIME,
            DEFAULT_POOL_TIMEOUT,
            get_pg_pool,
        )

        pool = get_pg_pool()
        # NB: ``pool.check`` is a *method* (runs the checks on demand); the
        # configured callback lives on the private ``_check`` attribute.
        assert pool._check == ConnectionPool.check_connection
        assert pool.max_lifetime == DEFAULT_POOL_MAX_LIFETIME
        assert pool.max_idle == DEFAULT_POOL_MAX_IDLE
        assert pool.timeout == DEFAULT_POOL_TIMEOUT


def test_pg_transaction_rejects_unknown_isolation_level() -> None:
    """M0 review M3: the isolation level feeds a SET statement, so anything
    outside the closed allow-list must be rejected before touching the DB."""
    from app.db_pg import _ALLOWED_ISOLATION_LEVELS  # noqa: PLC2701 - assert surface

    with pytest.raises(ValueError, match="unsupported isolation level"):
        with pg_transaction(isolation="SERIALIZABLE; DROP TABLE users"):  # type: ignore[arg-type]
            pass

    assert "SERIALIZABLE" in _ALLOWED_ISOLATION_LEVELS
    assert "READ COMMITTED" in _ALLOWED_ISOLATION_LEVELS
    assert "REPEATABLE READ" in _ALLOWED_ISOLATION_LEVELS


@pytest.fixture(autouse=True)
def _close_pool_between_tests() -> Iterator[None]:
    """Reset the module-level pool so each test binds its own DSN."""
    from app.db_pg import close_pg_pool

    close_pg_pool()
    yield
    close_pg_pool()


# ---------------------------------------------------------------------------
# API/Worker bootstrap integration (require the PG fixture)
# ---------------------------------------------------------------------------


@pytestmark_pg
def test_api_bootstrap_completes_in_pg_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m app.bootstrap` must finish the PG ready check without
    touching any SQLite file (API startup path for the PG lane)."""
    from app import bootstrap as bootstrap_module

    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", PG_DSN)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    bootstrap_module.main([])  # must return cleanly after check_pg_ready()


@pytestmark_pg
def test_worker_main_dispatches_to_pg_forever_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generation worker entry must complete the PG ready check and hand
    over to the T25 fair-queue loop (``run_forever_pg``) — never the SQLite
    task loop.

    M0 review H1 required a non-zero exit while the PG loop was still
    unimplemented ("restart on failure must not mistake the unimplemented PG
    lane for a healthy idle worker"). T25 landed the PG loop, so the worker
    now stays in it; the supervisor's health signal is the loop's liveness,
    not an exit code.

    Asserted by behaviour (not log output, which is vulnerable to global
    logging state left behind by other tests): neither the one-shot loop nor
    the SQLite forever loop may run in PG mode, and the pool stays usable
    once the worker releases it.
    """
    from app import generation_worker as worker_module
    from app.db_pg import get_pg_pool

    calls: list[str] = []
    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", PG_DSN)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["generation_worker"])
    monkeypatch.setattr(
        worker_module, "run_forever_pg", lambda **kwargs: calls.append("run_forever_pg")
    )
    # CW-030 removed the SQLite worker entry points entirely: the SQLite
    # forever loop cannot run in any mode because it no longer exists.
    assert not hasattr(worker_module, "run_forever")
    assert not hasattr(worker_module, "run_sqlite_worker_round")

    worker_module.main()

    assert calls == ["run_forever_pg"], f"PG-mode worker must dispatch to the PG loop, got {calls}"
    # The worker closes its pool after the loop returns; a fresh pool must
    # still be creatable from the same configuration.
    assert not get_pg_pool().closed


# ---------------------------------------------------------------------------
# M0 review H3 — VIDEO_REPLICA_DATABASE_URL drives `alembic upgrade head`
# ---------------------------------------------------------------------------


def _alembic_head() -> str:
    from alembic.script import ScriptDirectory

    server_dir = Path(__file__).resolve().parent.parent
    return ScriptDirectory(str(server_dir / "migrations")).get_current_head()


def _run_alembic_upgrade_head() -> subprocess.CompletedProcess[str]:
    server_dir = Path(__file__).resolve().parent.parent
    # Alembic echoes migration comments that may contain non-ASCII bytes;
    # decode as UTF-8 with replacement instead of the ambient Windows code
    # page, whose reader thread would otherwise die mid-decode and leave
    # stderr as None.
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=server_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_alembic_env_var_targets_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: the env var must override the alembic.ini default, so `alembic
    upgrade head` targets the configured SQLite file instead of data/app.db."""
    db_path = tmp_path / "env-var.db"
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{db_path}")

    result = _run_alembic_upgrade_head()

    assert result.returncode == 0, result.stderr
    assert db_path.exists(), "env var must redirect migrations away from data/app.db"
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _alembic_head()


@pytestmark_pg
def test_alembic_env_var_dsn_runs_migrations_on_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H3: a bare ``postgresql://`` env var must be rewritten to the psycopg3
    driver (bare URLs resolve to psycopg2, which is not installed) and drive
    the real `alembic upgrade head` against a fresh PG database — the exact
    operator path the ini-file default used to block."""
    db_name = "h3_env_url_test"
    admin_dsn = PG_DSN.rsplit("/", 1)[0] + "/postgres"
    dsn = PG_DSN.rsplit("/", 1)[0] + f"/{db_name}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')

    # Deliberately the bare postgresql:// scheme: the rewrite is part of the
    # behaviour under test.
    monkeypatch.setenv(DATABASE_URL_ENV, dsn)
    try:
        result = _run_alembic_upgrade_head()
        assert result.returncode == 0, result.stderr
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _alembic_head()
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_pool_max_is_capped_at_a_hard_ceiling() -> None:
    """A misconfigured POOL_MAX must not drain the server's connections (M1 review LOW)."""
    from app.db_pg import POOL_MAX_ENV, _pool_bounds

    with _env(**{POOL_MAX_ENV: "100000"}):
        pool_min, pool_max = _pool_bounds()
    assert pool_max == 64


def test_pool_min_is_capped_at_the_hard_ceiling() -> None:
    """A POOL_MIN above the ceiling must not yield an invalid (min > max)
    pair: ConnectionPool rejects max_size smaller than min_size, turning
    the connection-budget safeguard into a startup failure (Codex P2)."""
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, _pool_bounds

    with _env(**{POOL_MIN_ENV: "100000", POOL_MAX_ENV: "32"}):
        pool_min, pool_max = _pool_bounds()
    assert (pool_min, pool_max) == (64, 64)


@pytest.mark.usefixtures("pg_hard_gate")
def test_check_pg_ready_redacts_dsn_credentials() -> None:
    """PgReadyInfo.dsn must never carry the password (M1 review LOW)."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        ready = check_pg_ready()
    assert ready.dsn != PG_DSN
    assert "testpass" not in ready.dsn
    assert "customer_v3_test" in ready.dsn


# ---------------------------------------------------------------------------
# CW-055 — 池耗尽有界失败、断连回收、异常事务复用
# ---------------------------------------------------------------------------


def test_pool_timeout_is_configurable_and_capped() -> None:
    """CW-055：借用超时必须是**可配置**的运维旋钮，且带硬上限。

    硬编码 30s 意味着"池耗尽"的最坏排队时间无法按部署形态收紧；而没有上限
    同样危险——一个被误配成数小时的 timeout 会把"有界失败"退化回
    2026-09-07 评审 §7-0 那种无限期挂住（实测曾拖停公平队列 19 分钟）。
    """
    from app.db_pg import POOL_TIMEOUT_CEILING, POOL_TIMEOUT_ENV, _pool_timeout

    with _env(**{POOL_TIMEOUT_ENV: ""}):
        assert _pool_timeout() == DEFAULT_POOL_TIMEOUT
    with _env(**{POOL_TIMEOUT_ENV: "0.5"}):
        assert _pool_timeout() == 0.5
    with _env(**{POOL_TIMEOUT_ENV: "not-a-number"}):
        assert _pool_timeout() == DEFAULT_POOL_TIMEOUT
    # 0 与负值都不是合法的"有界等待"：psycopg_pool 会把它们当成不等待或异常，
    # 因此必须回落到默认值而不是照单全收。
    with _env(**{POOL_TIMEOUT_ENV: "0"}):
        assert _pool_timeout() == DEFAULT_POOL_TIMEOUT
    with _env(**{POOL_TIMEOUT_ENV: "-3"}):
        assert _pool_timeout() == DEFAULT_POOL_TIMEOUT
    with _env(**{POOL_TIMEOUT_ENV: "999999"}):
        assert _pool_timeout() == POOL_TIMEOUT_CEILING


@pytestmark_pg
def test_pool_exhaustion_fails_within_the_configured_timeout() -> None:
    """CW-055：池 max=N 时第 N+1 次借用必须在**配置的** timeout 内失败。"""
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, POOL_TIMEOUT_ENV

    timeout = 0.4
    with _env(
        **{
            DATABASE_URL_ENV: PG_DSN,
            POOL_MIN_ENV: "1",
            POOL_MAX_ENV: "2",
            POOL_TIMEOUT_ENV: str(timeout),
        }
    ):
        with pg_transaction(), pg_transaction():
            started = time.monotonic()
            with pytest.raises(PoolTimeout):
                with pg_transaction():
                    pytest.fail("池已耗尽，第三次借用不得拿到连接")
            elapsed = time.monotonic() - started

    assert elapsed >= timeout, f"必须真的等到配置超时才失败，实际 {elapsed:.3f}s"
    assert elapsed < DEFAULT_POOL_TIMEOUT, (
        f"失败必须受配置 timeout 约束，实际 {elapsed:.3f}s（默认 {DEFAULT_POOL_TIMEOUT}s）"
    )


@pytestmark_pg
def test_pool_exhaustion_logs_a_redacted_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    """CW-055：池耗尽必须留下**脱敏**的诊断日志。

    没有日志时运维只看到一串 PoolTimeout，无法区分"池太小"还是"PG 不可达"；
    而带上原始 DSN 的日志会把口令写进日志盘。这里同时钉住两侧。

    NB：本文件多数断言刻意避开日志（见
    test_worker_main_dispatches_to_pg_forever_loop 的理由：全局 logging 状态
    可能被其他测试污染）。此处按 logger 名过滤记录，只认 app.db_pg 自己发出的
    WARNING，不依赖 root handler 的全局配置。
    """
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, POOL_TIMEOUT_ENV

    password = unquote(urlsplit(PG_DSN).password or "")
    assert password, "本用例要求 DSN 带口令，否则脱敏断言无意义"

    with caplog.at_level(logging.WARNING, logger="app.db_pg"):
        with _env(
            **{
                DATABASE_URL_ENV: PG_DSN,
                POOL_MIN_ENV: "1",
                POOL_MAX_ENV: "1",
                POOL_TIMEOUT_ENV: "0.3",
            }
        ):
            with pg_transaction():
                with pytest.raises(PoolTimeout) as excinfo:
                    with pg_transaction():
                        pass

    messages = [record.getMessage() for record in caplog.records if record.name == "app.db_pg"]
    assert messages, "池耗尽必须记录 WARNING 级诊断日志"
    joined = "\n".join(messages)
    assert password not in joined, "诊断日志不得包含 DSN 口令"
    assert password not in str(excinfo.value), "抛出的异常文本不得包含 DSN 口令"
    assert redact_postgres_dsn(PG_DSN) in joined, "日志应携带脱敏 DSN 以定位是哪个库/主机耗尽"


@pytestmark_pg
def test_pool_recovers_once_connections_are_returned() -> None:
    """CW-055：耗尽是有界失败，不是永久损坏——归还后下一请求必须成功。"""
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, POOL_TIMEOUT_ENV

    with _env(
        **{
            DATABASE_URL_ENV: PG_DSN,
            POOL_MIN_ENV: "1",
            POOL_MAX_ENV: "1",
            POOL_TIMEOUT_ENV: "0.3",
        }
    ):
        with pg_transaction() as held:
            assert held.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(PoolTimeout):
                with pg_transaction():
                    pass
        # 上一个 with 已归还唯一连接：下一次借用必须直接成功。
        with pg_transaction() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytestmark_pg
def test_failed_transaction_does_not_poison_the_returned_connection() -> None:
    """CW-055：异常事务回收——失败连接不得带着 aborted transaction 返池。

    max_size=1 强制下一个借用者拿到**同一条**物理连接：若池没有复位它，
    借用者会立刻撞上 InFailedSqlTransaction（"current transaction is aborted"），
    把一次业务失败放大成整条链路的连锁失败。
    """
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV

    with _env(**{DATABASE_URL_ENV: PG_DSN, POOL_MIN_ENV: "1", POOL_MAX_ENV: "1"}):
        # DDL 必须先在自己已提交的事务里建：否则它会随下面的失败事务
        # 一起回滚（PG 的事务性 DDL），后续断言就变成 UndefinedTable 而非在测池。
        with pg_transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t055_poison (id INTEGER PRIMARY KEY)")
            conn.execute("TRUNCATE t055_poison")
        with pytest.raises(RuntimeError, match="boom"):
            with pg_transaction() as conn:
                conn.execute("INSERT INTO t055_poison (id) VALUES (1)")
                raise RuntimeError("boom")
        with pg_transaction() as conn:
            assert conn.info.transaction_status != psycopg.pq.TransactionStatus.INERROR
            # 行为断言：事务已完整回滚，且这条连接仍能正常执行语句。
            assert conn.execute("SELECT COUNT(*) FROM t055_poison").fetchone()[0] == 0


@pytestmark_pg
def test_pool_recycles_a_server_side_terminated_connection() -> None:
    """CW-055：断连——被服务端终止的连接必须由池回收，而非交给下一个借用者。

    模拟 PG 重启 / 网络闪断 / OOM killer 三类同构故障：连接在池里看着正常，
    但后端已经没了。这正是 check=ConnectionPool.check_connection 要挡住的场景。
    """
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, get_pg_pool

    with _env(**{DATABASE_URL_ENV: PG_DSN, POOL_MIN_ENV: "1", POOL_MAX_ENV: "1"}):
        pool = get_pg_pool()
        with pool.connection() as conn:
            killed_pid = int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
        # 连接已归还池中；从池外终止它。
        with psycopg.connect(PG_DSN, autocommit=True) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (killed_pid,))
        with pg_transaction() as conn:
            surviving_pid = int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert surviving_pid != killed_pid, "check=check_connection 必须换掉被终止的连接"


# ---------------------------------------------------------------------------
# CW-055 — SQLSTATE → 幂等恢复矩阵
# ---------------------------------------------------------------------------

# 矩阵用一张最小可复现的计费账本：UNIQUE 幂等键 + 余额行。它与真实的
# wallet_transactions / users 同构（幂等键去重、账本与余额必须同事务），
# 但建在共享测试库里，不污染已迁移的业务表。
_LEDGER_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS t055_ledger ("
    "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
    "idem TEXT NOT NULL UNIQUE, "
    "amount INTEGER NOT NULL CHECK (amount > 0))",
    "CREATE TABLE IF NOT EXISTS t055_balance (user_id TEXT PRIMARY KEY, balance INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS t055_lock (k TEXT PRIMARY KEY, v INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS t055_serial (id INTEGER PRIMARY KEY, v INTEGER NOT NULL)",
)
_SEED_BALANCE = 1000
_CHARGE_AMOUNT = 100


def _seed_billing(conn: psycopg.Connection) -> None:
    """把矩阵表复位到已知的干净起点（每个参数化用例各自调用）。"""
    for ddl in _LEDGER_DDL:
        conn.execute(ddl)
    conn.execute("TRUNCATE t055_ledger RESTART IDENTITY")
    conn.execute("TRUNCATE t055_balance")
    conn.execute("TRUNCATE t055_lock")
    conn.execute("TRUNCATE t055_serial")
    conn.execute("INSERT INTO t055_balance (user_id, balance) VALUES ('u1', %s)", (_SEED_BALANCE,))
    conn.execute("INSERT INTO t055_lock (k, v) VALUES ('a', 0), ('b', 0)")
    conn.execute("INSERT INTO t055_serial (id, v) VALUES (1, 0)")


def _apply_charge(conn: psycopg.Connection, idem: str, amount: int = _CHARGE_AMOUNT) -> None:
    """在**已开启的**事务内落账：账本行与余额扣减必须同生共死。"""
    conn.execute("INSERT INTO t055_ledger (idem, amount) VALUES (%s, %s)", (idem, amount))
    conn.execute("UPDATE t055_balance SET balance = balance - %s WHERE user_id = 'u1'", (amount,))


def _charge_idempotent(idem: str, amount: int = _CHARGE_AMOUNT) -> bool:
    """幂等扣费：账本 UNIQUE 键是唯一的去重事实源。

    True = 本次真正扣了费；False = 该幂等键此前已落账，本次安全跳过。
    这就是"失败重试不重复付费"的实现形态——重试安全性不依赖调用方
    记住上次的结果（调用方在超时/断连下根本不可能知道）。
    """
    try:
        with pg_transaction() as conn:
            _apply_charge(conn, idem, amount)
        return True
    except psycopg.errors.UniqueViolation:
        return False


def _read_billing_state() -> tuple[int, int]:
    """返回 (u1 余额, 账本行数)。"""
    with pg_transaction() as conn:
        balance = int(
            conn.execute("SELECT balance FROM t055_balance WHERE user_id = 'u1'").fetchone()[0]
        )
        rows = int(conn.execute("SELECT COUNT(*) FROM t055_ledger").fetchone()[0])
    return balance, rows


def _fail_query_canceled(idem: str) -> None:
    """57014 QueryCanceled：写入已执行，随后被语句超时打断。

    用 SET LOCAL 把超时压到 30ms 以在测试内稳定触发，不改动连接级 GUC，
    因而不会泄漏到池里其他借用者。
    """
    with pg_transaction() as conn:
        _apply_charge(conn, idem)
        conn.execute("SET LOCAL statement_timeout = '30ms'")
        conn.execute("SELECT pg_sleep(1)")


def _fail_serialization(idem: str) -> None:
    """40001 SerializationFailure：SERIALIZABLE 快照被并发已提交写作废。

    必须对**同一键**先读后写才能构成 SSI 依赖环（与
    test_pg_transaction_serializable_write_conflict 同一构型）；只读不写同键
    不会形成环，PG 就不会报 40001。对手只动 t055_serial（不动余额/账本），
    因此矩阵的"完整回滚"断言仍可统一为"余额未动、账本 0 行"。
    """
    with pg_transaction(isolation="SERIALIZABLE") as conn:
        conn.execute("SELECT v FROM t055_serial WHERE id = 1").fetchone()
        with pg_transaction() as other:
            other.execute("SELECT v FROM t055_serial WHERE id = 1").fetchone()
            other.execute("UPDATE t055_serial SET v = v + 1 WHERE id = 1")
        # 同键写回（基于已作废快照）+ 落账 → 退出时 40001，两者均必须回滚。
        conn.execute("UPDATE t055_serial SET v = v + 100 WHERE id = 1")
        _apply_charge(conn, idem)


def _fail_deadlock(idem: str) -> None:
    """40P01 DeadlockDetected：本事务是最后进入等待的一方，由它检出并中止。

    确定性构造：对手先持有 b 并阻塞在 a 上（此时尚无环），本事务再请求 b
    才闭环。对手的 deadlock_timeout 抬到 5s，确保检出者是本事务，
    避免"谁输"随机化而使用例变成 flaky。
    """
    with pg_transaction() as conn:
        conn.execute("SET LOCAL deadlock_timeout = '20ms'")
        conn.execute("UPDATE t055_lock SET v = v + 1 WHERE k = 'a'")
        adversary_holds_b = threading.Event()

        def adversary() -> None:
            try:
                with pg_transaction() as other:
                    other.execute("SET LOCAL deadlock_timeout = '5s'")
                    other.execute("UPDATE t055_lock SET v = v + 1 WHERE k = 'b'")
                    adversary_holds_b.set()
                    other.execute("UPDATE t055_lock SET v = v + 1 WHERE k = 'a'")
            except psycopg.errors.DeadlockDetected:
                pass

        thread = threading.Thread(target=adversary, daemon=True)
        thread.start()
        try:
            assert adversary_holds_b.wait(10), "对手必须先持有 b 并阻塞在 a 上"
            conn.execute("UPDATE t055_lock SET v = v + 1 WHERE k = 'b'")
            _apply_charge(conn, idem)
        finally:
            # 无论本事务是否被中止，都必须等对手退出，否则它持有的池连接
            # 会泄漏到下一个用例（而 autouse fixture 会 close_pg_pool）。
            thread.join(30)


def _fail_business_error(idem: str) -> None:
    """业务异常（无 SQLSTATE）：调用方主动放弃这次扣费。"""
    with pg_transaction() as conn:
        _apply_charge(conn, idem)
        raise RuntimeError("business rule rejected the charge")


_SQLSTATE_CASES: tuple[tuple[str, Callable[[str], None], str | None], ...] = (
    ("57014_query_canceled", _fail_query_canceled, "57014"),
    ("40001_serialization_failure", _fail_serialization, "40001"),
    ("40p01_deadlock_detected", _fail_deadlock, "40P01"),
    ("business_error", _fail_business_error, None),
)


@pytestmark_pg
@pytest.mark.parametrize(
    ("name", "attempt", "expected_sqlstate"),
    _SQLSTATE_CASES,
    ids=[case[0] for case in _SQLSTATE_CASES],
)
def test_sqlstate_recovery_matrix_rolls_back_and_recovers(
    name: str, attempt: Callable[[str], None], expected_sqlstate: str | None
) -> None:
    """CW-055 核心交付：SQLSTATE → 幂等恢复矩阵。

    四类失败（语句超时 / 串行化冲突 / 死锁 / 业务异常）必须一致地满足：
    ① 完整回滚（账本 0 行、余额未动）；② 连接可复用（池未被污染）；
    ③ 幂等重试只扣一次费；④ 再次重试仍不重复扣费。
    """
    idem = f"k-{name}"
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            _seed_billing(conn)

        if expected_sqlstate is None:
            with pytest.raises(RuntimeError, match="business rule"):
                attempt(idem)
        else:
            with pytest.raises(psycopg.Error) as excinfo:
                attempt(idem)
            assert excinfo.value.sqlstate == expected_sqlstate, (
                f"{name}: 期望 SQLSTATE {expected_sqlstate}，实际 {excinfo.value.sqlstate}"
            )

        # ① 完整回滚：失败的尝试不得留下任何账本痕迹。
        assert _read_billing_state() == (_SEED_BALANCE, 0), f"{name}: 失败后必须无任何落账"

        # ② 连接可复用：紧接着的借用必须健康，不被 aborted transaction 污染。
        with pg_transaction() as conn:
            assert conn.info.transaction_status != psycopg.pq.TransactionStatus.INERROR
            assert conn.execute("SELECT 1").fetchone()[0] == 1

        # ③ 幂等重试只扣一次。
        assert _charge_idempotent(idem) is True, f"{name}: 重试必须真正落账一次"
        assert _read_billing_state() == (_SEED_BALANCE - _CHARGE_AMOUNT, 1), (
            f"{name}: 重试后必须恰好扣一次费"
        )

        # ④ 再次重试不重复扣费。
        assert _charge_idempotent(idem) is False, f"{name}: 重复重试不得再次扣费"
        assert _read_billing_state() == (_SEED_BALANCE - _CHARGE_AMOUNT, 1), (
            f"{name}: 重复重试后余额与账本行数不得变化"
        )


@pytestmark_pg
def test_sequence_values_are_not_returned_by_a_rollback() -> None:
    """CW-055 明确要求钉住的 PG 事实：序列/identity 值**不随事务回滚归还**。

    回滚只丢弃行，不丢弃已分配的序列号。任何"回滚后 id 仍连续"的断言
    都是错的：它会把正常的序列空洞当成缺陷，或反过来掩盖真正的重复分配。
    """
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t055_seq ("
                "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v TEXT)"
            )
            conn.execute("TRUNCATE t055_seq RESTART IDENTITY")

        def _insert(value: str) -> int:
            with pg_transaction() as conn:
                row = conn.execute(
                    "INSERT INTO t055_seq (v) VALUES (%s) RETURNING id", (value,)
                ).fetchone()
                return int(row[0])

        kept = _insert("kept")
        with pytest.raises(RuntimeError, match="boom"):
            with pg_transaction() as conn:
                burned = int(
                    conn.execute(
                        "INSERT INTO t055_seq (v) VALUES ('burned') RETURNING id"
                    ).fetchone()[0]
                )
                raise RuntimeError("boom")
        after = _insert("after")

        assert burned == kept + 1
        assert after > burned, "回滚不得归还序列值：下一个 id 必须跳过被烧掉的号"
        with pg_transaction() as conn:
            values = [
                row[0] for row in conn.execute("SELECT v FROM t055_seq ORDER BY id").fetchall()
            ]
        assert values == ["kept", "after"], "回滚只丢弃行，不得留下 'burned'"


# ---------------------------------------------------------------------------
# CW-055 — 提交边界：门面不得中途提交
# ---------------------------------------------------------------------------


@pytestmark_pg
def test_pg_lane_facade_commit_never_publishes_early() -> None:
    """CW-055：PG lane 的 BusinessConnection.commit()/rollback() 是刻意 no-op，
    提交权只属于外层 pg_transaction / fenced_pg_transaction。

    用第二条连接的可见性来证明（而非日志或桩）：外层事务未退出前，
    门面 commit() 之后的写入对任何其他连接都不可见。
    """
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t055_publish (k TEXT PRIMARY KEY, v TEXT)")
            conn.execute("TRUNCATE t055_publish")

        with pg_transaction() as raw:
            facade = BusinessConnection.postgres(raw)
            facade.execute("INSERT INTO t055_publish (k, v) VALUES ('a', '1')")
            facade.commit()  # no-op：不得提前发布
            facade.rollback()  # no-op：不得废弃外层事务
            with facade.transaction():  # PG lane no-op
                facade.execute("INSERT INTO t055_publish (k, v) VALUES ('b', '2')")
            with facade.transaction(isolation="SERIALIZABLE"):
                facade.execute("INSERT INTO t055_publish (k, v) VALUES ('c', '3')")

            # 外层事务仍开着 → 另一条连接什么都看不见。
            with pg_transaction() as observer:
                visible = observer.execute("SELECT COUNT(*) FROM t055_publish").fetchone()[0]
                assert visible == 0, "门面 commit() 不得提前发布未完成的写入"
            # rollback() 也没能废弃它：三行仍活在外层事务里。
            assert raw.execute("SELECT COUNT(*) FROM t055_publish").fetchone()[0] == 3

        with pg_transaction() as conn:
            keys = [
                row[0] for row in conn.execute("SELECT k FROM t055_publish ORDER BY k").fetchall()
            ]
        assert keys == ["a", "b", "c"], "只有外层事务退出才发布全部写入"


@pytestmark_pg
def test_pg_lane_facade_never_calls_underlying_commit_or_rollback() -> None:
    """CW-055：结构性证明——门面在 PG lane 绥不触达底层连接的提交权。

    app/ 里共 84 处 BusinessConnection.postgres 构造点（generation_worker 67、
    recharge_routes 5 等），无法逐个跑行为测试；它们全部经由该门面，
    因此本用例 + 下面的静态调用点扫描共同闭合"全调用链均未中途提交"。
    """
    calls: list[str] = []
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as raw:
            real_commit = raw.commit
            real_rollback = raw.rollback

            def spy_commit() -> None:
                calls.append("commit")
                real_commit()

            def spy_rollback() -> None:
                calls.append("rollback")
                real_rollback()

            raw.commit = spy_commit  # type: ignore[method-assign]
            raw.rollback = spy_rollback  # type: ignore[method-assign]

            facade = BusinessConnection.postgres(raw)
            facade.execute("SELECT 1")
            facade.commit()
            facade.rollback()
            with facade:
                facade.execute("SELECT 1")
            with facade.transaction():
                facade.execute("SELECT 1")
            with facade.transaction(isolation="SERIALIZABLE"):
                facade.execute("SELECT 1")

            # 必须在外层 pg_transaction 退出**之前**断言：退出时
            # conn.transaction() 自己会合法地提交一次。
            assert calls == [], f"PG lane 门面不得调用底层提交/回滚，实际: {calls}"


_APP_PATTERN = re.compile(r"\.raw\.(commit|rollback)\(")
_AUTOCOMMIT_PATTERN = re.compile(r"\.raw\.autocommit\s*=\s*True")


def _scan_app_sources(pattern: re.Pattern[str]) -> list[str]:
    """返回 app/*.py 中命中该模式的 "文件:行号" 列表。"""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    return [
        f"{source.name}:{lineno}"
        for source in sorted(app_dir.glob("*.py"))
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]


def test_pg_lane_has_no_mid_transaction_commit_call_sites() -> None:
    """CW-055：静态收口——app/ 里不得出现绕过门面的底层提交/回滚。

    行为测试只能覆盖跑到的路径，而 84 处 PG 构造点分布在 10 个模块里。
    因此把不变量钉在源码层面：任何新增的 .raw.commit() / .raw.rollback()
    都必须位于门面 db_portable.py 内部（历史上有 SQLiteBackend 分支守卫，
    CW-042-b 退役 SQLite lane 后门面仅余 PG 路径，提交权仍只属于外层
    pg_transaction），否则本用例失败并列出具体调用点。
    """
    offenders = [
        site for site in _scan_app_sources(_APP_PATTERN) if not site.startswith("db_portable.py:")
    ]
    assert offenders == [], (
        "PG lane 的提交权只属于外层 pg_transaction / fenced_pg_transaction；"
        f"以下调用点绕过门面直接提交: {offenders}"
    )


def test_pool_borrow_autocommit_escape_hatch_stays_confined() -> None:
    """CW-055：唯一绕开 pg_transaction() 的池借用点必须可枚举。

    定时采集已将旧 viral_routes 刷新 lane 改为独立短事务；不再保留
    autocommit 例外。新增任何 .raw.autocommit = True 都会让本用例失败。
    """
    files = {site.split(":", 1)[0] for site in _scan_app_sources(_AUTOCOMMIT_PATTERN)}
    assert files == set(), (
        f"autocommit 逃生口集合发生变化: {sorted(files)}（需 CW-055 重新评审提交边界）"
    )
