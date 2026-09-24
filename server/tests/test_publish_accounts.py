"""CW-068 — C5 发布模块第一阶段（账号授权）TEST-PG 矩阵.

分支 ``feat/c5-publish-module`` 的 ``test_publish.py`` 跑在 SQLite lane 上；本
模块把其中**账号授权**那一半移植到真实 PostgreSQL（CW-002 §1：客户云版数据库
行为以真实 PG 为准）。正式发布链路（records / publish round / published_total）
按 §8 属第二阶段，其 14 条用例本轮不迁，随代码一起留到下一任务。

每条用例映射 ``docs/evidence/CW002-SCOPE-DECISIONS.md`` §9「发布管理专用验收
矩阵」的一行（A1–A12）：

- A1–A3 连接：抖音必须同时给 cookie + security_sdk；视频号只需 cookie
- A4 **凭据不回传**：任何 accounts 端点响应体都不出现 cookie / security_sdk
- A5 **密文落库**：Fernet 密文 + 全库明文扫描（PG lane 用 ``iterdump()``
  遍历每张用户表的每一行，替代 SQLite 版扫 ``.db`` 文件字节）+ 可解密还原
- A6/A7 **用户隔离**：列表只返回本人账号；跨属主 DELETE 404 掩蔽
- A8 探测发起 + 跨属主 404
- A9 探测租约与回写（claim / finalize 的 CAS 语义）
- A10 **PG 专有**：``FOR UPDATE SKIP LOCKED`` 并发抢占。SQLite lane 的有界
  翻译层会把 ``FOR UPDATE [SKIP LOCKED]`` 整段移除（db_portable T21/SES-04），
  所以这条语义只能在真实 PG 上覆盖 —— 正是迁 PG 的收益
- A11 worker 轮次闭环（claim → _dispatch_probe → finalize）
- A12 **范围切分**：verify claim 在只有 ``publish_accounts``、没有
  ``publish_records`` 的库上正常工作，证明 ``_quarantine_expired_verifies()``
  已从原 ``_quarantine_expired_publishes()``（同时 UPDATE 两张表）解耦

专属隔离库 ``cw068_publish_accounts_test`` 已登记
``pg_test_kit.RECORDED_TEST_DATABASES``；用例间 TRUNCATE 隔离。缺 PG 即硬失败
（PG-05），不静默 skip。

路由替身说明：accounts 的**写**路由骑 ``BusinessDbDep``，其 customer session
围栏在 PG lane 上拒绝内部 ``X-Dev-User-Id`` 身份（CW-026/CW-031 已锁定该安全
性质），故用 ``_PgBusinessDb`` 注入选定 actor —— 它仍然打开**真实**
``pg_transaction``，并照生产 ``fenced_pg_transaction`` 处理
``AuditedSecurityDenial``（catch → persist → re-raise）。数据库从不 mock。
**读**路由（``GET /accounts``）骑未围栏的 ``get_database`` PG 分支，同样注入
actor 以在一条用例内切换属主。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser, get_current_user
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.main import app
from app.permissions import AuditedSecurityDenial, persist_security_denial
from app.publish import claim_account_verify_work, finalize_account_verify
from app.settings import SETTINGS_KEY_ENV

CW068_TEST_DB = "cw068_publish_accounts_test"


def test_w19_publish_stop_prevents_any_new_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event

    from app import publish_worker

    stop = Event()
    stop.set()
    monkeypatch.setattr(publish_worker, "check_pg_ready", lambda: object())
    monkeypatch.setattr(publish_worker, "_pg_round", lambda **_: pytest.fail("claim after stop"))
    publish_worker.run_pg_forever(worker_id="instance", idle_seconds=2, stop_event=stop)


def test_w19_publish_stops_after_busy_round_and_reuses_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from app import publish_worker

    stop = Event()
    observed: list[str] = []
    monkeypatch.setattr(publish_worker, "check_pg_ready", lambda: object())

    def process(*, worker_id: str) -> int:
        observed.append(worker_id)
        if len(observed) == 2:
            stop.set()
        assert len(observed) <= 2
        return 1

    monkeypatch.setattr(publish_worker, "_pg_round", process)
    publish_worker.run_pg_forever(worker_id="same-startup", idle_seconds=2, stop_event=stop)
    assert observed == ["same-startup", "same-startup"]


def test_w19_publish_idle_wait_can_be_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event

    from app import publish_worker

    stop = Event()
    waited: list[float] = []
    monkeypatch.setattr(publish_worker, "check_pg_ready", lambda: object())
    monkeypatch.setattr(publish_worker, "_pg_round", lambda **_: 0)

    def wait(seconds: float) -> bool:
        waited.append(seconds)
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", wait)
    publish_worker.run_pg_forever(worker_id="instance", idle_seconds=3600, stop_event=stop)
    assert len(waited) == 1 and 0 < waited[0] <= 0.2


def test_w19_publish_signal_during_event_wait_cannot_deadlock() -> None:
    import subprocess
    import sys
    import textwrap

    # Isolate the old deadlock: the parent always reaps the child on timeout.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import signal
            import sys
            from threading import Event
            from types import SimpleNamespace
            from app import publish_worker as worker

            class SignalDuringWait(Event):
                def wait(self, timeout=None):
                    with self._cond:
                        signal.raise_signal(signal.SIGTERM)
                    return self.is_set()

            previous = {kind: signal.getsignal(kind)
                        for kind in (signal.SIGINT, signal.SIGTERM)}
            closed = []
            rounds = []
            worker.Event = SignalDuringWait
            worker.check_pg_ready = lambda: object()
            worker.resolve_database_config = lambda: SimpleNamespace(
                mode=worker.DatabaseMode.POSTGRESQL)
            worker.validate_customer_production = lambda _: None
            worker.close_pg_pool = lambda: closed.append(True)
            def idle(**kwargs):
                rounds.append(kwargs['worker_id'])
                assert len(rounds) == 1, 'new claim after SIGTERM'
                return 0
            worker._pg_round = idle
            sys.argv = ['publish-worker', '--idle-seconds', '3600']
            worker.main()
            assert closed == [True]
            assert len(rounds) == 1
            assert all(signal.getsignal(kind) == handler
                       for kind, handler in previous.items())
            print('shutdown completed')
        """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "shutdown completed" in result.stdout


def test_w19_publish_cli_closes_pool_and_restores_signals_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal
    import sys
    from types import SimpleNamespace

    from app import publish_worker

    handlers: dict[Any, Any] = {}
    original = object()
    closed: list[bool] = []
    observed: list[str] = []
    monkeypatch.setattr(sys, "argv", ["publish-worker", "--worker-id", "pool-a"])
    monkeypatch.setattr(signal, "getsignal", lambda _: original)
    monkeypatch.setattr(signal, "signal", lambda kind, handler: handlers.__setitem__(kind, handler))
    monkeypatch.setattr(
        publish_worker,
        "resolve_database_config",
        lambda: SimpleNamespace(mode=publish_worker.DatabaseMode.POSTGRESQL),
    )
    monkeypatch.setattr(publish_worker, "validate_customer_production", lambda _: None)
    monkeypatch.setattr(publish_worker, "close_pg_pool", lambda: closed.append(True))

    def fail(**kwargs: Any) -> None:
        observed.append(kwargs["worker_id"])
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert kwargs["stop_requested"]()
        raise RuntimeError("synthetic loop failure")

    monkeypatch.setattr(publish_worker, "run_pg_forever", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic loop failure"):
            publish_worker.main()
        assert handlers[signal.SIGTERM] is original
        assert handlers[signal.SIGINT] is original
    assert len(closed) == 2 and observed[0] != observed[1]
    assert all(value.startswith("pool-a:") for value in observed)


ACCOUNTS_URL = "/api/studio/publish/accounts"

_TEST_KEY = Fernet.generate_key().decode("ascii")

# 合成凭据：形状像真 cookie，但不含任何真实会话值，避开仓库密钥扫描。
_DOUYIN_COOKIE = "sessionid=douyin-test-cookie-value; ttwid=1" + "0" * 32
_SECURITY_SDK = '{"key_version": 3, "ticket": "test-ticket", "data": "' + "x" * 64 + '"}'

# 中断租约复位文案：由 _quarantine_expired_verifies() 写入，A12 逐字断言。
_INTERRUPTED_MESSAGE = "登录态校验中断，请重新发起验证"


def actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(  # type: ignore[arg-type]
        id=user_id, username=user_id, display_name=user_id, role=role
    )


# --------------------------------------------------------------------------- #
# 座子：专属库 + TRUNCATE 隔离 + 生产 PG 通道
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def cw068_dsn() -> Iterator[str]:
    """专属账号授权测试库：建库 → alembic head（082）→ 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW068_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW068_TEST_DB)


_TRUNCATED_TABLES = "publish_accounts, users, audit_logs"


@pytest.fixture()
def pg(cw068_dsn: str) -> Iterator[psycopg.Connection]:
    """autocommit 原生连接：负责播种与密文落库断言读取；用例间 TRUNCATE 隔离.

    与 ``pg_test_kit.seed_customer_scenario`` 同款：append-only 审计表的
    TRUNCATE 拒绝触发器在 ``session_replication_role = replica`` 下不触发
    （专属 allowlist 测试库内的用例隔离，不影响任何共享库）。
    """
    close_pg_pool()
    conn = psycopg.connect(cw068_dsn, autocommit=True)
    conn.execute("SET session_replication_role = replica")
    conn.execute(f"TRUNCATE {_TRUNCATED_TABLES} CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
            ],
        )
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


@pytest.fixture()
def lane_env(cw068_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：get_database / pg_transaction 走生产 PG 通道.

    凭据加密必须走环境变量密钥，绝不回落到 OS keystore。
    """
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, cw068_dsn)
    monkeypatch.setenv(SETTINGS_KEY_ENV, _TEST_KEY)
    yield cw068_dsn
    close_pg_pool()


class _PgBusinessDb:
    """A ``BusinessDb`` double whose ``write()`` runs on a real PG transaction."""

    def __init__(self, current_actor: CurrentUser) -> None:
        self.current_actor = current_actor

    @contextmanager
    def write(self, *, isolation: Any = None) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        try:
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), self.current_actor
        except AuditedSecurityDenial as exc:
            persist_security_denial(exc)
            raise


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    """Guarantee a route double never leaks into the next test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def holder() -> _PgBusinessDb:
    """Pin the acting user for both the write fence and the read dependency.

    读写共用一个 holder：改 ``holder.current_actor`` 一次即可在同一用例内切换
    属主（A6/A7/A8 的跨用户隔离断言需要）。
    """
    db = _PgBusinessDb(actor("employee_1"))
    app.dependency_overrides[get_business_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.current_actor
    return db


@pytest.fixture()
def client(pg: psycopg.Connection, lane_env: str, holder: _PgBusinessDb) -> Iterator[TestClient]:
    yield TestClient(app)


# --------------------------------------------------------------------------- #
# 播种与请求助手
# --------------------------------------------------------------------------- #


def account_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "platform": "douyin",
        "display_name": "乡墅张工",
        "cookie": _DOUYIN_COOKIE,
        "security_sdk": _SECURITY_SDK,
    }
    body.update(overrides)
    return body


def connect_account(
    client: TestClient, holder: _PgBusinessDb, *, owner: str = "employee_1", **overrides: object
) -> dict[str, Any]:
    """走真实 HTTP 连接一个账号，返回响应体（A3/A4 的被测对象）."""
    holder.current_actor = actor(owner)
    response = client.post(ACCOUNTS_URL, json=account_body(**overrides))
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def seed_account(
    pg: psycopg.Connection,
    account_id: str,
    *,
    user_id: str = "employee_1",
    platform: str = "douyin",
    cookie_enc: str = "gAAAAA-placeholder-enc",
    security_sdk_enc: str | None = None,
    status: str = "connected",
    verify_requested: int = 0,
) -> None:
    """直插一行账号，绕过 HTTP：claim / worker 用例只关心持久化状态."""
    pg.execute(
        "INSERT INTO publish_accounts (id, user_id, platform, display_name, cookie_enc,"
        " security_sdk_enc, status, verify_requested)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account_id,
            user_id,
            platform,
            "号",
            cookie_enc,
            security_sdk_enc,
            status,
            verify_requested,
        ),
    )


def account_row(pg: psycopg.Connection, account_id: str, columns: str) -> tuple[Any, ...]:
    row = pg.execute(
        f"SELECT {columns} FROM publish_accounts WHERE id = %s",  # noqa: S608 - 测试内固定字面量
        (account_id,),
    ).fetchone()
    assert row is not None, f"account {account_id} disappeared"
    return tuple(row)


# --------------------------------------------------------------------------- #
# A1–A5 连接与凭据保护（HTTP 层）
# --------------------------------------------------------------------------- #


def test_create_douyin_account_requires_security_sdk(
    client: TestClient, holder: _PgBusinessDb, pg: psycopg.Connection
) -> None:
    """A1：抖音必须同时粘贴 cookie 与 security_sdk，缺后者 422."""
    holder.current_actor = actor("employee_1")
    response = client.post(ACCOUNTS_URL, json=account_body(security_sdk=None))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PUBLISH_SECURITY_SDK_REQUIRED"
    # 被拒的请求不得留下半建成的账号行。
    count = pg.execute("SELECT count(*) FROM publish_accounts").fetchone()
    assert count is not None and count[0] == 0


def test_create_account_roundtrip_and_credential_never_returned(
    client: TestClient, holder: _PgBusinessDb
) -> None:
    """A3 连接成功回显 + A4 **凭据不回传**（创建与列表两个端点都扫）."""
    created = connect_account(client, holder)
    assert created["platform"] == "douyin"
    assert created["display_name"] == "乡墅张工"
    assert created["status"] == "connected"
    assert created["security_sdk_required"] is True
    assert "cookie" not in created
    assert "security_sdk" not in created

    holder.current_actor = actor("employee_1")
    listed = client.get(ACCOUNTS_URL)
    assert listed.status_code == 200, listed.text
    accounts = listed.json()["accounts"]
    assert [item["display_name"] for item in accounts] == ["乡墅张工"]

    # A4：把两个端点的完整响应体（含 header）都序列化后扫明文凭据。
    serialized = repr(created) + repr(listed.json()) + repr(dict(listed.headers))
    assert _DOUYIN_COOKIE not in serialized
    assert _SECURITY_SDK not in serialized


def test_channel_account_omits_security_sdk(client: TestClient, holder: _PgBusinessDb) -> None:
    """A2：视频号只需 cookie，security_sdk_required 为 False."""
    created = connect_account(
        client, holder, platform="wechat_channels", display_name="视频号小墅", security_sdk=None
    )
    assert created["platform"] == "wechat_channels"
    assert created["security_sdk_required"] is False


def test_cookie_is_encrypted_at_rest(
    client: TestClient, holder: _PgBusinessDb, pg: psycopg.Connection
) -> None:
    """A5 **密文落库**：Fernet 密文 + 全库明文扫描 + 可解密还原.

    SQLite 版扫 ``.db`` 文件字节；PG lane 用 ``BusinessConnection.iterdump()``
    遍历每张用户表的每一行（CW-054 起 PG lane 产出真实 INSERT），覆盖面等价
    且不受表空间/文件布局影响。额外补一条可解密断言：密文必须能还原成原文，
    否则「不等于明文」也可能只是被截断的垃圾。
    """
    created = connect_account(client, holder)
    cookie_enc, sdk_enc = account_row(pg, str(created["id"]), "cookie_enc, security_sdk_enc")
    assert cookie_enc != _DOUYIN_COOKIE
    assert str(cookie_enc).startswith("gAAAAA")
    assert sdk_enc != _SECURITY_SDK
    assert sdk_enc is not None

    with pg_transaction() as raw:
        dump = "\n".join(BusinessConnection.postgres(raw).iterdump())
    # Sentinel：dump 必须真的遍历到了这一行的密文列，否则下面两条「明文不在
    # dump 里」会在空 dump 上恒真（假绿）。
    assert str(cookie_enc) in dump
    assert str(sdk_enc) in dump
    assert _DOUYIN_COOKIE not in dump
    assert _SECURITY_SDK not in dump

    fernet = Fernet(_TEST_KEY.encode("ascii"))
    assert fernet.decrypt(str(cookie_enc).encode("ascii")).decode("utf-8") == _DOUYIN_COOKIE
    assert fernet.decrypt(str(sdk_enc).encode("ascii")).decode("utf-8") == _SECURITY_SDK


# --------------------------------------------------------------------------- #
# A6–A8 用户隔离与探测发起
# --------------------------------------------------------------------------- #


def test_accounts_are_user_scoped(client: TestClient, holder: _PgBusinessDb) -> None:
    """A6 **用户隔离（读）**：列表只返回本人账号，他人账号不可见."""
    connect_account(client, holder, owner="employee_1")
    connect_account(client, holder, owner="employee_2", display_name="别人家的号")

    holder.current_actor = actor("employee_1")
    mine = client.get(ACCOUNTS_URL).json()["accounts"]
    assert [item["display_name"] for item in mine] == ["乡墅张工"]

    holder.current_actor = actor("employee_2")
    theirs = client.get(ACCOUNTS_URL).json()["accounts"]
    assert [item["display_name"] for item in theirs] == ["别人家的号"]


def test_delete_account_is_owner_scoped(client: TestClient, holder: _PgBusinessDb) -> None:
    """A7 **用户隔离 + 解绑**：跨属主 DELETE 404 掩蔽，属主 200 且列表清空."""
    created = connect_account(client, holder, owner="employee_1")
    account_id = str(created["id"])

    holder.current_actor = actor("employee_2")
    foreign = client.delete(f"{ACCOUNTS_URL}/{account_id}")
    assert foreign.status_code == 404
    # 404 掩蔽：不得泄露「该账号存在但属于别人」。
    assert foreign.json()["detail"]["code"] == "PUBLISH_ACCOUNT_NOT_FOUND"

    holder.current_actor = actor("employee_1")
    removed = client.delete(f"{ACCOUNTS_URL}/{account_id}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert client.get(ACCOUNTS_URL).json()["accounts"] == []


def test_verify_request_marks_account(
    client: TestClient, holder: _PgBusinessDb, pg: psycopg.Connection
) -> None:
    """A8 探测发起 + 跨属主 404：verify_requested 落 1，他人无法代发起."""
    created = connect_account(client, holder, owner="employee_1")
    account_id = str(created["id"])

    holder.current_actor = actor("employee_1")
    response = client.post(f"{ACCOUNTS_URL}/{account_id}/verify")
    assert response.status_code == 200
    assert response.json()["submitted"] is True

    holder.current_actor = actor("employee_2")
    foreign = client.post(f"{ACCOUNTS_URL}/{account_id}/verify")
    assert foreign.status_code == 404

    assert account_row(pg, account_id, "verify_requested") == (1,)


# --------------------------------------------------------------------------- #
# A9 探测租约与回写（domain 层，真实 PG 事务）
# --------------------------------------------------------------------------- #


def test_account_verify_claim_and_finalize(lane_env: str, pg: psycopg.Connection) -> None:
    """A9：claim 得到 account_verify 租约、同一请求二次 claim 落空、
    finalize(ok=False) 把账号转 invalid 并复位排队标记与错误文案.

    分支 SQLite 版先播一条 ``publish_records`` 再播账号；本阶段没有那张表，
    直接播账号即等价（records 属第二阶段）。
    """
    seed_account(pg, "acc_1", verify_requested=1)

    with psycopg.connect(lane_env) as raw:
        conn = BusinessConnection.postgres(raw)
        lease = claim_account_verify_work(conn, worker_id="w1")
        assert lease is not None
        assert lease.kind == "account_verify"
        assert lease.record_id == "acc_1"
        # 租约已写入（lease_expires_at 在未来），同一事务内二次 claim 必须落空。
        assert claim_account_verify_work(conn, worker_id="w1") is None
        finalize_account_verify(conn, lease=lease, ok=False, message="Cookie 已失效")
        raw.commit()

    assert account_row(pg, "acc_1", "status, verify_requested, error_message") == (
        "invalid",
        0,
        "Cookie 已失效",
    )
    # 租约必须被清空，否则该行再也无法被重新探测。
    assert account_row(pg, "acc_1", "lease_owner, lease_expires_at") == (None, None)
    assert account_row(pg, "acc_1", "last_verified_at")[0] is not None


# --------------------------------------------------------------------------- #
# A10 FOR UPDATE SKIP LOCKED 并发抢占（PG 专有，SQLite lane 无法覆盖）
# --------------------------------------------------------------------------- #


def test_verify_claim_skip_locked_across_concurrent_connections(
    lane_env: str, pg: psycopg.Connection
) -> None:
    """A10：两个 worker 并发 claim 同一 verify 请求，只有一个拿到租约.

    SQLite lane 的有界翻译层把 ``FOR UPDATE [SKIP LOCKED]`` 整段移除，因此这条
    抢占语义只能在真实 PG 上验证。第二个连接带 ``lock_timeout``：若 SKIP LOCKED
    退化成普通 ``FOR UPDATE``，它会**阻塞**在行锁上并被超时打断抛错，而不是
    静默返回 None —— 断言因此能区分「跳过被锁行」与「等锁」。
    """
    seed_account(pg, "acc_lock", verify_requested=1)

    first_raw = psycopg.connect(lane_env)
    second_raw = psycopg.connect(lane_env, options="-c lock_timeout=5000")
    try:
        first = claim_account_verify_work(BusinessConnection.postgres(first_raw), worker_id="w-a")
        assert first is not None
        assert first.record_id == "acc_lock"
        # first_raw 刻意不 commit：FOR UPDATE 行锁仍然持有，模拟探测尚未完成。
        second = claim_account_verify_work(BusinessConnection.postgres(second_raw), worker_id="w-b")
        assert second is None
    finally:
        first_raw.close()
        second_raw.close()

    # first_raw 关闭 = 事务回滚，租约随之消失（崩溃安全：探测进程死亡不会把
    # 账号永久钉在租约里）。第三个 worker 应能重新 claim 到同一账号。
    with psycopg.connect(lane_env) as third_raw:
        third = claim_account_verify_work(BusinessConnection.postgres(third_raw), worker_id="w-c")
        third_raw.commit()
    assert third is not None
    assert third.record_id == "acc_lock"
    assert account_row(pg, "acc_lock", "verify_requested") == (1,)


# --------------------------------------------------------------------------- #
# A11 worker 轮次闭环（适配器打桩，数据库真实）
# --------------------------------------------------------------------------- #


def test_worker_round_verifies_invalid_account(
    lane_env: str, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A11：run_publish_round 走 claim → _dispatch_probe → finalize 全链.

    probe 收到解密后的真 cookie 与正确 platform；返回失败则账号转 invalid。
    平台 I/O 打桩（不触网），数据库与 Fernet 解密全程真实。
    """
    import app.publish_worker as worker_mod

    fernet = Fernet(_TEST_KEY.encode("ascii"))
    cookie_enc = fernet.encrypt(_DOUYIN_COOKIE.encode("utf-8")).decode("ascii")
    seed_account(
        pg,
        "acc_v",
        platform="wechat_channels",
        cookie_enc=cookie_enc,
        verify_requested=1,
    )

    probes: list[tuple[str, str]] = []

    def fake_probe(platform: str, cookie: str, security_sdk: str | None) -> tuple[bool, str | None]:
        probes.append((platform, cookie))
        return False, "Cookie 已过期"

    monkeypatch.setattr(worker_mod, "_dispatch_probe", fake_probe)

    @contextmanager
    def open_txn() -> Iterator[BusinessConnection]:
        with pg_transaction() as raw:
            yield BusinessConnection.postgres(raw)

    processed = worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=fernet)
    assert processed == 1
    assert probes == [("wechat_channels", _DOUYIN_COOKIE)]
    assert account_row(pg, "acc_v", "status, verify_requested, error_message") == (
        "invalid",
        0,
        "Cookie 已过期",
    )

    # 队列已排空：再跑一轮不得重复探测同一账号。
    probes.clear()
    assert worker_mod.run_publish_round(open_txn, worker_id="w1", fernet=fernet) == 0
    assert probes == []


# --------------------------------------------------------------------------- #
# A12 范围切分：accounts-only 库上没有 publish_records
# --------------------------------------------------------------------------- #


def test_claim_verify_does_not_touch_publish_records(lane_env: str, pg: psycopg.Connection) -> None:
    """A12：verify claim 与中断租约复位都只碰 ``publish_accounts``.

    第二阶段（20260917T1000_publish_records）已建 ``publish_records`` 表，但
    verify 半边的范围切分不变：把 records 表临时改名藏起来后，claim 与复位
    仍须在 accounts-only 的库上跑通——若 ``_quarantine_expired_verifies()``
    像 ``publish_records._quarantine_expired_publishes()`` 那样触碰 records，
    这里会直接报缺表。
    """
    pg.execute("ALTER TABLE publish_records RENAME TO publish_records_hidden_a12")
    try:
        _run_verify_claim_on_accounts_only_schema(lane_env, pg)
    finally:
        pg.execute("ALTER TABLE publish_records_hidden_a12 RENAME TO publish_records")


def _run_verify_claim_on_accounts_only_schema(lane_env: str, pg: psycopg.Connection) -> None:
    tables = pg.execute(
        "SELECT tablename FROM pg_tables"
        " WHERE schemaname = 'public' AND tablename IN ('publish_accounts','publish_records')"
        " ORDER BY tablename"
    ).fetchall()
    assert [row[0] for row in tables] == ["publish_accounts"]

    seed_account(pg, "acc_scope", verify_requested=1)

    # 第一段：accounts-only 库上 claim 正常拿到租约。
    with psycopg.connect(lane_env) as raw:
        lease = claim_account_verify_work(BusinessConnection.postgres(raw), worker_id="w-scope")
        raw.commit()
    assert lease is not None
    assert lease.record_id == "acc_scope"

    # 第二段：模拟探测进程中途死亡留下的过期租约 —— 复位路径同样必须在没有
    # records 表的库上跑通。复位后该请求退出队列，claim 落空。
    pg.execute(
        "UPDATE publish_accounts SET verify_requested = 1, lease_owner = 'dead-worker',"
        " lease_expires_at = '2000-01-01 00:00:00', attempt_count = 3"
        " WHERE id = 'acc_scope'"
    )
    with psycopg.connect(lane_env) as raw:
        assert (
            claim_account_verify_work(BusinessConnection.postgres(raw), worker_id="w-next") is None
        )
        raw.commit()
    assert account_row(
        pg, "acc_scope", "verify_requested, lease_owner, lease_expires_at, error_message"
    ) == (0, None, None, _INTERRUPTED_MESSAGE)
