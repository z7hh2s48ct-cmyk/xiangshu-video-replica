"""T07 reconciliation for the one-shot SQLite-to-PostgreSQL cutover.

Reports contain counts and SHA-256 digests only. Raw business values,
credentials, storage URLs, activation codes, and tokens are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Dialect = Literal["sqlite", "postgresql"]
EXCLUDED_TABLES = frozenset({"alembic_version"})
# PostgreSQL-only tables created by revisions 026/027/028/029/031/032 for the
# customer production line (per-operator admin sessions, T09/DB-08; the
# activation code catalog incl. its append-only event table, T10/ACT-01;
# the customer device slots, pairing requests, session state/events and
# idempotency envelopes, T13/ACT-05; the admin write idempotency ledger,
# T12/ACT-04; the shared security rate-limit counters and the append-only
# auth-failure audit, T15/ACT-08; the append-only admin device operations
# audit, T18/DEV-03; the append-only audited admin adjustments, T23/BILL-02;
# the per-user fair-queue cursors, T25/041; committed customer-write fencing
# evidence, T37/042; the admin password-credential registry, T38/043; and the
# per-customer unit-price overrides, T39/044; the cost/price ledgers,
# W8/W10/056/058/059; and the administrator-configured recharge packages,
# 20260922T1200). They have no SQLite counterpart
# in the T07 import source, so an empty such table on the target is expected;
# a non-empty one is divergent state and must fail closed.
PG_ONLY_TABLES: frozenset[str] = frozenset(
    {
        # Web QR credentials have no historical SQLite source; non-empty targets
        # remain divergent and must never be overwritten by archive import.
        "publish_browser_accounts",
        "publish_browser_logins",
        # 20260917T1000_publish_records: delivery queue written by the publish
        # worker from browser-login credentials; no SQLite lineage either.
        "publish_records",
        "viral_script_cache",
        "h3_provider_accounts",
        "h3_provider_task_accounts",
        "admin_password_credentials",
        "admin_sessions",
        "admin_adjustments",
        "admin_device_events",
        "activation_code_batches",
        "activation_codes",
        "activation_code_deliveries",
        "activation_code_exports",
        "activation_code_activations",
        "activation_code_events",
        # 089_customer_api_keys: 客户程序 API Key 泳道，PG-only（089 明确 SQLite lane 不建表）。
        "customer_api_keys",
        # 20260912T1353_customer_discounts: 客户消耗侧折扣配置，PG-only
        # （非 postgresql 方言 return）。
        "customer_discounts",
        "customer_credit_pricing",
        "legacy_credit_policy",
        "wallet_credit_conversions",
        # New-only economics tables: historical archive input must never populate them.
        "billing_tariffs",
        "billing_operations",
        "billing_attempts",
        "billing_credit_lots",
        "billing_evidence",
        "viral_collection_batches",
        "viral_collection_members",
        "viral_collection_charges",
        "customer_devices",
        "device_pairing_requests",
        "customer_session_state",
        "customer_session_events",
        "customer_idempotency_envelopes",
        "customer_unit_prices",
        "customer_fencing_write_evidence",
        "customer_authorization_evidence",
        "ops_alert_state",
        "admin_write_idempotency",
        "security_rate_limit_counters",
        "security_auth_failures",
        "user_queue_cursors",
        "daily_external_prices",
        "operation_cost_rates",
        "operation_cost_records",
        # 20260916T1400_content_objects: 内容寻址登记表（去重 + 引用计数），PG-only
        # （迁移在非 postgresql 方言下 return，SQLite lane 不建表）。T07 导入源无此表，
        # 目标库为空属预期；非空即 divergent，仍 fail closed。
        "content_objects",
        # 20260921T1200_sub_account_quotas: 子账号月度额度配置（母账号内控限额），
        # PG-only（守卫同 20260916T1400）。T07 导入源无此表，目标库为空属预期；
        # 非空即 divergent，仍 fail closed。
        "sub_account_quotas",
        # 20260922T1200_recharge_packages: 管理员配置的充值套餐（档位 + 赠送积分 +
        # 折扣权益）仅在 PG 落地（非 postgresql 方言 return）。T07 导入源无此表，
        # 目标库为空属预期；非空即 divergent，仍 fail closed。
        "recharge_packages",
        # 20260922T1800_sub_account_permissions: 子账号功能权限配置（Phase 3b
        # 权限矩阵，无行 = 全允许），PG-only（守卫同 20260916T1400）。T07 导入源
        # 无此表，目标库为空属预期；非空即 divergent，仍 fail closed。
        "sub_account_permissions",
        # 20260922T2000_analysis_task_attempts: 拆解失败按 attempt 留痕的重试
        # 历史表，PG-only（守卫同 20260916T1400）。T07 导入源无此表，目标库为空
        # 属预期；非空即 divergent，仍 fail closed。
        "analysis_task_attempts",
    }
)

# Most PG-only tables must be empty before cutover. Rate configuration is the
# one exception: revision 056 seeds these exact defaults. Any edit, omission,
# or extra subject is pre-existing target state and must still fail closed.
PG_ONLY_SEEDED_TABLES: frozenset[str] = frozenset(
    {"operation_cost_rates", "customer_credit_pricing", "legacy_credit_policy"}
)
_OPERATION_COST_RATE_SEEDS = (
    ("character_sheet_image", "upstream_cost", "image", None, 5, None),
    ("context_ir", "upstream_cost", "call", None, 5, None),
    ("external_price_2k", "external_price", "second", "2K", 20, None),
    ("external_price_768p", "external_price", "second", "768P", 12, None),
    ("first_frame_image", "upstream_cost", "image", None, 5, None),
    ("video_analysis_2k", "upstream_cost", "second", "2K", 15, None),
    ("video_analysis_768p", "upstream_cost", "second", "768P", 9, None),
    ("video_generation_2k", "upstream_cost", "second", "2K", 15, None),
    ("video_generation_768p", "upstream_cost", "second", "768P", 9, None),
)

# Shared tables may carry columns that exist only on the PostgreSQL lane
# (T25/041 adds runtime_settings.fair_queue_enabled; T37/042 materializes
# typed probe timestamps while retaining the legacy SQLite audit strings).
# The column-set contract below must exempt these, or the T07 import would
# fail closed on its own published migrations.
PG_ONLY_COLUMNS: dict[str, frozenset[str]] = {
    "viral_videos": frozenset({"homepage_featured", "deleted_at", "collection_published"}),
    "viral_media_preparations": frozenset({"cache_scope", "storage_uri"}),
    "viral_runtime_controls": frozenset(
        {"collection_interval_days", "keywords_json", "per_keyword_limit", "next_collection_at"}
    ),
    "viral_refresh_tasks": frozenset({"collection_config_json", "checkpoint_json", "retry_count"}),
    "runtime_settings": frozenset({"fair_queue_enabled", "active_payment_provider"}),
    "audit_logs": frozenset({"occurred_at"}),
    "generation_tasks": frozenset({"created_at_utc", "discount_rate_snapshot"}),
    # 083_recharge_orders_multi_provider: WeChat Native 支付回执列仅存在于
    # PG（T07 的 SQLite 源 schema 冻结于 042 前基线）。
    "recharge_orders": frozenset(
        {
            "prepay_id",
            "code_url",
            "transaction_id",
            "credit_pricing_snapshot_json",
            # 20260922T1200_recharge_packages: 套餐来源与下单冻结快照仅存在于 PG
            # （T07 的 SQLite 源 schema 冻结于其前基线）。
            "package_id",
            "package_snapshot_json",
        }
    ),
    # 086_remove_device_slot_constraints: 每用户设备上限列仅存在于 PG
    # （T07 的 SQLite 源 schema 冻结于 042 前基线）。
    # 20260912T1400_customer_registration_credentials: 自助注册凭证列仅存在于 PG
    # （SQLite 内部泳道从不自注册客户，088 在该 lane 为 guarded no-op）。
    # 20260919T1200_sub_accounts: 母子账号层级列仅存在于 PG（客户版 V3 新体系，
    # T07 的 SQLite 源 schema 冻结于其前基线）。
    "users": frozenset(
        {"max_devices", "password_hash", "registration_source", "account_type", "parent_user_id"}
    ),
    # 20260912T1353_customer_discounts: wallet_transactions.discount_rate 仅存在于 PG
    # （本迁移非 postgresql 方言 return，SQLite lane 不建此列）。
    # 20260912T2200: legacy records have no API key attribution; import as NULL.
    # 20260919T1300_wallet_actor: 子账号消费的实际操作人列仅存在于 PG。
    "wallet_transactions": frozenset(
        {
            "discount_rate",
            "api_key_id",
            "auth_source",
            "pricing_snapshot_json",
            "billing_operation_id",
            "actor_user_id",
        }
    ),
    # 20260919T1500_device_parent_cascade: 设备的母账号归属列仅存在于 PG。
    "customer_devices": frozenset({"parent_user_id"}),
    # 20260916T1400_content_objects: 内容寻址登记表的外键列仅存在于 PG
    # （该迁移非 postgresql 方言 return，SQLite lane 不加此列）。
    # T07 导入时留空，随后由 `python -m scripts.backfill_content_objects` 回填。
    "assets": frozenset({"content_object_id"}),
    # 20260919T1000_oral_soft_delete: 口播分身/声音克隆的软删列仅存在于 PG
    # （SQLite 内部泳道在 065 后退役，无历史软删事实）。
    "oral_avatars": frozenset({"deleted_at", "deleted_by_user_id"}),
    "oral_voices": frozenset({"deleted_at", "deleted_by_user_id"}),
    # 20260919T1000_browser_account_probe: 健康探针调度列仅存在于 PG。
    "publish_browser_accounts": frozenset(
        {"next_probe_at", "probe_lease_owner", "probe_lease_expires_at", "probe_attempt_count"}
    ),
    # 20260920T0100_add_api_metadata_to_billing_ops: viral_data 的 API 类型
    # 元数据仅存在于 PG（SQLite lane 无计费运行时）。
    "billing_operations": frozenset({"api_metadata"}),
    # 20260922T1200_analysis_task_failure_diagnostic: analysis_tasks 的上游结构化
    # 诊断列仅存在于 PG（非 postgresql 方言 return，SQLite lane 不加此列）。
    # 同链的 20260922T1600 request_id 两泳道都加，故不在此豁免。
    "analysis_tasks": frozenset({"upstream_diagnostic_json"}),
}
DEFAULT_DIGEST_BATCH_SIZE = 1000
_DIGEST_MODULUS = 1 << 256
_MAX_SAFE_MESSAGE_LENGTH = 600

# Only these exception types carry verbatim messages in CLI failures. They are
# raised exclusively by this tool's own guardrails with fixed wording; driver
# and row-conversion exceptions (sqlite3.Error, psycopg.Error, ValueError,
# json.JSONDecodeError, ...) can embed raw business values such as
# PostgreSQL's ``DETAIL: Failing row contains (...)`` and therefore degrade
# to their class name plus a bounded stage hint instead.
_VERBATIM_ERROR_TYPES: tuple[type[BaseException], ...] = (
    RuntimeError,
    FileNotFoundError,
    FileExistsError,
    PermissionError,
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    target_type: str
    nullable: bool = True


@dataclass(frozen=True)
class TableDigest:
    row_count: int
    primary_key_sha256: str | None
    row_sha256: str


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    scope: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TableReconciliation:
    table: str
    source_count: int
    target_count: int
    source_pk_sha256: str | None
    target_pk_sha256: str | None
    source_rows_sha256: str
    target_rows_sha256: str

    @property
    def matches(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.source_pk_sha256 == self.target_pk_sha256
            and self.source_rows_sha256 == self.target_rows_sha256
        )

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["matches"] = self.matches
        return result


@dataclass(frozen=True)
class ReconciliationReport:
    tables: tuple[TableReconciliation, ...]
    issues: tuple[ReconciliationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues and all(table.matches for table in self.tables)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tables": [table.to_dict() for table in self.tables],
            "issues": [issue.to_dict() for issue in self.issues],
        }


class _UnorderedDigest:
    """Bounded-memory multiset digest.

    Each canonical row is SHA-256 hashed, then accumulated with independent
    commutative moments. The final SHA-256 binds count, sum, xor, and squared
    sum. This keeps memory constant while remaining insensitive to row order
    and sensitive to duplicate multiplicity.
    """

    __slots__ = ("count", "total", "xor", "squares")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0
        self.xor = 0
        self.squares = 0

    def update(self, payload: bytes) -> None:
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        self.count += 1
        self.total = (self.total + value) % _DIGEST_MODULUS
        self.xor ^= value
        self.squares = (self.squares + value * value) % _DIGEST_MODULUS

    def hexdigest(self, domain: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(domain)
        digest.update(self.count.to_bytes(16, "big"))
        digest.update(self.total.to_bytes(32, "big"))
        digest.update(self.xor.to_bytes(32, "big"))
        digest.update(self.squares.to_bytes(32, "big"))
        return digest.hexdigest()


def redact_postgres_dsn(dsn: str) -> str:
    """Delegate to the app-layer canonical redactor (db_pg.redact_postgres_dsn)."""

    from app.db_pg import redact_postgres_dsn as _redact

    return _redact(dsn)


def _dsn_sensitive_values(dsn: str) -> set[str]:
    candidates = {dsn}
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return candidates
    for value in (parts.username, parts.password, parts.fragment):
        if value:
            decoded = unquote(value)
            candidates.update({value, decoded, quote(decoded, safe="")})
    try:
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        query_pairs = []
    for key, value in query_pairs:
        if key:
            decoded_key = unquote(key)
            candidates.update({key, decoded_key, quote(decoded_key, safe="")})
        if value:
            decoded_value = unquote(value)
            candidates.update({value, decoded_value, quote(decoded_value, safe="")})
    return candidates


def safe_error_message(error: Exception, dsn: str, *, stage: str = "migration") -> str:
    """Render a migration failure without exposing source business values.

    ``str(error)`` of driver or row-conversion exceptions can embed storage
    URIs, token digests, provider configuration, or PostgreSQL's
    ``DETAIL: Failing row contains (...)`` with the failing row itself. Only
    the tool's own guardrail exception types keep a verbatim message — still
    scrubbed for DSN fragments and hard length-bound; every other exception
    degrades to its class name plus the bounded stage hint.
    """

    if isinstance(error, _VERBATIM_ERROR_TYPES):
        message = str(error)
        for candidate in sorted(
            (item for item in _dsn_sensitive_values(dsn) if item),
            key=len,
            reverse=True,
        ):
            message = message.replace(candidate, "<redacted>")
        if len(message) > _MAX_SAFE_MESSAGE_LENGTH:
            message = message[:_MAX_SAFE_MESSAGE_LENGTH] + "…<truncated>"
        return f"{type(error).__name__}: {message}"
    return (
        f"{type(error).__name__} during {stage}; error text suppressed "
        "(migration errors never include source business values)"
    )


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def connect_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_open_sqlite_readonly = connect_sqlite_readonly


def _row_value(row: object, key: str, index: int = 0) -> object:
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, Mapping):
        return row[key]
    return row[index]  # type: ignore[index]


def _table_names(conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect) -> list[str]:
    if dialect == "sqlite":
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [
            str(_row_value(row, "name"))
            for row in rows
            if str(_row_value(row, "name")) not in EXCLUDED_TABLES
        ]
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [
        str(_row_value(row, "table_name"))
        for row in rows
        if str(_row_value(row, "table_name")) not in EXCLUDED_TABLES
    ]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    rows = conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})").fetchall()
    columns = [str(row[1]) for row in rows]
    primary_key = [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5])]
    return columns, primary_key


def _pg_columns(
    conn: psycopg.Connection[Any], table: str
) -> tuple[list[str], list[str], dict[str, str]]:
    rows = conn.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    columns = [str(_row_value(row, "column_name")) for row in rows]
    types = {
        str(_row_value(row, "column_name")): str(
            _row_value(row, "udt_name", 2) or _row_value(row, "data_type", 1)
        ).lower()
        for row in rows
    }
    pk_rows = conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (table,),
    ).fetchall()
    primary_key = [str(_row_value(row, "column_name")) for row in pk_rows]
    return columns, primary_key, types


def canonical_value(value: object, target_type: str | None) -> object:
    if value is None:
        return None
    normalized_type = None if target_type is None else target_type.lower()
    if isinstance(value, memoryview):
        value = bytes(value)
    if normalized_type == "bytea" or isinstance(value, bytes):
        raw = value if isinstance(value, bytes) else bytes(value)
        return {"bytes_sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    if normalized_type in {"bool", "boolean"}:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "t", "yes", "on"}:
                return True
            if normalized in {"0", "false", "f", "no", "off"}:
                return False
            raise ValueError("invalid boolean value in reconciliation input")
        return bool(value)
    if normalized_type in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return int(value)
    if normalized_type in {"numeric", "decimal"}:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            return str(number)
        if number == 0:
            return "0"
        return format(number.normalize(), "f")
    if normalized_type in {"float4", "float8", "real", "double precision"}:
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return format(number, ".17g")
    if normalized_type == "uuid":
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    if normalized_type == "date":
        parsed_date = value if isinstance(value, date) else date.fromisoformat(str(value))
        return parsed_date.isoformat()
    if normalized_type in {"timestamp", "timestamptz"}:
        parsed_datetime = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return parsed_datetime.isoformat()
    if normalized_type in {"time", "timetz"}:
        parsed_time = value if isinstance(value, time) else time.fromisoformat(str(value))
        return parsed_time.isoformat()
    if normalized_type in {"json", "jsonb"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return _canonical_json(parsed)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return canonical_value(value, "numeric")
    if isinstance(value, float):
        return canonical_value(value, "float8")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return str(value) if not isinstance(value, str) else value


_canonical_value = canonical_value


def _canonical_json(value: object) -> object:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(key): _canonical_json(item) for key, item in ordered}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return canonical_value(value, None)


def _row_payload(row: Mapping[str, object], columns: Sequence[ColumnSpec]) -> bytes:
    values = [canonical_value(row[column.name], column.target_type) for column in columns]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pk_payload(
    row: Mapping[str, object], columns: Mapping[str, ColumnSpec], primary_key: Sequence[str]
) -> bytes:
    values = [canonical_value(row[name], columns[name].target_type) for name in primary_key]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _iter_sqlite_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[dict[str, object]]:
    names = ", ".join(_quote_sqlite_identifier(column) for column in columns)
    cursor = conn.execute(f"SELECT {names} FROM {_quote_sqlite_identifier(table)}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        for row in rows:
            yield {key: row[key] for key in row.keys()}


def _iter_pg_rows(
    conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[dict[str, object]]:
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.Identifier(table),
    )
    cursor_name = f"t07_reconcile_{uuid4().hex}"
    with conn.cursor(name=cursor_name, row_factory=dict_row) as cursor:
        cursor.itersize = batch_size
        cursor.execute(query)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield dict(row)


def compute_table_digest(
    conn: sqlite3.Connection | psycopg.Connection[Any],
    table: str,
    columns: Sequence[ColumnSpec],
    primary_key: Sequence[str],
    *,
    dialect: Dialect | None = None,
    batch_size: int = DEFAULT_DIGEST_BATCH_SIZE,
) -> TableDigest:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    selected_dialect: Dialect = (
        dialect
        if dialect is not None
        else ("sqlite" if isinstance(conn, sqlite3.Connection) else "postgresql")
    )
    names = [column.name for column in columns]
    by_name = {column.name: column for column in columns}
    missing_pk = set(primary_key) - set(by_name)
    if missing_pk:
        raise ValueError(f"primary-key columns absent from digest specification: {len(missing_pk)}")

    rows: Iterable[dict[str, object]]
    if selected_dialect == "sqlite":
        assert isinstance(conn, sqlite3.Connection)
        rows = _iter_sqlite_rows(conn, table, names, batch_size)
    else:
        rows = _iter_pg_rows(conn, table, names, batch_size)  # type: ignore[arg-type]

    row_digest = _UnorderedDigest()
    pk_digest = _UnorderedDigest() if primary_key else None
    for row in rows:
        row_digest.update(_row_payload(row, columns))
        if pk_digest is not None:
            pk_digest.update(_pk_payload(row, by_name, primary_key))
    return TableDigest(
        row_count=row_digest.count,
        primary_key_sha256=(
            None if pk_digest is None else pk_digest.hexdigest(b"t07-primary-key-v1")
        ),
        row_sha256=row_digest.hexdigest(b"t07-canonical-row-v1"),
    )


def _table_reconciliation(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    table: str,
) -> tuple[TableReconciliation | None, list[ReconciliationIssue]]:
    issues: list[ReconciliationIssue] = []
    source_columns, source_pk = _sqlite_columns(sqlite_conn, table)
    target_columns, target_pk, target_types = _pg_columns(pg_conn, table)
    # Exempt PG-only columns (the 041 fair-queue setting and 042 typed probe
    # timestamps): the target carries them, the T07 source never does.
    pg_only = PG_ONLY_COLUMNS.get(table, frozenset())
    if set(source_columns) != set(target_columns) - pg_only:
        issues.append(
            ReconciliationIssue(
                code="table_column_mismatch",
                scope=table,
                detail=(
                    f"column sets differ: source={len(source_columns)} target={len(target_columns)}"
                ),
            )
        )
        return None, issues
    if source_pk != target_pk:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_contract_mismatch",
                scope=table,
                detail="source and target primary-key column order differs",
            )
        )
        return None, issues

    specs = tuple(ColumnSpec(name, target_types[name]) for name in source_columns)
    source = compute_table_digest(
        sqlite_conn,
        table,
        specs,
        source_pk,
        dialect="sqlite",
    )
    target = compute_table_digest(
        pg_conn,
        table,
        specs,
        source_pk,
        dialect="postgresql",
    )
    result = TableReconciliation(
        table=table,
        source_count=source.row_count,
        target_count=target.row_count,
        source_pk_sha256=source.primary_key_sha256,
        target_pk_sha256=target.primary_key_sha256,
        source_rows_sha256=source.row_sha256,
        target_rows_sha256=target.row_sha256,
    )
    if source.row_count != target.row_count:
        issues.append(
            ReconciliationIssue(
                code="table_row_count_mismatch",
                scope=table,
                detail=f"source={source.row_count} target={target.row_count}",
            )
        )
    if source.primary_key_sha256 != target.primary_key_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_mismatch",
                scope=table,
                detail="primary-key multiset SHA-256 differs",
            )
        )
    if source.row_sha256 != target.row_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_hash_mismatch",
                scope=table,
                detail="canonical row multiset SHA-256 differs",
            )
        )
    return result, issues


def _count_rows(
    conn: sqlite3.Connection | psycopg.Connection[Any], query: str | sql.Composed
) -> int:
    """Run a COUNT query on either dialect and return the integer result.

    The billing and asset invariants are expressed as SQL aggregates and
    anti-joins so a production-sized ledger never has to be materialised in
    Python (bounded memory end to end).
    """

    if isinstance(conn, sqlite3.Connection):
        if not isinstance(query, str):
            raise TypeError("sqlite count queries must be plain SQL text")
        row: object = conn.execute(query).fetchone()
    else:
        row = conn.execute(query).fetchone()
    if row is None:
        raise RuntimeError("reconciliation count query returned no row")
    if isinstance(row, Mapping):
        value = next(iter(row.values()))
    else:
        value = row[0]  # type: ignore[index]
    return int(value)  # type: ignore[arg-type]


def pg_only_table_has_divergent_state(conn: psycopg.Connection[Any], table: str) -> bool:
    if table == "legacy_credit_policy":
        rows = conn.execute(
            "SELECT id, version, mode, numerator, denominator FROM legacy_credit_policy"
        ).fetchall()
        return len(rows) != 1 or tuple(
            _row_value(rows[0], name, i)
            for i, name in enumerate(("id", "version", "mode", "numerator", "denominator"))
        ) != (1, 0, "keep", 1, 1)
    if table == "customer_credit_pricing":
        rows = conn.execute(
            "SELECT id, version, config_json FROM customer_credit_pricing"
        ).fetchall()
        return len(rows) != 1 or (
            _row_value(rows[0], "id", 0) != 1
            or _row_value(rows[0], "version", 1) != 0
            or _row_value(rows[0], "config_json", 2) is not None
        )
    if table not in PG_ONLY_SEEDED_TABLES:
        query = sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
        return bool(_count_rows(conn, query))
    rows = conn.execute(
        """
        SELECT subject, kind, unit, resolution, unit_price_fen, updated_by_user_id
        FROM operation_cost_rates
        ORDER BY subject
        """
    ).fetchall()
    actual = tuple(
        (
            str(_row_value(row, "subject", 0)),
            str(_row_value(row, "kind", 1)),
            str(_row_value(row, "unit", 2)),
            (
                None
                if _row_value(row, "resolution", 3) is None
                else str(_row_value(row, "resolution", 3))
            ),
            int(str(_row_value(row, "unit_price_fen", 4))),
            (
                None
                if _row_value(row, "updated_by_user_id", 5) is None
                else str(_row_value(row, "updated_by_user_id", 5))
            ),
        )
        for row in rows
    )
    return actual != _OPERATION_COST_RATE_SEEDS


_WALLET_AGGREGATE_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM wallets AS w
LEFT JOIN (
    SELECT user_id,
           COALESCE(SUM(available_delta), 0) AS available_total,
           COALESCE(SUM(reserved_delta), 0) AS reserved_total
    FROM wallet_transactions
    GROUP BY user_id
) AS ledger ON ledger.user_id = w.user_id
WHERE w.available_credits != COALESCE(ledger.available_total, 0)
   OR w.reserved_credits != COALESCE(ledger.reserved_total, 0)
   OR w.available_credits < 0
   OR w.reserved_credits < 0
"""

_WALLET_MISSING_FOR_LEDGER_OWNER_QUERY = """
SELECT COUNT(*) AS bad
FROM (SELECT DISTINCT user_id FROM wallet_transactions) AS ledger
LEFT JOIN wallets AS w ON w.user_id = ledger.user_id
WHERE w.user_id IS NULL
"""


def _wallet_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    if not {"wallets", "wallet_transactions"}.issubset(tables):
        return []
    issues: list[ReconciliationIssue] = []
    mismatched = _count_rows(conn, _WALLET_AGGREGATE_MISMATCH_QUERY)
    if mismatched:
        issues.append(
            ReconciliationIssue(
                code="wallet_balance_mismatch",
                scope=f"{side}:wallets",
                detail=(
                    "wallets whose balances differ from the append-only "
                    f"transaction aggregates: {mismatched}"
                ),
            )
        )
    missing = _count_rows(conn, _WALLET_MISSING_FOR_LEDGER_OWNER_QUERY)
    if missing:
        issues.append(
            ReconciliationIssue(
                code="wallet_missing_for_ledger_owner",
                scope=f"{side}:wallets",
                detail=f"ledger owners without wallets: {missing}",
            )
        )
    return issues


_PAID_ORDER_CHARGE_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM recharge_orders AS o
WHERE o.status = 'PAID'
  AND (
    (
      SELECT COUNT(*) FROM wallet_transactions AS c
      WHERE c.type = 'CHARGE' AND c.recharge_order_id = o.id
    ) != 1
    OR NOT EXISTS (
      SELECT 1 FROM wallet_transactions AS c
      WHERE c.type = 'CHARGE'
        AND c.recharge_order_id = o.id
        AND c.user_id = o.user_id
        AND c.available_delta = o.credits
        AND c.reserved_delta = 0
    )
  )
"""

_UNPAID_ORDER_WITH_CHARGE_QUERY = """
SELECT COUNT(*) AS bad
FROM recharge_orders AS o
WHERE o.status != 'PAID'
  AND EXISTS (
    SELECT 1 FROM wallet_transactions AS c
    WHERE c.type = 'CHARGE' AND c.recharge_order_id = o.id
  )
"""

_CHARGE_WITHOUT_ORDER_QUERY = """
SELECT COUNT(*) AS bad
FROM wallet_transactions AS c
LEFT JOIN recharge_orders AS o ON o.id = c.recharge_order_id
WHERE c.type = 'CHARGE' AND (c.recharge_order_id IS NULL OR o.id IS NULL)
"""


def _paid_charge_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    if not {"recharge_orders", "wallet_transactions"}.issubset(tables):
        return []
    issues: list[ReconciliationIssue] = []
    paid_mismatched = _count_rows(conn, _PAID_ORDER_CHARGE_MISMATCH_QUERY)
    if paid_mismatched:
        issues.append(
            ReconciliationIssue(
                code="paid_order_charge_mismatch",
                scope=f"{side}:recharge_orders",
                detail=(
                    f"PAID orders without exactly one shape-matching CHARGE: {paid_mismatched}"
                ),
            )
        )
    unpaid_with_charge = _count_rows(conn, _UNPAID_ORDER_WITH_CHARGE_QUERY)
    if unpaid_with_charge:
        issues.append(
            ReconciliationIssue(
                code="unpaid_order_has_charge",
                scope=f"{side}:recharge_orders",
                detail=f"non-PAID orders with CHARGE rows: {unpaid_with_charge}",
            )
        )
    charge_without_order = _count_rows(conn, _CHARGE_WITHOUT_ORDER_QUERY)
    if charge_without_order:
        issues.append(
            ReconciliationIssue(
                code="charge_without_order",
                scope=f"{side}:wallet_transactions",
                detail=f"CHARGE rows reference missing orders: {charge_without_order}",
            )
        )
    return issues


_BILLING_ROUND_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM (
    SELECT task_id, billing_round,
           SUM(CASE WHEN type = 'RESERVE' THEN 1 ELSE 0 END) AS reserves,
           SUM(CASE WHEN type IN ('SETTLE', 'RELEASE') THEN 1 ELSE 0 END) AS terminals
    FROM wallet_transactions
    WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
    GROUP BY task_id, billing_round
) AS rounds
WHERE rounds.reserves != 1 OR rounds.terminals > 1
"""

_BILLING_OWNER_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM wallet_transactions AS wt
LEFT JOIN generation_tasks AS t ON t.id = wt.task_id
LEFT JOIN generation_batches AS b ON b.id = t.batch_id
WHERE wt.task_id IS NOT NULL AND wt.billing_round IS NOT NULL
  AND (t.id IS NULL OR b.id IS NULL
       OR b.created_by_user_id IS NULL
       OR b.created_by_user_id != wt.user_id)
"""

_BILLING_ROUND_GAP_QUERY = """
SELECT COUNT(*) AS bad
FROM (
    SELECT task_id,
           MIN(billing_round) AS first_round,
           MAX(billing_round) AS last_round,
           COUNT(DISTINCT billing_round) AS distinct_rounds
    FROM wallet_transactions
    WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
    GROUP BY task_id
) AS spans
WHERE spans.first_round != 1 OR spans.distinct_rounds != spans.last_round
"""


def _generation_billing_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    required = {"wallet_transactions", "generation_tasks", "generation_batches"}
    if not required.issubset(tables):
        return []
    issues: list[ReconciliationIssue] = []
    round_mismatched = _count_rows(conn, _BILLING_ROUND_MISMATCH_QUERY)
    if round_mismatched:
        issues.append(
            ReconciliationIssue(
                code="generation_billing_round_mismatch",
                scope=f"{side}:wallet_transactions",
                detail=(
                    "task billing rounds without exactly one RESERVE or with "
                    f"multiple terminals: {round_mismatched}"
                ),
            )
        )
    owner_mismatched = _count_rows(conn, _BILLING_OWNER_MISMATCH_QUERY)
    if owner_mismatched:
        issues.append(
            ReconciliationIssue(
                code="generation_billing_owner_mismatch",
                scope=f"{side}:wallet_transactions",
                detail=(
                    f"task billing rows not owned by the generation batch owner: {owner_mismatched}"
                ),
            )
        )
    round_gaps = _count_rows(conn, _BILLING_ROUND_GAP_QUERY)
    if round_gaps:
        issues.append(
            ReconciliationIssue(
                code="generation_billing_round_gap",
                scope=f"{side}:wallet_transactions",
                detail=f"tasks with non-contiguous billing rounds: {round_gaps}",
            )
        )
    return issues


def _json_asset_reference_issues_sqlite(
    conn: sqlite3.Connection,
    tables: Sequence[str],
    side: str,
) -> list[ReconciliationIssue]:
    """Validate ``*_asset_ids_json`` columns with in-database JSON expansion.

    The malformed-shape and orphan-reference invariants run as SQLite
    ``json_valid``/``json_type``/``json_each`` queries so neither the asset
    ID universe nor the referencing rows are ever materialised in Python.
    ``CASE`` guards keep every JSON function behind the preceding validity
    check, because evaluating ``json_type``/``json_each`` on malformed JSON
    would raise instead of counting.
    """

    issues: list[ReconciliationIssue] = []
    for table in tables:
        columns, _ = _sqlite_columns(conn, table)
        for column in (name for name in columns if name.endswith("_asset_ids_json")):
            quoted_table = _quote_sqlite_identifier(table)
            quoted_column = _quote_sqlite_identifier(column)
            invalid = _count_rows(
                conn,
                f"""
                SELECT COUNT(*) FROM {quoted_table} AS row_src
                WHERE row_src.{quoted_column} IS NOT NULL
                  AND CASE
                        WHEN json_valid(row_src.{quoted_column}) = 0 THEN 1
                        WHEN json_type(row_src.{quoted_column}) != 'array' THEN 1
                        WHEN EXISTS (
                          SELECT 1 FROM json_each(row_src.{quoted_column}) AS element
                          WHERE element.type != 'text'
                        ) THEN 1
                        ELSE 0
                      END = 1
                """,
            )
            orphan = _count_rows(
                conn,
                f"""
                SELECT COUNT(*) FROM {quoted_table} AS row_src
                WHERE row_src.{quoted_column} IS NOT NULL
                  AND CASE
                        WHEN json_valid(row_src.{quoted_column}) = 1
                         AND json_type(row_src.{quoted_column}) = 'array'
                        THEN EXISTS (
                          SELECT 1 FROM json_each(row_src.{quoted_column}) AS element
                          WHERE element.type = 'text'
                            AND element.value NOT IN (SELECT id FROM assets)
                        )
                        ELSE 0
                      END = 1
                """,
            )
            if invalid:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_json_invalid",
                        scope=f"{side}:{table}.{column}",
                        detail=f"invalid asset reference JSON rows={invalid}",
                    )
                )
            if orphan:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={orphan}",
                    )
                )
    return issues


def _json_asset_reference_issues_pg(
    conn: psycopg.Connection[Any],
    tables: Sequence[str],
    side: str,
) -> list[ReconciliationIssue]:
    """Validate ``*_asset_ids_json`` columns with in-database JSON expansion.

    The TEXT columns are validated with ``pg_input_is_valid`` (PostgreSQL 16+,
    matching the project's PG16 baseline) and expanded with
    ``jsonb_array_elements_text`` so neither the asset ID universe nor the
    referencing rows are materialised in Python. ``CASE`` guards keep every
    cast behind the preceding validity check: PostgreSQL does not guarantee
    short-circuit evaluation inside ``AND``/``OR`` chains.
    """

    issues: list[ReconciliationIssue] = []
    for table in tables:
        columns, _, _ = _pg_columns(conn, table)
        for column in (name for name in columns if name.endswith("_asset_ids_json")):
            identifiers = {"table": sql.Identifier(table), "column": sql.Identifier(column)}
            invalid = _count_rows(
                conn,
                sql.SQL(
                    """
                    SELECT COUNT(*) FROM {table} AS row_src
                    WHERE row_src.{column} IS NOT NULL
                      AND CASE
                            WHEN NOT pg_input_is_valid(row_src.{column}, 'jsonb') THEN true
                            WHEN jsonb_typeof(row_src.{column}::jsonb) IS DISTINCT FROM 'array'
                              THEN true
                            WHEN EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(row_src.{column}::jsonb) AS element
                              WHERE jsonb_typeof(element) IS DISTINCT FROM 'string'
                            ) THEN true
                            ELSE false
                          END
                    """
                ).format(**identifiers),
            )
            orphan = _count_rows(
                conn,
                sql.SQL(
                    """
                    SELECT COUNT(*) FROM {table} AS row_src
                    WHERE row_src.{column} IS NOT NULL
                      AND CASE
                            WHEN NOT pg_input_is_valid(row_src.{column}, 'jsonb') THEN false
                            WHEN jsonb_typeof(row_src.{column}::jsonb) = 'array' THEN EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements_text(row_src.{column}::jsonb) AS element
                              WHERE element.value NOT IN (SELECT id FROM assets)
                            )
                            ELSE false
                          END
                    """
                ).format(**identifiers),
            )
            if invalid:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_json_invalid",
                        scope=f"{side}:{table}.{column}",
                        detail=f"invalid asset reference JSON rows={invalid}",
                    )
                )
            if orphan:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={orphan}",
                    )
                )
    return issues


def _asset_reference_issues_sqlite(
    conn: sqlite3.Connection, side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "sqlite")
    if "assets" not in tables:
        return []
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _ = _sqlite_columns(conn, table)
        for column in columns:
            if column != "asset_id" and not column.endswith("_asset_id"):
                continue
            query = (
                f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table)} AS child "
                "LEFT JOIN assets AS parent ON "
                f"child.{_quote_sqlite_identifier(column)} = parent.id "
                f"WHERE child.{_quote_sqlite_identifier(column)} IS NOT NULL "
                "AND parent.id IS NULL"
            )
            count = int(conn.execute(query).fetchone()[0])
            if count:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    issues.extend(_json_asset_reference_issues_sqlite(conn, tables, side))
    return issues


def _asset_reference_issues_pg(
    conn: psycopg.Connection[Any], side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "postgresql")
    if "assets" not in tables:
        return []
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _, _ = _pg_columns(conn, table)
        for column in columns:
            if column != "asset_id" and not column.endswith("_asset_id"):
                continue
            query = sql.SQL(
                "SELECT COUNT(*) AS orphan_count FROM {} AS child "
                "LEFT JOIN assets AS parent ON child.{} = parent.id "
                "WHERE child.{} IS NOT NULL AND parent.id IS NULL"
            ).format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column))
            row = conn.execute(query).fetchone()
            if row is None:
                raise RuntimeError("asset reconciliation count query returned no row")
            count = int(_row_value(row, "orphan_count"))
            if count:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    issues.extend(_json_asset_reference_issues_pg(conn, tables, side))
    return issues


def validate_database_invariants(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> tuple[ReconciliationIssue, ...]:
    issues: list[ReconciliationIssue] = []
    if dialect == "sqlite":
        assert isinstance(conn, sqlite3.Connection)
        issues.extend(_asset_reference_issues_sqlite(conn, side))
    else:
        issues.extend(_asset_reference_issues_pg(conn, side))  # type: ignore[arg-type]
    issues.extend(_wallet_issues(conn, dialect, side))
    issues.extend(_paid_charge_issues(conn, dialect, side))
    issues.extend(_generation_billing_issues(conn, dialect, side))
    return tuple(issues)


def reconcile_connection_pair(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> ReconciliationReport:
    source_tables = set(_table_names(sqlite_conn, "sqlite"))
    target_tables = set(_table_names(pg_conn, "postgresql"))
    issues: list[ReconciliationIssue] = []
    missing = sorted(source_tables - target_tables)
    extra = sorted(target_tables - source_tables)
    if missing:
        issues.append(
            ReconciliationIssue(
                code="target_table_missing",
                scope="schema",
                detail=f"target is missing source tables: {len(missing)}",
            )
        )
    unexpected_extra = [table for table in extra if table not in PG_ONLY_TABLES]
    divergent_pg_only = [
        table
        for table in extra
        if table in PG_ONLY_TABLES and pg_only_table_has_divergent_state(pg_conn, table)
    ]
    if unexpected_extra or divergent_pg_only:
        issues.append(
            ReconciliationIssue(
                code="target_table_extra",
                scope="schema",
                detail=(
                    "target has tables absent from the source: "
                    f"{len(unexpected_extra) + len(divergent_pg_only)}"
                ),
            )
        )

    tables: list[TableReconciliation] = []
    for table in sorted(source_tables & target_tables):
        result, table_issues = _table_reconciliation(sqlite_conn, pg_conn, table)
        if result is not None:
            tables.append(result)
        issues.extend(table_issues)
    issues.extend(validate_database_invariants(sqlite_conn, "sqlite", "source"))
    issues.extend(validate_database_invariants(pg_conn, "postgresql", "target"))
    return ReconciliationReport(tables=tuple(tables), issues=tuple(issues))


def reconcile_databases(
    sqlite_path: str | Path,
    postgres_dsn: str,
) -> ReconciliationReport:
    with closing(connect_sqlite_readonly(sqlite_path)) as sqlite_conn:
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            return reconcile_connection_pair(sqlite_conn, pg_conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a legacy SQLite snapshot with PG")
    parser.add_argument("--sqlite", required=True, help="read-only SQLite snapshot path")
    parser.add_argument("--postgres-url", required=True, help="PostgreSQL DSN (never printed)")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    try:
        report = reconcile_databases(args.sqlite, args.postgres_url)
    except Exception as error:
        parser.exit(1, safe_error_message(error, args.postgres_url, stage="reconciliation") + "\n")
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
