"""CW-078 — the customer API-Key lane: mint, digest, authenticate, revoke.

技术方案 §2.5 B（独立泳道）：客户程序用 ``Authorization: Bearer xsk_live_...``
调充值/查询接口，不经会话围栏。本模块是这条泳道的凭据内核，与设备指纹域
（``customer_device_service``）**完全独立**——各用各的 HMAC key env，互不复用
（§2.5 C 要求 key 管理独立化，API Key 域天生自持）。

红线（§2.5 B / R-A）：

- 明文 key ``xsk_live_<8prefix>_<40 base62>`` **只在生成时返回一次**；库内只存
  ``key_prefix``（8 字符，查找定位）与 ``key_digest``（HMAC-SHA256 十六进制）。
  任何列表/认证路径都不回传明文或 digest。
- 认证：拆 prefix → 查候选行 → 用配置的 key 版本重算 digest，``hmac.compare_digest``
  恒定时间比对 → 检 ``revoked_at``。prefix 未命中或 digest 不匹配一律 ``None``
  （单一答案，无 oracle）；被吊销的 key 同样 ``None``。
- HMAC key 版本化（沿设备域先例）：轮换窗口内多个 key 版本并存，旧版本派生的
  digest 仍可认证；新行永远用最高版本。无任何配置版本是**服务端配置故障**，
  抛 ``ApiKeyError``（路由翻译成 503，绝不伪装成客户端凭据错误的 401）。

PostgreSQL 是客户泳道唯一真源，故每个入口都期望活的 PG 连接。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

# 独立的 API-Key HMAC key 域（不与设备指纹域共用 env）。
API_KEY_HMAC_KEY_ENV = "VIDEO_REPLICA_API_KEY_HMAC_KEY"
MIN_HMAC_KEY_BYTES = 32
MAX_KEY_VERSION = 64

# 明文 key 形状：xsk_live_<8 base62 prefix>_<40 base62 secret>。
API_KEY_SCHEME_PARTS = ("xsk", "live")
KEY_PREFIX_LEN = 8
KEY_SECRET_LEN = 40
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# 默认授予的白名单能力（§2.5 B：充值/查询钱包/价目表）。CW-078 只存储与回显
# Scope membership is checked at the request gate and under the transaction lock.
DEFAULT_API_KEY_SCOPES: tuple[str, ...] = ("recharge", "wallet", "pricing", "generation")


class ApiKeyError(Exception):
    """API-Key 域的密钥配置故障；路由 fail-closed 成 503（绝不成 401/500）。"""


@dataclass(frozen=True)
class GeneratedApiKey:
    """一次生成的全部产物；``plaintext`` 只此一份，落库的只有 prefix/digest。"""

    plaintext: str
    key_prefix: str
    key_digest: str
    key_version: int


@dataclass(frozen=True)
class ApiKeyRecord:
    """密钥管理视图的一行元数据——永不含 plaintext 或 key_digest。"""

    id: str
    key_prefix: str
    label: str
    scopes: tuple[str, ...]
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    token_group_id: str = ""
    credential_version: int = 1
    is_default: bool = False
    total_consumed_credits: int = 0


@dataclass(frozen=True)
class AuthenticatedApiKey:
    """一次成功认证解析出的最小 key 身份（注入 ApiKeyUser 的凭据面）。"""

    key_id: str
    user_id: str
    scopes: tuple[str, ...]


# ---------------------------------------------------------------------------
# 版本化 HMAC key（独立于设备域；沿 _env_key_candidates 先例）
# ---------------------------------------------------------------------------


def _env_key_candidates(base_env: str, key_version: int) -> list[str]:
    candidates = [f"{base_env}_V{key_version}"]
    if key_version == 1:
        candidates.append(base_env)
    return candidates


def _configured_key_versions() -> list[int]:
    return [
        version
        for version in range(1, MAX_KEY_VERSION + 1)
        if any(
            os.environ.get(name, "").strip()
            for name in _env_key_candidates(API_KEY_HMAC_KEY_ENV, version)
        )
    ]


def _api_key_hmac_key(key_version: int) -> bytes:
    for name in _env_key_candidates(API_KEY_HMAC_KEY_ENV, key_version):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        raw = value.encode("utf-8")
        if len(raw) < MIN_HMAC_KEY_BYTES:
            raise ApiKeyError(f"{name} must be at least {MIN_HMAC_KEY_BYTES} bytes")
        return raw
    raise ApiKeyError(f"{API_KEY_HMAC_KEY_ENV} for key version {key_version} is not configured")


def highest_api_key_hmac_key() -> tuple[int, bytes]:
    """最高已配置 key 版本及其原始字节——新 key 永远用它派生 digest。"""
    configured = _configured_key_versions()
    if not configured:
        raise ApiKeyError(f"no {API_KEY_HMAC_KEY_ENV} key version is configured")
    version = max(configured)
    return version, _api_key_hmac_key(version)


def _keyed_digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def api_key_digests(plaintext: str) -> list[str]:
    """明文 key 在每个已配置版本下的 digest——认证须逐一探测（轮换窗口）。

    无任何配置版本是服务端配置故障，抛 ``ApiKeyError`` 而不是静默把每个 key
    判成无效（那会把运维问题伪装成客户端凭据问题）。
    """
    versions = _configured_key_versions()
    if not versions:
        raise ApiKeyError(f"no {API_KEY_HMAC_KEY_ENV} key version is configured")
    return [_keyed_digest(_api_key_hmac_key(version), plaintext) for version in versions]


# ---------------------------------------------------------------------------
# 明文 key 形状
# ---------------------------------------------------------------------------


def _random_base62(length: int) -> str:
    return "".join(secrets.choice(_BASE62) for _ in range(length))


def generate_api_key() -> GeneratedApiKey:
    """铸造一枚新 key：明文只此一份，digest 用最高 key 版本派生。"""
    prefix = _random_base62(KEY_PREFIX_LEN)
    secret = _random_base62(KEY_SECRET_LEN)
    plaintext = f"{'_'.join(API_KEY_SCHEME_PARTS)}_{prefix}_{secret}"
    version, key = highest_api_key_hmac_key()
    return GeneratedApiKey(
        plaintext=plaintext,
        key_prefix=prefix,
        key_digest=_keyed_digest(key, plaintext),
        key_version=version,
    )


def parse_api_key_prefix(plaintext: str) -> str | None:
    """从明文 key 拆出 8 字符 prefix；形状不合返回 ``None``（无 oracle）。"""
    parts = plaintext.split("_")
    if len(parts) != 4:
        return None
    if (parts[0], parts[1]) != API_KEY_SCHEME_PARTS:
        return None
    prefix, secret = parts[2], parts[3]
    if len(prefix) != KEY_PREFIX_LEN or len(secret) != KEY_SECRET_LEN:
        return None
    return prefix


# ---------------------------------------------------------------------------
# 持久化：create / list / revoke
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_scopes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def create_api_key(
    conn: psycopg.Connection,
    *,
    user_id: str,
    label: str = "",
    scopes: tuple[str, ...] | None = None,
    token_group_id: str | None = None,
    credential_version: int = 1,
    is_default: bool = False,
) -> tuple[str, ApiKeyRecord]:
    """为一个用户铸造并落库一枚 key；返回 ``(plaintext, record)``。

    ``plaintext`` 只在此处出现一次——调用方（路由）负责把它一次性回显给客户，
    库内只有 prefix + digest。scopes 默认授予白名单能力集。
    """
    generated = generate_api_key()
    key_id = str(uuid.uuid4())
    effective_scopes = DEFAULT_API_KEY_SCOPES if scopes is None else tuple(scopes)
    created_at = _now_iso()
    group_id = token_group_id or key_id
    conn.execute(
        "INSERT INTO customer_api_keys "
        "(id, user_id, key_prefix, key_digest, key_version, scopes, label, created_at, "
        "token_group_id, credential_version, is_default) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            key_id,
            user_id,
            generated.key_prefix,
            generated.key_digest,
            generated.key_version,
            json.dumps(list(effective_scopes), ensure_ascii=True, separators=(",", ":")),
            label,
            created_at,
            group_id,
            credential_version,
            int(is_default),
        ),
    )
    record = ApiKeyRecord(
        id=key_id,
        key_prefix=generated.key_prefix,
        label=label,
        scopes=effective_scopes,
        created_at=created_at,
        last_used_at=None,
        revoked_at=None,
        token_group_id=group_id,
        credential_version=credential_version,
        is_default=is_default,
    )
    return generated.plaintext, record


def list_api_keys(conn: psycopg.Connection, *, user_id: str) -> list[ApiKeyRecord]:
    """Latest credential in each logical group, including fully revoked groups."""
    rows = conn.execute(
        "SELECT k.id, k.key_prefix, k.label, k.scopes, k.created_at, "
        "(SELECT max(v.last_used_at) FROM customer_api_keys v "
        "WHERE v.token_group_id = k.token_group_id), "
        "k.revoked_at, k.token_group_id, k.credential_version, k.is_default, "
        "(SELECT COALESCE(SUM(-wt.reserved_delta), 0) FROM wallet_transactions wt "
        "JOIN customer_api_keys v ON v.id = wt.api_key_id "
        "WHERE v.token_group_id = k.token_group_id "
        "AND wt.user_id = k.user_id AND wt.type = 'SETTLE') "
        "FROM (SELECT DISTINCT ON (token_group_id) * FROM customer_api_keys "
        "WHERE user_id = %s ORDER BY token_group_id, credential_version DESC) k "
        "ORDER BY k.created_at DESC, k.id DESC",
        (user_id,),
    ).fetchall()
    return [
        ApiKeyRecord(
            id=str(r[0]),
            key_prefix=str(r[1]),
            label=str(r[2]),
            scopes=_parse_scopes(r[3]),
            created_at=str(r[4]),
            last_used_at=None if r[5] is None else str(r[5]),
            revoked_at=None if r[6] is None else str(r[6]),
            token_group_id=str(r[7]),
            credential_version=int(r[8]),
            is_default=bool(r[9]),
            total_consumed_credits=int(r[10]),
        )
        for r in rows
    ]


# revoke_api_key 的服务级结果（路由翻译成 HTTP）：吊销成功 / 无此 key 或属他人。
REVOKE_OUTCOME_REVOKED = "revoked"
REVOKE_OUTCOME_NOT_FOUND = "not_found"
REVOKE_OUTCOME_ALREADY_REVOKED = "already_revoked"


def revoke_all_api_keys(conn: psycopg.Connection, *, user_id: str) -> int:
    """吊销调用方名下**全部**未吊销的 key，返回真正被吊销的条数。

    「账号可能已泄漏」的一键自救：逐枚吊销需要 N 次请求，中途失败会留下仍然
    有效的 key；这里一次 UPDATE 完成，且天然幂等（第二次返回 0）。行一律保留
    （``revoked_at`` 置时刻），账务与审计都靠历史行说话，不硬删。
    """
    rows = conn.execute(
        "UPDATE customer_api_keys SET revoked_at = %s "
        "WHERE user_id = %s AND revoked_at IS NULL "
        "RETURNING id",
        (_now_iso(), user_id),
    ).fetchall()
    return len(rows)


def revoke_api_key(conn: psycopg.Connection, *, user_id: str, key_id: str) -> str:
    """软吊销一枚 key（``revoked_at`` 置时刻，行保留作审计，不硬删）。

    只作用于调用方**自己**的 key：缺失或属他人一律 ``not_found``（单一答案，
    无 IDOR oracle）；已吊销的幂等返回 ``already_revoked``。
    """
    row = conn.execute(
        "SELECT revoked_at FROM customer_api_keys WHERE id = %s AND user_id = %s",
        (key_id, user_id),
    ).fetchone()
    if row is None:
        return REVOKE_OUTCOME_NOT_FOUND
    if row[0] is not None:
        return REVOKE_OUTCOME_ALREADY_REVOKED
    conn.execute(
        "UPDATE customer_api_keys SET revoked_at = %s WHERE id = %s AND user_id = %s",
        (_now_iso(), key_id, user_id),
    )
    return REVOKE_OUTCOME_REVOKED


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


def authenticate_api_key(conn: psycopg.Connection, plaintext: str) -> AuthenticatedApiKey | None:
    """把一枚明文 key 解析成 ``AuthenticatedApiKey``，或 ``None``。

    单一失败答案（``None``）覆盖：形状不合、prefix 未命中、digest 不匹配、已吊销
    ——无 oracle。digest 用 ``hmac.compare_digest`` 恒定时间比对（bearer 秘密的
    标准做法，优于 DB 侧比较）。本函数**只读**；``last_used_at`` 的回写由业务写
    事务（``write_for_api_key``）调用 ``touch_last_used`` 完成。
    """
    prefix = parse_api_key_prefix(plaintext)
    if prefix is None:
        return None
    row = conn.execute(
        "SELECT k.id, k.user_id, k.key_digest, k.scopes, k.revoked_at "
        "FROM customer_api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_prefix = %s AND u.is_active = 1 AND u.role = 'customer'",
        (prefix,),
    ).fetchone()
    if row is None:
        return None
    if row[4] is not None:
        # 已吊销：失效，但仍是「无效凭据」单一答案。
        return None
    stored_digest = str(row[2])
    matched = any(
        hmac.compare_digest(stored_digest, candidate) for candidate in api_key_digests(plaintext)
    )
    if not matched:
        return None
    return AuthenticatedApiKey(
        key_id=str(row[0]),
        user_id=str(row[1]),
        scopes=_parse_scopes(row[3]),
    )


def touch_last_used(conn: psycopg.Connection, *, key_id: str, when: str | None = None) -> None:
    """在业务写事务内回写 ``last_used_at``（认证成功的一次副作用）。"""
    conn.execute(
        "UPDATE customer_api_keys SET last_used_at = %s WHERE id = %s",
        (when or _now_iso(), key_id),
    )
