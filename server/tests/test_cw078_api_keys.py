"""CW-078 — API Key 独立认证泳道（技术方案 §2.5 B / §3.1 line 224 / §5 line 336）.

四层覆盖，对应 claim 的「migration + service + auth + routes」：

- **A 组纯单元（无 PG）**：``generate_api_key`` 形状/prefix/HMAC-SHA256 digest、
  ``parse_api_key_prefix`` 单一失败答案、版本化 key 域（最高版本铸钥、轮换窗口多版本
  认证）、``MIN_HMAC_KEY_BYTES`` 强制、无配置 key 抛 ``ApiKeyError``（服务端故障而非
  静默无效）、默认 scopes、``apikey:ip``/``apikey:key`` 限速维度注册。
- **B 组 service 持久化（共享库）**：迁移 089 建 ``customer_api_keys`` 表 + 唯一 prefix
  索引 + 扩 ``ck_security_auth_failures_dimension``；create/list/revoke/authenticate/
  touch_last_used 落库；scopes 存 TEXT-JSON（维持 head ``jsonb_columns=0``）；软吊销
  幂等 + 用户隔离（无 IDOR oracle）；明文/digest 永不外泄。
- **C 组认证泳道（customer_fence）**：``resolve_api_key_user`` 非 key bearer → None
  （落回会话/内部泳道）、customer DB 未配置时 xsk_live_ → 401（内部泳道无此表）、
  ``write_for_api_key`` 独立泳道（绝不调 ``fenced_pg_transaction``）+ last_used 回写。
- **D 组路由（TestClient）**：DTO 明文一次性契约（R-A：结构上无 ``key_digest`` /
  ``cost_price_fen``，``extra=forbid``）；管理路由会话围栏 401 / 超长 label 422；
  管理 happy-path（stub 掉已在他处充分测试的围栏，跑真实 PG）；白名单
  ``GET /api/customer/recharge-orders`` 经 API-Key 泳道 200 + last_used 回写、无凭据
  401、无效 key 401、失败预算耗尽 429 + ``Retry-After``。

PG 约束（沿 CW-031，claim: dedicated_database=无、pg_test_kit 不 owned）：不建任何新
PG 库，只用共享库 ``customer_v3_test``；模块级持 ``shared_suite_lock``；全部数据用唯一
``cw078-`` 前缀；**绝不 TRUNCATE 共享业务表**，收尾只 ``DELETE`` cw078- 行 + 复位
CW-078 独占的 ``apikey:*`` 限速桶；全部 PG 用例挂 CW-007 硬门（fixture 不可达即 fail，
不 skip）。API Key 明文只在测试内由 ``secrets`` 生成即弃（no_production_secrets）；HMAC
key 用临时 32+ 字节测试值，非生产密钥。PG 套件 CI-Linux-only（pg_test_kit 模块级
``import fcntl``，POSIX-only）；A 组纯函数已用一次性探针在本地验证（34/34 PASS）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pg_test_kit import (
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)
from pydantic import ValidationError

from app.api_key_routes import (
    API_KEY_LABEL_MAX,
    ApiKeyListResponse,
    ApiKeyRecordResponse,
    CreatedApiKeyResponse,
)
from app.api_key_routes import router as api_key_router
from app.api_key_service import (
    API_KEY_HMAC_KEY_ENV,
    DEFAULT_API_KEY_SCOPES,
    MIN_HMAC_KEY_BYTES,
    REVOKE_OUTCOME_ALREADY_REVOKED,
    REVOKE_OUTCOME_NOT_FOUND,
    REVOKE_OUTCOME_REVOKED,
    ApiKeyError,
    ApiKeyRecord,
    _env_key_candidates,
    api_key_digests,
    authenticate_api_key,
    create_api_key,
    generate_api_key,
    highest_api_key_hmac_key,
    list_api_keys,
    parse_api_key_prefix,
    revoke_api_key,
    touch_last_used,
)
from app.customer_fence import (
    API_KEY_BEARER_PREFIX,
    RETRY_AFTER_HEADER,
    ApiKeyUser,
    BusinessDb,
    CustomerSessionSnapshot,
    resolve_api_key_user,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.recharge_routes import router as recharge_router
from app.security_rate_limit import (
    AUDIT_DIMENSIONS,
    DIMENSION_APIKEY_IP,
    DIMENSION_APIKEY_KEY,
    RATE_LIMIT_APIKEY_IP_ENV,
    RATE_LIMIT_APIKEY_KEY_ENV,
    RATE_LIMIT_DIMENSIONS,
    apikey_ip_limit,
    apikey_key_limit,
)

# ---------------------------------------------------------------------------
# 常量与临时密钥（全部测试内生成即弃，绝无生产密钥）
# ---------------------------------------------------------------------------

DB_NAME_SHARED = "customer_v3_test"
CW078 = "cw078-"
# 32+ 字节的临时 HMAC key（token_urlsafe(48) → 64 字符）；每次运行随机，永不落生产。
TEST_HMAC_KEY = secrets.token_urlsafe(48)
TEST_HMAC_KEY_V2 = secrets.token_urlsafe(48)
SHORT_HMAC_KEY = "cw078-too-short"  # 15 字节 < MIN_HMAC_KEY_BYTES

API_KEYS_PATH = "/api/customer/api-keys"
RECHARGE_LIST_PATH = "/api/customer/recharge-orders"

# 明文 key 形状：xsk_live_<8 base62>_<40 base62>。
_PLAINTEXT_RE = re.compile(r"xsk_live_[0-9A-Za-z]{8}_[0-9A-Za-z]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _well_shaped_unknown_key() -> str:
    """一枚形状合法但库里没有的 key（prefix 未命中 → 单一失败答案）。"""
    return API_KEY_BEARER_PREFIX + "Z" * 8 + "_" + "z" * 40


def _request(bearer: str | None) -> Request:
    """最小 starlette Request：只够 _bearer_token 解析 Authorization 头。

    resolve_api_key_user 的 None 落回分支与「customer DB 未配置」401 分支都在触达
    ops_metrics / client_ip / PG 池之前返回，故无需完整 scope。
    """
    headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


# ---------------------------------------------------------------------------
# 共享库 fixture + cw078- 前缀种子/清理（绝不新建 PG 库，绝不 TRUNCATE）
# ---------------------------------------------------------------------------


def _clean_cw078(dsn: str) -> None:
    """删除全部 cw078- 作用域行 + 复位 CW-078 独占的 apikey:* 限速桶。

    子表先于父表删除以满足外键；只 DELETE、绝不 TRUNCATE 共享业务表（CW-031 红线）。
    ``apikey:ip``/``apikey:key`` 限速桶是 CW-078 独占维度，复位它可让 429 预算用例
    不会把耗尽的预算泄漏给兄弟用例。append-only 的 ``security_auth_failures`` 审计行
    不删（触发器保护，且不影响预算判定——预算在 counters 表）。
    """
    like = CW078 + "%"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM customer_api_keys WHERE user_id LIKE %s", (like,))
        conn.execute("DELETE FROM audit_logs WHERE actor_user_id LIKE %s", (like,))
        conn.execute("DELETE FROM customer_devices WHERE user_id LIKE %s", (like,))
        conn.execute(
            "DELETE FROM activation_codes WHERE id LIKE %s OR bound_user_id LIKE %s",
            (like, like),
        )
        conn.execute("DELETE FROM activation_code_batches WHERE id LIKE %s", (like,))
        conn.execute("DELETE FROM recharge_orders WHERE user_id LIKE %s", (like,))
        conn.execute("DELETE FROM wallets WHERE user_id LIKE %s", (like,))
        conn.execute("DELETE FROM users WHERE id LIKE %s", (like,))
        conn.execute(
            # counters 表按 032 设计只有复合 bucket_key（"{dimension}|{identifier}"），
            # 无独立 dimension 列——按前缀匹配清 apikey:* 桶。
            "DELETE FROM security_rate_limit_counters "
            "WHERE bucket_key LIKE %s OR bucket_key LIKE %s",
            (f"{DIMENSION_APIKEY_IP}|%", f"{DIMENSION_APIKEY_KEY}|%"),
        )


def _seed_customer(dsn: str, user_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, 'customer') ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, f"CW078 {user_id}"),
        )


def _seed_key(dsn: str, user_id: str, *, label: str = "") -> tuple[str, ApiKeyRecord]:
    """为 user 铸一枚真实 key，返回 ``(plaintext, record)``（明文只此一份）。"""
    with psycopg.connect(dsn) as conn:
        return create_api_key(conn, user_id=user_id, label=label)


def _new_user(tag: str) -> str:
    return f"{CW078}{tag}-{uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + CW-007 硬门 + 迁移到 head + 模块级套件锁（沿 CW-031）。"""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert dsn.rsplit("/", 1)[1] == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_cw078(dsn)
        try:
            yield dsn
        finally:
            _clean_cw078(dsn)
            close_pg_pool()


@pytest.fixture()
def pg_env(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """把 app 连接池绑到共享测试库 + 配置 API-Key HMAC key（单版本）。"""
    monkeypatch.setenv(DATABASE_URL_ENV, pg_dsn)
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    monkeypatch.delenv(API_KEY_HMAC_KEY_ENV + "_V2", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    close_pg_pool()
    yield pg_dsn
    close_pg_pool()


def _build_client() -> TestClient:
    """只挂本任务相关路由的子集 app（沿 CW-031，避免 main.app 的 bootstrap 副作用）。"""
    application = FastAPI()
    application.include_router(recharge_router)
    application.include_router(api_key_router)
    return TestClient(application)


@pytest.fixture()
def client(pg_env: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """PG 客户泳道 TestClient（DATABASE_URL_ENV 指向共享库、HMAC key 已配）。"""
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY", secrets.token_urlsafe(48))
    with _build_client() as test_client:
        test_client.headers["Idempotency-Key"] = str(uuid4())
        yield test_client


@pytest.fixture()
def bare_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """无 PG 运行时的 TestClient：customer DB 未配置 → 会话快照 None（管理路由 401）。"""
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with _build_client() as test_client:
        yield test_client


@contextmanager
def _stub_fenced(
    snapshot: CustomerSessionSnapshot,
    *,
    isolation: object = None,
    record_write_evidence: bool = False,
) -> Iterator[tuple[psycopg.Connection, SimpleNamespace]]:
    """替身围栏：开真实 pg_transaction，但跳过已在他处充分测试的会话再校验。

    管理路由（api_key_routes）本任务的新逻辑是 label 校验 / create-list-revoke 编排 /
    明文一次性 DTO / 审计落库 / HTTP 状态映射；会话围栏（customer_session_snapshot +
    fenced_pg_transaction + verify_session_context）归 test_customer_fencing /
    test_customer_sessions 覆盖，这里 stub 掉以隔离被测单元。
    """
    del isolation, record_write_evidence  # 替身不需要这些旋钮
    with pg_transaction() as conn:
        yield conn, SimpleNamespace(user_id=snapshot.expected_user_id)


@pytest.fixture()
def stub_session(pg_env: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """把 api_key_routes 的会话快照/围栏替身成固定 cw078- 客户，返回其 user_id。"""
    user_id = _new_user("mgmt")
    _seed_customer(pg_env, user_id)
    snapshot = CustomerSessionSnapshot(
        token="cw078-stub-session-token",
        expected_user_id=user_id,
        expected_device_id=f"{CW078}stub-device",
        expected_session_id=f"{CW078}stub-session",
        expected_session_epoch=0,
        expected_lease_until="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr("app.api_key_routes.customer_session_snapshot", lambda request: snapshot)
    monkeypatch.setattr("app.api_key_routes.fenced_pg_transaction", _stub_fenced)
    return user_id


# ===========================================================================
# A 组：纯单元（无 PG）——凭据内核的密码学 / 版本化 / 限速维度契约
# ===========================================================================


def test_generate_api_key_shape_prefix_and_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """明文形状 xsk_live_<8>_<40>、prefix 8 字符、单版本 version=1、digest=HMAC-SHA256。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    generated = generate_api_key()

    assert _PLAINTEXT_RE.fullmatch(generated.plaintext) is not None
    assert len(generated.key_prefix) == 8
    assert generated.key_version == 1
    expected = hmac.new(
        TEST_HMAC_KEY.encode("utf-8"), generated.plaintext.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert generated.key_digest == expected
    assert _DIGEST_RE.fullmatch(generated.key_digest) is not None


def test_generated_keys_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    """两枚 key 的明文/prefix/digest 互不相同（secrets 随机、prefix 永不复用）。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    a, b = generate_api_key(), generate_api_key()

    assert a.plaintext != b.plaintext
    assert a.key_prefix != b.key_prefix
    assert a.key_digest != b.key_digest


def test_parse_api_key_prefix_valid_and_single_failure_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法 → prefix；错误 scheme / 段数 / 长度 / 空串一律 None（无 oracle）。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    generated = generate_api_key()

    assert parse_api_key_prefix(generated.plaintext) == generated.key_prefix
    assert parse_api_key_prefix("xsk_test_" + "a" * 8 + "_" + "b" * 40) is None
    assert parse_api_key_prefix("xsk_live_" + "a" * 8) is None  # 3 段
    assert parse_api_key_prefix(generated.plaintext + "_extra") is None  # 5 段
    assert parse_api_key_prefix("xsk_live_" + "a" * 7 + "_" + "b" * 40) is None  # prefix 短
    assert parse_api_key_prefix("xsk_live_" + "a" * 8 + "_" + "b" * 39) is None  # secret 短
    assert parse_api_key_prefix("") is None


def test_versioned_key_domain_mints_with_highest_and_authenticates_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮换窗口：新钥用最高版本派生；api_key_digests 覆盖每个已配置版本。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV + "_V2", TEST_HMAC_KEY_V2)

    version, key_bytes = highest_api_key_hmac_key()
    assert version == 2
    assert key_bytes == TEST_HMAC_KEY_V2.encode("utf-8")

    generated = generate_api_key()
    assert generated.key_version == 2
    assert (
        generated.key_digest
        == hmac.new(
            TEST_HMAC_KEY_V2.encode("utf-8"), generated.plaintext.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    digests = api_key_digests(generated.plaintext)
    assert len(digests) == 2  # V1 + V2 并存认证
    assert generated.key_digest in digests


def test_env_key_candidates_version_shape() -> None:
    """v1 → [base_V1, base]（向后兼容裸 env）；vN>1 → [base_VN]。"""
    assert _env_key_candidates("ENVX", 1) == ["ENVX_V1", "ENVX"]
    assert _env_key_candidates("ENVX", 2) == ["ENVX_V2"]


def test_short_hmac_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """短于 MIN_HMAC_KEY_BYTES 的 key 是配置故障 → ApiKeyError（不静默铸钥）。"""
    assert len(SHORT_HMAC_KEY.encode("utf-8")) < MIN_HMAC_KEY_BYTES
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, SHORT_HMAC_KEY)

    with pytest.raises(ApiKeyError):
        generate_api_key()


def test_missing_hmac_key_is_a_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """无任何配置版本 → ApiKeyError（路由翻译成 503，绝不伪装成客户端 401）。"""
    monkeypatch.delenv(API_KEY_HMAC_KEY_ENV, raising=False)
    monkeypatch.delenv(API_KEY_HMAC_KEY_ENV + "_V1", raising=False)

    with pytest.raises(ApiKeyError):
        api_key_digests(_well_shaped_unknown_key())
    with pytest.raises(ApiKeyError):
        highest_api_key_hmac_key()


def test_default_scopes_are_the_whitelist_capabilities() -> None:
    """默认授予 §2.5 B 白名单能力：充值 / 钱包 / 价目表。"""
    assert DEFAULT_API_KEY_SCOPES == ("recharge", "wallet", "pricing", "generation")


def test_apikey_rate_limit_dimensions_are_registered() -> None:
    """apikey:ip / apikey:key 同时进限速预算集与审计维度集（089 扩 CHECK）。"""
    assert DIMENSION_APIKEY_IP == "apikey:ip"
    assert DIMENSION_APIKEY_KEY == "apikey:key"
    assert DIMENSION_APIKEY_IP in RATE_LIMIT_DIMENSIONS
    assert DIMENSION_APIKEY_KEY in RATE_LIMIT_DIMENSIONS
    assert DIMENSION_APIKEY_IP in AUDIT_DIMENSIONS
    assert DIMENSION_APIKEY_KEY in AUDIT_DIMENSIONS


def test_apikey_limits_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认预算 20(ip)/10(key)；env 覆盖生效（正整数）。"""
    monkeypatch.delenv(RATE_LIMIT_APIKEY_IP_ENV, raising=False)
    monkeypatch.delenv(RATE_LIMIT_APIKEY_KEY_ENV, raising=False)
    assert apikey_ip_limit() == 20
    assert apikey_key_limit() == 10

    monkeypatch.setenv(RATE_LIMIT_APIKEY_IP_ENV, "3")
    monkeypatch.setenv(RATE_LIMIT_APIKEY_KEY_ENV, "2")
    assert apikey_ip_limit() == 3
    assert apikey_key_limit() == 2


# ===========================================================================
# B 组：service 持久化（共享库 + cw078- 前缀）——迁移 089 + CRUD + 认证
# ===========================================================================


def test_migration_089_creates_table_columns_and_indexes(pg_dsn: str) -> None:
    """迁移到 head 后：customer_api_keys 列集齐、scopes 为 TEXT（jsonb=0）、唯一 prefix 索引在。"""
    with psycopg.connect(pg_dsn) as conn:
        columns = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'customer_api_keys'"
            ).fetchall()
        }
        prefix_index = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'customer_api_keys' "
            "AND indexname = 'uq_customer_api_keys_prefix'"
        ).fetchone()
        user_index = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'customer_api_keys' "
            "AND indexname = 'idx_customer_api_keys_user_created'"
        ).fetchone()

    assert set(columns) == {
        "id",
        "user_id",
        "key_prefix",
        "key_digest",
        "key_version",
        "scopes",
        "label",
        "created_at",
        "last_used_at",
        "revoked_at",
        "token_group_id",
        "credential_version",
        "is_default",
    }
    # R-A / cw056 §617：JSON 全存 TEXT，维持 head jsonb_columns=0 不变量。
    assert columns["scopes"] == "text"
    assert columns["key_digest"] == "text"
    assert prefix_index is not None and "UNIQUE" in str(prefix_index[0]).upper()
    assert user_index is not None


def test_migration_089_extends_auth_failure_dimension_check(pg_dsn: str) -> None:
    """ck_security_auth_failures_dimension 现允许 apikey:ip / apikey:key（承载 key 尝试限速）。"""
    with psycopg.connect(pg_dsn) as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_security_auth_failures_dimension'"
        ).fetchone()

    assert definition is not None
    assert "apikey:ip" in str(definition[0])
    assert "apikey:key" in str(definition[0])


def test_create_and_list_roundtrip_keeps_secret_server_side(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create 落库 prefix+digest（非明文）、scopes TEXT-JSON；list 只回元数据。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    user_id = _new_user("svc")
    _seed_customer(pg_dsn, user_id)

    with psycopg.connect(pg_dsn) as conn:
        plaintext, record = create_api_key(conn, user_id=user_id, label="probe-label")
        row = conn.execute(
            "SELECT key_digest, key_prefix, scopes FROM customer_api_keys WHERE id = %s",
            (record.id,),
        ).fetchone()
        listed = list_api_keys(conn, user_id=user_id)

    assert isinstance(record, ApiKeyRecord)
    assert record.scopes == DEFAULT_API_KEY_SCOPES
    assert record.label == "probe-label"
    assert record.last_used_at is None and record.revoked_at is None
    assert row is not None
    # 库里存的是 digest，绝不是明文；prefix 与 record 一致。
    assert str(row[0]) != plaintext
    assert _DIGEST_RE.fullmatch(str(row[0])) is not None
    assert str(row[1]) == record.key_prefix
    assert json.loads(str(row[2])) == list(DEFAULT_API_KEY_SCOPES)  # TEXT-JSON 数组
    assert [item.id for item in listed] == [record.id]
    # ApiKeyRecord 结构上不含明文/digest 字段（永不外泄）。
    assert not hasattr(listed[0], "plaintext")
    assert not hasattr(listed[0], "key_digest")


def test_authenticate_success_and_single_failure_answer(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正确 key → AuthenticatedApiKey；错 secret / 未命中 prefix / 形状不合一律 None。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    user_id = _new_user("auth")
    _seed_customer(pg_dsn, user_id)

    with psycopg.connect(pg_dsn) as conn:
        plaintext, record = create_api_key(conn, user_id=user_id)
        authed = authenticate_api_key(conn, plaintext)
        # 同 prefix、改末位 secret → digest 失配 → None（无 oracle）。
        tampered = plaintext[:-1] + ("A" if plaintext[-1] != "A" else "B")
        tampered_result = authenticate_api_key(conn, tampered)
        unknown_result = authenticate_api_key(conn, generate_api_key().plaintext)
        malformed_result = authenticate_api_key(conn, "xsk_live_short")

    assert authed is not None
    assert authed.key_id == record.id
    assert authed.user_id == user_id
    assert authed.scopes == DEFAULT_API_KEY_SCOPES
    assert tampered_result is None
    assert unknown_result is None
    assert malformed_result is None


def test_revoke_is_soft_scoped_and_idempotent(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """软吊销保留行；吊销后认证 None；重复吊销幂等；他人/缺失 → not_found（无 IDOR）。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    owner, other = _new_user("owner"), _new_user("other")
    _seed_customer(pg_dsn, owner)
    _seed_customer(pg_dsn, other)

    with psycopg.connect(pg_dsn) as conn:
        plaintext, record = create_api_key(conn, user_id=owner)
        first = revoke_api_key(conn, user_id=owner, key_id=record.id)
        after_revoke_auth = authenticate_api_key(conn, plaintext)
        second = revoke_api_key(conn, user_id=owner, key_id=record.id)
        by_other = revoke_api_key(conn, user_id=other, key_id=record.id)
        missing = revoke_api_key(conn, user_id=owner, key_id=f"{CW078}nope")
        surviving = conn.execute(
            "SELECT revoked_at FROM customer_api_keys WHERE id = %s", (record.id,)
        ).fetchone()

    assert first == REVOKE_OUTCOME_REVOKED
    assert after_revoke_auth is None
    assert second == REVOKE_OUTCOME_ALREADY_REVOKED
    assert by_other == REVOKE_OUTCOME_NOT_FOUND
    assert missing == REVOKE_OUTCOME_NOT_FOUND
    assert surviving is not None and surviving[0] is not None  # 行保留作审计


def test_touch_last_used_writeback(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """touch_last_used 在业务事务内回写 last_used_at（认证成功的一次副作用）。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    user_id = _new_user("touch")
    _seed_customer(pg_dsn, user_id)
    stamp = "2026-09-12T00:00:00+00:00"

    with psycopg.connect(pg_dsn) as conn:
        _, record = create_api_key(conn, user_id=user_id)
        assert record.last_used_at is None
        touch_last_used(conn, key_id=record.id, when=stamp)
        row = conn.execute(
            "SELECT last_used_at FROM customer_api_keys WHERE id = %s", (record.id,)
        ).fetchone()

    assert row is not None and str(row[0]) == stamp


def test_unique_prefix_index_rejects_a_second_row(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uq_customer_api_keys_prefix 全局唯一：重复 prefix 落库被拒（前缀永不复用）。"""
    monkeypatch.setenv(API_KEY_HMAC_KEY_ENV, TEST_HMAC_KEY)
    user_id = _new_user("uniq")
    _seed_customer(pg_dsn, user_id)

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        _, record = create_api_key(conn, user_id=user_id)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO customer_api_keys "
                "(id, user_id, key_prefix, key_digest, key_version, token_group_id) "
                "VALUES (%s, %s, %s, %s, 1, %s)",
                (_new_user("dup"), user_id, record.key_prefix, "0" * 64, record.id),
            )


# ===========================================================================
# C 组：认证泳道（customer_fence）——API-Key 独立泳道绝不触碰会话围栏（§2.5 B）
# ===========================================================================


def test_resolve_api_key_user_returns_none_for_a_non_key_bearer() -> None:
    """非 xsk_live_ bearer（会话令牌 / 无凭据）→ None，落回会话或内部泳道。"""
    assert resolve_api_key_user(_request("an-opaque-session-token")) is None
    assert resolve_api_key_user(_request(None)) is None


def test_resolve_api_key_user_rejects_key_when_customer_db_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """customer DB 未配置时 xsk_live_ → 401 API_KEY_INVALID（内部泳道无此表）。"""
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    with pytest.raises(HTTPException) as excinfo:
        resolve_api_key_user(_request(_well_shaped_unknown_key()))

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail["code"] == "API_KEY_INVALID"


def test_business_db_write_takes_the_independent_api_key_lane(
    pg_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_key 泳道的 write() 走 write_for_api_key，绝不调用 fenced_pg_transaction。

    把围栏替身成一律抛 AssertionError 的 _boom：write() 若误入会话泳道即炸。真实
    pg_transaction 内回写 last_used_at，actor 是持钥客户本人（role=customer）。
    """
    user_id = _new_user("lane")
    _seed_customer(pg_env, user_id)
    _, record = _seed_key(pg_env, user_id)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the API-Key lane must never call fenced_pg_transaction")

    monkeypatch.setattr("app.customer_fence.fenced_pg_transaction", _boom)

    business = BusinessDb(
        snapshot=None,
        authorization=None,
        dev_user_id=None,
        api_key=ApiKeyUser(key_id=record.id, user_id=user_id, scopes=DEFAULT_API_KEY_SCOPES),
    )
    with business.write() as (bc, actor):
        assert actor.role == "customer"
        assert actor.id == user_id
        assert bc.execute("SELECT 1").fetchone()[0] == 1

    with psycopg.connect(pg_env) as conn:
        row = conn.execute(
            "SELECT last_used_at FROM customer_api_keys WHERE id = %s", (record.id,)
        ).fetchone()
    assert row is not None and row[0] is not None


# ===========================================================================
# D 组：路由（TestClient）——DTO 契约 / 会话围栏 / 白名单 API-Key 泳道 / 限速
# ===========================================================================


def test_response_dtos_expose_plaintext_once_and_never_digest_or_cost() -> None:
    """明文只在 CreatedApiKeyResponse；记录/列表 DTO 结构上无明文/digest/成本价。"""
    created_fields = set(CreatedApiKeyResponse.model_fields)
    record_fields = set(ApiKeyRecordResponse.model_fields)
    list_fields = set(ApiKeyListResponse.model_fields)

    assert "plaintext" in created_fields
    assert "plaintext" not in record_fields
    assert "plaintext" not in list_fields
    for fields in (created_fields, record_fields, list_fields):
        assert "key_digest" not in fields
        assert "cost_price_fen" not in fields

    # extra=forbid：试图把 digest 塞回记录 DTO 会被拒（字段白名单是结构性的）。
    with pytest.raises(ValidationError):
        ApiKeyRecordResponse(
            id=f"{CW078}dto",
            key_prefix="abcd1234",
            label="",
            scopes=[],
            created_at="2026-01-01T00:00:00+00:00",
            last_used_at=None,
            revoked_at=None,
            key_digest="0" * 64,
        )


def test_management_routes_require_a_customer_session(bare_client: TestClient) -> None:
    """无 PG 运行时（会话快照 None）→ 管理三路由一律 401 SESSION_REQUIRED。"""
    post = bare_client.post(API_KEYS_PATH, json={"label": "x"})
    get = bare_client.get(API_KEYS_PATH)
    delete = bare_client.delete(f"{API_KEYS_PATH}/{CW078}any")

    for resp in (post, get, delete):
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "SESSION_REQUIRED"


def test_create_route_rejects_an_overlong_label(bare_client: TestClient) -> None:
    """超长 label → 422 INVALID_API_KEY_LABEL，且先于会话检查（无 PG 也拦得住）。"""
    resp = bare_client.post(API_KEYS_PATH, json={"label": "x" * (API_KEY_LABEL_MAX + 1)})

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_API_KEY_LABEL"


def test_create_route_returns_plaintext_once_and_audits_prefix_only(
    client: TestClient, stub_session: str, pg_dsn: str
) -> None:
    """创建 201 回明文一次；审计只落 prefix+scopes，明文/digest 永不入 audit_logs。"""
    user_id = stub_session
    resp = client.post(API_KEYS_PATH, json={"label": "probe-label"})

    assert resp.status_code == 201
    body = resp.json()
    assert _PLAINTEXT_RE.fullmatch(body["plaintext"]) is not None
    assert body["label"] == "probe-label"
    assert body["scopes"] == list(DEFAULT_API_KEY_SCOPES)
    assert len(body["key_prefix"]) == 8
    assert "key_digest" not in resp.text

    with psycopg.connect(pg_dsn) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE actor_user_id = %s AND action = 'customer.api_key.created' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    assert audit is not None
    raw_metadata = str(audit[0])
    metadata = json.loads(raw_metadata)
    assert metadata["key_prefix"] == body["key_prefix"]
    assert metadata["scopes"] == list(DEFAULT_API_KEY_SCOPES)
    assert body["plaintext"] not in raw_metadata
    assert "key_digest" not in raw_metadata


def test_list_route_returns_metadata_without_plaintext_or_digest(
    client: TestClient, stub_session: str, pg_dsn: str
) -> None:
    """列表只回元数据：total 计数正确，响应体不含明文/digest。"""
    user_id = stub_session
    _, first = _seed_key(pg_dsn, user_id, label="k1")
    _, second = _seed_key(pg_dsn, user_id, label="k2")

    resp = client.get(API_KEYS_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert "plaintext" not in resp.text
    assert "key_digest" not in resp.text
    assert {item["label"] for item in body["items"]} == {"k1", "k2"}
    assert {item["id"] for item in body["items"]} == {first.id, second.id}


def test_revoke_route_maps_outcomes_idempotently(
    client: TestClient, stub_session: str, pg_dsn: str
) -> None:
    """吊销自己的 key → 204；重复吊销幂等 204；缺失/他人 → 404 API_KEY_NOT_FOUND。"""
    user_id = stub_session
    _, record = _seed_key(pg_dsn, user_id, label="to-revoke")

    first = client.delete(f"{API_KEYS_PATH}/{record.id}")
    again = client.delete(f"{API_KEYS_PATH}/{record.id}")
    missing = client.delete(f"{API_KEYS_PATH}/{CW078}no-such-key")

    assert first.status_code == 204
    assert again.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "API_KEY_NOT_FOUND"


def test_whitelist_recharge_list_with_api_key_succeeds_and_touches_last_used(
    client: TestClient, pg_dsn: str
) -> None:
    """白名单 GET 经 API-Key 独立泳道 200（新客户空列表）+ 回写 last_used_at。"""
    user_id = _new_user("wl")
    _seed_customer(pg_dsn, user_id)
    plaintext, record = _seed_key(pg_dsn, user_id)

    resp = client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []

    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT last_used_at FROM customer_api_keys WHERE id = %s", (record.id,)
        ).fetchone()
    assert row is not None and row[0] is not None


def test_whitelist_recharge_status_is_404_for_a_missing_order(
    client: TestClient, pg_dsn: str
) -> None:
    """白名单状态路由：持合法 key 查不存在订单 → 404 RECHARGE_ORDER_NOT_FOUND。"""
    user_id = _new_user("wl404")
    _seed_customer(pg_dsn, user_id)
    plaintext, _ = _seed_key(pg_dsn, user_id)

    resp = client.get(f"{RECHARGE_LIST_PATH}/{CW078}no-such-order", headers=_bearer(plaintext))

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECHARGE_ORDER_NOT_FOUND"


def test_api_key_lane_fenced_when_master_deactivated(client: TestClient, pg_dsn: str) -> None:
    """封禁母账号后，子账号名下的存量 API key 必须随之失效（上线评审 H-2）。

    会话泳道逐请求复核母账号存活性（deactivating the master freezes every
    session riding under it）；API-Key 泳道此前只查属主自身，构成组织封禁旁路。
    """
    master = _new_user("banm")
    sub = _new_user("bans")
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, registration_source, "
            "account_type, parent_user_id, password_hash) VALUES (%s, %s, %s, 'customer', "
            "'self_register', 'MASTER', NULL, 'cw078-not-a-real-hash')",
            (master, master, f"CW078 {master}"),
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, registration_source, "
            "account_type, parent_user_id) VALUES (%s, %s, %s, 'customer', "
            "'admin_create', 'SUB', %s)",
            (sub, sub, f"CW078 {sub}", master),
        )
    plaintext, _ = _seed_key(pg_dsn, sub)

    assert client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext)).status_code == 200

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = %s", (master,))

    resp = client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext))
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "API_KEY_INVALID"


def test_api_key_lane_fenced_when_bound_activation_code_suspended(
    client: TestClient, pg_dsn: str
) -> None:
    """激活码被管理端 SUSPENDED 后，该账号名下的 API key 必须失效（与会话泳道同链）。"""
    owner = _new_user("code")
    batch_id = f"{CW078}batch-{uuid4().hex[:8]}"
    code_id = f"{CW078}code-{uuid4().hex[:8]}"
    device_id = f"{CW078}dev-{uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, registration_source) "
            "VALUES (%s, %s, %s, 'customer', 'activation_code')",
            (owner, owner, f"CW078 {owner}"),
        )
        conn.execute(
            "INSERT INTO activation_code_batches (id, name, face_value_fen, "
            "unit_price_fen_snapshot, credits_snapshot, quantity, activation_expires_at, "
            "created_by_user_id) VALUES (%s, 'cw078', 100, 100, 100, 1, "
            "'2030-01-01T00:00:00+00:00', %s)",
            (batch_id, owner),
        )
        conn.execute(
            "INSERT INTO activation_codes (id, batch_id, code_digest, digest_key_version, "
            "masked_code, status, issued_at, bound_user_id, activated_at) VALUES "
            "(%s, %s, %s, 1, 'CW07-****', 'ACTIVE', '2026-01-01T00:00:00+00:00', %s, "
            "'2026-01-01T00:00:00+00:00')",
            (code_id, batch_id, uuid4().hex, owner),
        )
        conn.execute(
            "INSERT INTO customer_devices (id, activation_code_id, user_id, slot_no, "
            "display_name, platform, fingerprint_hmac, fingerprint_key_version, "
            "token_digest, token_key_version, status) VALUES "
            "(%s, %s, %s, 1, 'cw078', 'windows', %s, 1, %s, 1, 'BOUND')",
            (device_id, code_id, owner, uuid4().hex, uuid4().hex),
        )
    plaintext, _ = _seed_key(pg_dsn, owner)

    assert client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext)).status_code == 200

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', "
            "suspended_at = '2026-01-02T00:00:00+00:00' WHERE id = %s",
            (code_id,),
        )

    resp = client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext))
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "API_KEY_INVALID"


def test_whitelist_without_credential_requires_a_session(client: TestClient) -> None:
    """PG 已配置 + 无凭据 → 会话泳道 401 SESSION_TOKEN_REQUIRED（非 API_KEY_INVALID）。"""
    resp = client.get(RECHARGE_LIST_PATH)

    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


def test_whitelist_with_an_invalid_api_key_is_401(client: TestClient) -> None:
    """形状合法但库中不存在的 key → 单一失败答案 401 API_KEY_INVALID（无 oracle）。"""
    resp = client.get(RECHARGE_LIST_PATH, headers=_bearer(_well_shaped_unknown_key()))

    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "API_KEY_INVALID"


def test_api_key_failure_budget_trips_429_with_retry_after(
    client: TestClient, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apikey:ip 预算耗尽：首犯 401，再犯升级 429 API_KEY_RATE_LIMITED + Retry-After。"""
    # 复位 CW-078 独占的 apikey:* 桶，避免兄弟用例消耗泄漏进本用例预算判定。
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            # counters 表按 032 设计只有复合 bucket_key（"{dimension}|{identifier}"），
            # 无独立 dimension 列——按前缀匹配清 apikey:* 桶。
            "DELETE FROM security_rate_limit_counters "
            "WHERE bucket_key LIKE %s OR bucket_key LIKE %s",
            (f"{DIMENSION_APIKEY_IP}|%", f"{DIMENSION_APIKEY_KEY}|%"),
        )
    monkeypatch.setenv(RATE_LIMIT_APIKEY_IP_ENV, "1")

    unknown = _well_shaped_unknown_key()
    first = client.get(RECHARGE_LIST_PATH, headers=_bearer(unknown))
    second = client.get(RECHARGE_LIST_PATH, headers=_bearer(unknown))

    assert first.status_code == 401
    assert first.json()["detail"]["code"] == "API_KEY_INVALID"
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "API_KEY_RATE_LIMITED"
    assert RETRY_AFTER_HEADER in second.headers
    assert int(second.headers[RETRY_AFTER_HEADER]) > 0


def test_whitelist_and_management_responses_never_leak_secret_or_cost(
    client: TestClient, stub_session: str
) -> None:
    """端到端扫响应体：明文只出现一次，digest / cost_price_fen 永不外泄。"""
    created = client.post(API_KEYS_PATH, json={"label": "leak-probe"})
    assert created.status_code == 201
    plaintext = created.json()["plaintext"]
    assert _PLAINTEXT_RE.fullmatch(plaintext) is not None
    assert "key_digest" not in created.text
    assert "cost_price_fen" not in created.text

    listed = client.get(API_KEYS_PATH)
    assert listed.status_code == 200
    assert "key_digest" not in listed.text
    assert "cost_price_fen" not in listed.text
    assert plaintext not in listed.text  # 明文只在创建响应出现一次

    whitelisted = client.get(RECHARGE_LIST_PATH, headers=_bearer(plaintext))
    assert whitelisted.status_code == 200
    assert "cost_price_fen" not in whitelisted.text
    assert plaintext not in whitelisted.text
