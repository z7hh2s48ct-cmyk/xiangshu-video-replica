"""PostgreSQL runtime entry for the customer edition (T05 / DB-03).

This module owns the database *mode* resolution and the PG-side runtime
primitives: DSN handling, a psycopg3 connection pool, transaction contexts
(including the SERIALIZABLE replacement for SQLite ``BEGIN IMMEDIATE``),
server-side time, and the customer-production fail-closed guard.

Design constraints:
- No ORM, no Redis, no queue framework: psycopg3 + psycopg_pool only.
- The SQLite runtime stays untouched for the internal P0 mode; this module is
  additive until business modules migrate lane by lane.
- Customer production must refuse to start on SQLite or without a PG URL.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import psycopg
from psycopg_pool import ConnectionPool
from psycopg_pool.errors import PoolTimeout

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "VIDEO_REPLICA_DATABASE_URL"
DB_PATH_ENV = "VIDEO_REPLICA_DB_PATH"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
POOL_MIN_ENV = "VIDEO_REPLICA_PG_POOL_MIN"
POOL_MAX_ENV = "VIDEO_REPLICA_PG_POOL_MAX"
POOL_TIMEOUT_ENV = "VIDEO_REPLICA_PG_POOL_TIMEOUT"
# A misconfigured POOL_MAX must not drain the server's connection budget
# (shared by the multi-instance API/Worker fleet, M1 review LOW).
POOL_MAX_CEILING = 64
# CW-055：借用超时同样要有硬上限。它就是"池耗尽有界失败"的那个界；
# 一个被误配成数小时的 timeout 会把有界失败退化回 2026-09-07 评审 §7-0
# 那种无限期挂住（实测曾拖停公平队列 19 分钟），因此与单语句超时同量级。
POOL_TIMEOUT_CEILING = 300.0
# 空闲事务护栏（2026-09-07 梳理）：业务侧纪律是短事务，一个连接停留在
# "事务开着但不发语句"超过阈值即是缺陷（持锁泄漏会串住整个容量/队列路径）。
# 默认 5 分钟——高于最长的合法请求内外呼窗口，仍能把真实泄漏变成快速失败。

DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 8
# Pool hygiene (M0 review M1): stale connections are recycled instead of
# being handed out after a server restart or network blip.
DEFAULT_POOL_MAX_LIFETIME = 3600.0
DEFAULT_POOL_MAX_IDLE = 600.0
DEFAULT_POOL_TIMEOUT = 30.0
# 2026-09-07 评审 §7-0：claim 事务的提交权在外层 fenced 块，一旦有调用方把
# 未提交事务长期搁置（实测曾持容量行锁 19 分钟拖停公平队列），必须由数据库
# 侧护栏快速失败，而不是无限排队。单语句 5 分钟、事务内闲置 60 秒。
PG_STATEMENT_TIMEOUT_MS = 300_000
PG_IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000
PG_POOL_OPTIONS = (
    f"-c statement_timeout={PG_STATEMENT_TIMEOUT_MS}"
    f" -c idle_in_transaction_session_timeout={PG_IDLE_IN_TRANSACTION_TIMEOUT_MS}"
)
PG_URL_SCHEMES = ("postgresql://", "postgres://")
SQLITE_URL_SCHEMES = ("sqlite:///", "sqlite://")
_TRUTHY = {"1", "true", "yes", "on"}
_TLS_ENFORCING_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})

# M0 review M3: the isolation level is interpolated into a SET statement, so
# it must be constrained to a closed set (Literal for callers, frozenset for
# runtime validation) instead of accepting arbitrary strings.
IsolationLevel = Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]
_ALLOWED_ISOLATION_LEVELS: frozenset[str] = frozenset(
    {"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"}
)


class DatabaseMode(Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class DatabaseConfig:
    mode: DatabaseMode
    dsn: str | None
    sqlite_path: str | None


@dataclass(frozen=True)
class PgReadyInfo:
    dsn: str
    server_now: datetime
    pool_size: int


def _is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def _sqlite_path_from_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.split("sqlite:///", 1)[-1]
    return url.split("sqlite://", 1)[-1]


def _fetch_scalar(conn: psycopg.Connection, sql: str) -> object:
    """Fetch a single-row scalar, narrowing the Optional from fetchone()."""
    row = conn.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"query returned no rows: {sql}")
    return row[0]


class MissingDatabaseConfigError(ValueError):
    """The database-mode resolver found no database environment at all.

    Narrower than the generic ``ValueError`` also used for unsupported
    schemes: the API lifespan may legitimately tolerate this one on the
    internal lane (the legacy runtime resolves per-request), while a
    mistyped URL is a configuration error that must fail closed (Codex P1).
    """


def resolve_database_config() -> DatabaseConfig:
    """Resolve the active database mode from the environment (CW-025: 全环境 PG-only).

    ``VIDEO_REPLICA_DATABASE_URL`` 必须设置为 ``postgresql://`` (或 ``postgres://`` 别名)。

    CW-025 后全环境 fail-closed：
    - sqlite:// URL → RuntimeError
    - VIDEO_REPLICA_DB_PATH → RuntimeError
    - 缺 DSN → RuntimeError
    - 不支持的 scheme → ValueError (保留原有行为)

    历史 SQLite 工具（backup/sqlite_to_postgres/gate1_*）走 CW-060 独立白名单，
    不经过本函数的在线通道。
    """
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    db_path = os.environ.get(DB_PATH_ENV, "").strip()

    # CW-025: DB_PATH 在线入口全环境拒绝（历史工具走 CW-060 白名单）
    if db_path:
        raise RuntimeError(
            f"customer edition requires PostgreSQL: "
            f"{DB_PATH_ENV} is rejected in all environments "
            f"(set {DATABASE_URL_ENV} to a postgresql:// DSN; "
            f"legacy SQLite tools are isolated by CW-060)"
        )

    if url:
        if url.startswith(PG_URL_SCHEMES):
            return DatabaseConfig(mode=DatabaseMode.POSTGRESQL, dsn=url, sqlite_path=None)
        # CW-025: sqlite:// URL 全环境拒绝
        if url.startswith(SQLITE_URL_SCHEMES):
            raise RuntimeError(
                f"customer edition requires PostgreSQL: "
                f"sqlite:// URL is rejected in all environments "
                f"(set {DATABASE_URL_ENV} to a postgresql:// DSN; "
                f"legacy SQLite tools are isolated by CW-060)"
            )
        scheme = url.split(":", 1)[0]
        raise ValueError(f"unsupported database URL scheme: {scheme}:// (expected postgresql://)")

    # CW-025: 缺 DSN 全环境 fail-closed（不再区分 internal/customer lane）
    raise RuntimeError(
        f"customer edition requires PostgreSQL: "
        f"neither {DATABASE_URL_ENV} nor {DB_PATH_ENV} is set "
        f"(set {DATABASE_URL_ENV} to a postgresql:// DSN)"
    )


def validate_customer_production(config: DatabaseConfig) -> None:
    """Fail closed when a customer-production database boundary is unsafe.

    ``VIDEO_REPLICA_CUSTOMER_PRODUCTION`` marks the customer boundary. In that
    mode a missing URL or a SQLite target must abort startup instead of
    silently falling back to the single-file internal runtime. A leftover
    ``VIDEO_REPLICA_DB_PATH`` alongside a PG URL is likewise rejected: an
    ambiguous customer-production configuration is an error, not a hint.
    """
    if not _is_customer_production():
        return
    if config.mode is not DatabaseMode.POSTGRESQL:
        raise RuntimeError(
            "customer production requires PostgreSQL: set "
            f"{DATABASE_URL_ENV} to a postgresql:// DSN (SQLite startup is rejected)"
        )
    leftover_db_path = os.environ.get(DB_PATH_ENV, "").strip()
    if leftover_db_path:
        raise RuntimeError(
            "customer production must not set the legacy "
            f"{DB_PATH_ENV}; remove it and keep only {DATABASE_URL_ENV} "
            "(ambiguous database configuration is rejected)"
        )
    if config.dsn is None:
        raise RuntimeError("customer production PostgreSQL DSN is missing")
    try:
        sslmode_values = parse_qs(urlsplit(config.dsn).query, keep_blank_values=True).get(
            "sslmode", []
        )
    except ValueError as exc:
        raise RuntimeError("customer production PostgreSQL DSN is malformed") from exc
    if len(sslmode_values) != 1 or sslmode_values[0].strip().lower() not in _TLS_ENFORCING_SSLMODES:
        raise RuntimeError(
            "customer production PostgreSQL must enforce TLS: set exactly one "
            "sslmode=require, sslmode=verify-ca, or sslmode=verify-full"
        )


class CliDatabaseConfigError(RuntimeError):
    """A maintenance/seed CLI received a database configuration PG-01 rejects.

    Raised by :func:`resolve_cli_pg_dsn` before any connection attempt, so a
    misconfigured command fails closed with a fixed, credential-free message
    and never creates a database file (CW-057).
    """


def resolve_cli_pg_dsn(explicit: str | None = None) -> str:
    """Resolve the PostgreSQL DSN for one non-HTTP CLI invocation (CW-057).

    The single PG entry point for the maintenance/seed/admin CLI surface
    (PG-01/PG-08): ``--database-url`` wins over the environment, and the
    CW-025 fail-closed :func:`resolve_database_config` contract applies
    verbatim when the argument is empty — a missing DSN, a ``sqlite://`` URL
    or a leftover ``VIDEO_REPLICA_DB_PATH`` raises :class:`CliDatabaseConfigError`
    instead of starting against SQLite or creating a database file.
    """
    candidate = (explicit or "").strip()
    if not candidate:
        try:
            config = resolve_database_config()
        except (RuntimeError, ValueError) as exc:
            raise CliDatabaseConfigError(str(exc)) from exc
        if config.mode is not DatabaseMode.POSTGRESQL or config.dsn is None:
            raise CliDatabaseConfigError("maintenance/seed CLIs require PostgreSQL")
        return config.dsn
    # An explicit DSN must not silently coexist with a legacy DB_PATH leftover:
    # resolve_database_config() rejects that combination for the runtime lane,
    # and an ambiguous host configuration is an error, not a hint.
    if os.environ.get(DB_PATH_ENV, "").strip():
        raise CliDatabaseConfigError(
            "customer edition requires PostgreSQL: "
            f"{DB_PATH_ENV} is rejected in all environments "
            f"(set {DATABASE_URL_ENV} to a postgresql:// DSN; "
            "legacy SQLite tools are isolated by CW-060)"
        )
    if candidate.startswith(SQLITE_URL_SCHEMES):
        raise CliDatabaseConfigError(
            "customer edition requires PostgreSQL: sqlite:// URLs are rejected "
            "for maintenance/seed CLIs (PG-01); pass a postgresql:// DSN via "
            f"--database-url or {DATABASE_URL_ENV}"
        )
    if not candidate.startswith(PG_URL_SCHEMES):
        scheme = candidate.split(":", 1)[0]
        raise CliDatabaseConfigError(
            f"unsupported database URL scheme: {scheme}:// (expected postgresql://)"
        )
    return candidate


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _pool_bounds() -> tuple[int, int]:
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            logger.warning("invalid %s=%r, using default %d", name, raw, default)
            return default

    pool_min = max(0, _int_env(POOL_MIN_ENV, DEFAULT_POOL_MIN))
    pool_max = max(pool_min, _int_env(POOL_MAX_ENV, DEFAULT_POOL_MAX))
    if pool_max > POOL_MAX_CEILING:
        logger.warning(
            "capping %s=%d to the hard ceiling %d", POOL_MAX_ENV, pool_max, POOL_MAX_CEILING
        )
        pool_max = POOL_MAX_CEILING
    if pool_min > POOL_MAX_CEILING:
        # Clamp the minimum with the maximum: ConnectionPool rejects a
        # maximum smaller than its minimum, so an unclamped min would turn
        # the connection-budget guard into a startup failure (Codex P2).
        logger.warning(
            "capping %s=%d to the hard ceiling %d", POOL_MIN_ENV, pool_min, POOL_MAX_CEILING
        )
        pool_min = POOL_MAX_CEILING
    return pool_min, pool_max


def _redacted_pool_dsn(pool: ConnectionPool) -> str:
    """取连接池的 DSN 并脱敏，仅供日志使用。

    ``ConnectionPool.conninfo`` 在 psycopg_pool 3.3 里可以是 callable（用于轮换
    凭据），所以两种形态都要先收敛成 str 再交给唯一的脱敏器。
    """
    conninfo = pool.conninfo
    return redact_postgres_dsn(conninfo() if callable(conninfo) else conninfo)


def _pool_timeout() -> float:
    """CW-055：池借用超时（秒）。必须可配置且带硬上限。

    池耗尽时的行为完全由这个值决定：默认 30s 适合单机；多实例共用一个 PG
    时应按实例数收紧，否则一次故障会让整机一起排队到超时。
    非法值（非数字 / <=0）一律回落默认值而不是照单全收：0 与负值在
    psycopg_pool 里等于"不等待"，会把容量抖动直接变成请求失败。
    """
    raw = os.environ.get(POOL_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_POOL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r, using default %.1f", POOL_TIMEOUT_ENV, raw, DEFAULT_POOL_TIMEOUT
        )
        return DEFAULT_POOL_TIMEOUT
    if value <= 0:
        logger.warning(
            "invalid %s=%r (must be > 0), using default %.1f",
            POOL_TIMEOUT_ENV,
            raw,
            DEFAULT_POOL_TIMEOUT,
        )
        return DEFAULT_POOL_TIMEOUT
    if value > POOL_TIMEOUT_CEILING:
        logger.warning(
            "capping %s=%r to the hard ceiling %.1f",
            POOL_TIMEOUT_ENV,
            raw,
            POOL_TIMEOUT_CEILING,
        )
        return POOL_TIMEOUT_CEILING
    return value


def _as_datetime(value: object) -> datetime:
    """Narrow a fetched ``now()`` value (psycopg3 already returns a
    tz-aware datetime; the str round-trip is only a fallback)."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def get_pg_pool() -> ConnectionPool:
    """Return the process-wide PG connection pool (lazily created)."""
    global _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            config = resolve_database_config()
            if config.mode is not DatabaseMode.POSTGRESQL or not config.dsn:
                raise RuntimeError(
                    "PG pool requested but the resolved database mode is not PostgreSQL"
                )
            pool_min, pool_max = _pool_bounds()
            pool = ConnectionPool(
                config.dsn,
                min_size=pool_min,
                max_size=pool_max,
                open=True,
                name="video-replica-pg",
                check=ConnectionPool.check_connection,
                max_lifetime=DEFAULT_POOL_MAX_LIFETIME,
                max_idle=DEFAULT_POOL_MAX_IDLE,
                timeout=_pool_timeout(),
                kwargs={"options": PG_POOL_OPTIONS},
            )
            _pool = pool
        return _pool


def close_pg_pool() -> None:
    """Close the pool (used by tests and graceful shutdown)."""
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.close()
        _pool = None


@contextmanager
def pg_transaction(*, isolation: IsolationLevel | None = None) -> Iterator[psycopg.Connection]:
    """Run a transaction on a pooled connection.

    ``isolation="SERIALIZABLE"`` is the replacement for SQLite's
    ``BEGIN IMMEDIATE`` write fencing: committers that lose a concurrent
    write race fail with ``psycopg.errors.SerializationFailure`` and the
    caller decides how to retry.
    """
    if isolation is not None:
        level = isolation.upper().strip()
        if level not in _ALLOWED_ISOLATION_LEVELS:
            # Fail before acquiring a pooled connection: the level feeds a
            # SET statement and must never be an arbitrary string.
            raise ValueError(
                f"unsupported isolation level {isolation!r}; expected one of "
                f"{sorted(_ALLOWED_ISOLATION_LEVELS)}"
            )
    pool = get_pg_pool()
    acquired = False
    try:
        with pool.connection() as conn:
            acquired = True
            # pool.connection() returns the connection to the pool; a failed
            # transaction was already rolled back by conn.transaction().
            with conn.transaction():
                if isolation is not None:
                    conn.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
                yield conn
    except PoolTimeout:
        # CW-055：池耗尽必须留下可诊断且不泄密的痕迹。没有这条日志时，
        # 运维只看到一个 PoolTimeout，分不清是"池配小了"还是"PG 不可达"；
        # 而 pool.conninfo 带口令，绝不能原样进日志。
        if not acquired:
            # 只记本层借用失败：嵌套 pg_transaction 的内层耗尽已由内层记过，
            # 逐层重复会把一次池耗尽放大成 N 条日志。
            logger.warning(
                "PG connection pool exhausted: no connection became available within %.1fs for %s",
                pool.timeout,
                _redacted_pool_dsn(pool),
            )
        raise


def pg_server_now() -> datetime:
    """Return PostgreSQL server time (the only trusted clock, SES-01)."""
    pool = get_pg_pool()
    with pool.connection() as conn:
        return _as_datetime(_fetch_scalar(conn, "SELECT now()"))


def redact_postgres_dsn(dsn: str) -> str:
    """Remove credentials and optional DSN parameters before logging.

    The single canonical redactor for the app layer; ``scripts.reconcile_customer_billing``
    re-imports it so every lane redacts identically.
    """

    try:
        parts = urlsplit(dsn)
        if not parts.scheme.startswith("postgres"):
            return "<redacted-postgres-dsn>"
        hostname = parts.hostname or ""
        port = parts.port
        username = parts.username
    except (UnicodeError, ValueError):
        return "<redacted-postgres-dsn>"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None:
        netloc += f":{port}"
    if username:
        netloc = f"{quote(unquote(username), safe='')}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def check_pg_ready() -> PgReadyInfo:
    """Ready check for API/Worker startup: writable endpoint + round-trip."""
    config = resolve_database_config()
    validate_customer_production(config)
    pool = get_pg_pool()
    with pool.connection() as conn:
        transaction_read_only = (
            str(_fetch_scalar(conn, "SHOW transaction_read_only")).strip().lower()
        )
        if transaction_read_only != "off":
            raise RuntimeError(
                "PostgreSQL endpoint is read-only; a read-write primary endpoint is required"
            )
        server_now = _as_datetime(_fetch_scalar(conn, "SELECT now()"))
        _fetch_scalar(conn, "SELECT 1")
    # PgReadyInfo.dsn carries no credentials: once serialized into a log line
    # or a readiness endpoint the full DSN would leak the password (M1 review LOW).
    return PgReadyInfo(
        dsn=redact_postgres_dsn(config.dsn or ""),
        server_now=server_now,
        pool_size=pool.max_size,
    )
