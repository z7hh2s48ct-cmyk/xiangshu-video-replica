"""CW-062 B3：客户消费流水的「子账号维度」——真实集成测试。

**本文件在 2026-09-22 被整体重写。** 原版是占位实现：断言写成
``assert isinstance(...) or True``（恒真），或者对刚构造的局部对象再断言一次自己，
既不经过路由也不碰数据库，却报「6 passed / 7 total」。那种绿是假的——B3 的后端因此
一直处于「代码在、无人验证」的状态。

本版按 B1/B2/B4 的既有口径重写：真注册登录拿会话 → 真发 HTTP 请求 → 真断言响应
与 CSV 字节，并对跨账号隔离留锁。同时把三处实现缺陷暴露成失败用例（先红后绿）：

1. 聚合摘要的 ``credit_total`` 被硬编码成 0（SQL 里算出来的值被丢掉）；
2. 聚合摘要**忽略用户已经应用的筛选**（只按钱包归属过滤），与下方列表口径不一致；
3. CSV 表头用**全角逗号**分隔，数据行用半角逗号——表头与数据列数不一致，
   而且末列直接把内部 ``actor_user_id`` 导出给客户。

覆盖 B3 新增的三件事：
- ``GET /api/customer/wallet/transactions?sub_account_id=`` 按操作者过滤；
- 同端点 ``group_by_sub_account=true`` 的聚合摘要；
- ``GET /api/customer/wallet/transactions/export`` 的 CSV 导出。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import csv
import io
import os
import secrets
from uuid import uuid4

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

TRANSACTIONS_PATH = "/api/customer/wallet/transactions"
EXPORT_PATH = "/api/customer/wallet/transactions/export"
SUB_ACCOUNTS_PATH = "/api/customer/sub-accounts"

MASTER_PASSWORD = "master-pass-9"
SUB_PASSWORD = "sub-pass-9"
DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.customer_sub_account_routes import router as sub_account_router

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_API_KEY_HMAC_KEY", secrets.token_urlsafe(48))
    registration_client.app.include_router(sub_account_router)
    return registration_client


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _fingerprint() -> str:
    return "fp-" + uuid4().hex + uuid4().hex


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": _fingerprint(),
        },
    )


def _master_session(client, username: str):
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": MASTER_PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    login = _login(client, username, MASTER_PASSWORD)
    assert login.status_code == 200, login.text
    body = login.json()
    return {"Authorization": "Bearer " + body["session_token"]}, body


def _username(tag: str) -> str:
    return tag + uuid4().hex[:10]


def _create_sub(client, headers, username: str, display_name: str) -> str:
    """用母账号会话建一个子账号，返回它的 user_id。"""
    created = client.post(
        SUB_ACCOUNTS_PATH,
        headers=headers,
        json={"username": username, "display_name": display_name, "password": SUB_PASSWORD},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _seed_transaction(
    dsn: str,
    *,
    tx_id: str,
    owner_id: str,
    kind: str,
    credits: int,
    actor_id: str | None,
    created_at: str,
    service: str = "asr",
) -> None:
    """按 ``ck_wallet_transactions_shape`` 播一行账本。

    约束原文（PG 实读）：RESERVE/SETTLE/RELEASE 必须 ``recharge_order_id IS NULL``、
    ``billing_round IS NOT NULL``，且 ``task_id``/``oral_task_id`` 至多一个非空——
    两者都空时**必须**挂一个 ``billing_operation_id``（外键指向 billing_operations）。
    这里沿用 ``test_sub_account_quota`` 的口径：先播 operation 行，流水挂它，
    用 ``task_id = NULL`` 避开 ``uq_wallet_transactions_terminal_round``。
    """
    available, reserved = {
        "RESERVE": (-credits, credits),
        "SETTLE": (0, -credits),
        "RELEASE": (credits, -credits),
    }[kind]
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO billing_operations (id, user_id, service, module, source_id, "
            "request_fingerprint, pricing_snapshot_json, unit, budget_units, "
            "reserved_credits, created_at) "
            "VALUES (%s, %s, %s, 'analysis', %s, '', '{}', 'second', 2, %s, %s)",
            (tx_id, owner_id, service, tx_id, credits, created_at),
        )
        conn.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, billing_round, "
            " billing_operation_id, idempotency_key, actor_user_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s)",
            (
                tx_id,
                owner_id,
                kind,
                available,
                reserved,
                tx_id,
                f"{tx_id}:1",
                actor_id,
                created_at,
            ),
        )


def _transaction_ids(response) -> set[str]:
    return {item["id"] for item in response.json()["items"]}


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------


def test_transactions_require_a_session(client) -> None:
    """无会话令牌 → 401。"""
    assert client.get(TRANSACTIONS_PATH).status_code == 401


def test_sub_account_filter_narrows_to_that_actor(client, registration_dsn) -> None:
    """带上 sub_account_id 只回该操作者的行；不带则回全部（含母账号自己的）。"""
    master_headers, master = _master_session(client, _username("b3master"))
    sub_a = _create_sub(client, master_headers, _username("b3suba"), "子账号甲")
    sub_b = _create_sub(client, master_headers, _username("b3subb"), "子账号乙")
    owner = master["user_id"]

    _seed_transaction(
        registration_dsn,
        tx_id="tx-own",
        owner_id=owner,
        kind="RESERVE",
        credits=500,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="tx-a",
        owner_id=owner,
        kind="SETTLE",
        credits=30,
        actor_id=sub_a,
        created_at="2026-09-20 11:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="tx-b",
        owner_id=owner,
        kind="SETTLE",
        credits=70,
        actor_id=sub_b,
        created_at="2026-09-20 12:00:00+08",
    )

    everything = client.get(TRANSACTIONS_PATH, headers=master_headers)
    assert everything.status_code == 200, everything.text
    assert _transaction_ids(everything) == {"tx-own", "tx-a", "tx-b"}

    only_a = client.get(TRANSACTIONS_PATH, headers=master_headers, params={"sub_account_id": sub_a})
    assert only_a.status_code == 200, only_a.text
    assert _transaction_ids(only_a) == {"tx-a"}
    assert only_a.json()["total"] == 1


def test_ledger_never_crosses_wallets(client, registration_dsn) -> None:
    """另一个母账号的流水不会出现在我的列表里（跨账号不可枚举）。"""
    mine_headers, mine = _master_session(client, _username("b3mine"))
    theirs_headers, theirs = _master_session(client, _username("b3theirs"))

    _seed_transaction(
        registration_dsn,
        tx_id="tx-mine",
        owner_id=mine["user_id"],
        kind="RESERVE",
        credits=10,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="tx-theirs",
        owner_id=theirs["user_id"],
        kind="RESERVE",
        credits=99,
        actor_id=None,
        created_at="2026-09-20 10:05:00+08",
    )

    assert _transaction_ids(client.get(TRANSACTIONS_PATH, headers=mine_headers)) == {"tx-mine"}
    assert _transaction_ids(client.get(TRANSACTIONS_PATH, headers=theirs_headers)) == {"tx-theirs"}


# ---------------------------------------------------------------------------
# 聚合摘要
# ---------------------------------------------------------------------------


def test_sub_account_summary_reports_both_directions(client, registration_dsn) -> None:
    """聚合摘要同时给出「实际结算掉的」与「退回的」两侧金额。

    口径（本批定义，界面按此展示）：``debit_total`` = 该子账号 SETTLE 掉的额度，
    ``credit_total`` = 退回到该子账号名下的额度。原实现把 credit_total 硬编码为 0，
    等于把「退回」这一半永久抹平——对账时看不出来。
    """
    master_headers, master = _master_session(client, _username("b3agg"))
    sub_a = _create_sub(client, master_headers, _username("b3agga"), "账房甲")
    owner = master["user_id"]

    _seed_transaction(
        registration_dsn,
        tx_id="agg-settle-1",
        owner_id=owner,
        kind="SETTLE",
        credits=120,
        actor_id=sub_a,
        created_at="2026-09-20 10:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="agg-settle-2",
        owner_id=owner,
        kind="SETTLE",
        credits=80,
        actor_id=sub_a,
        created_at="2026-09-20 10:10:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="agg-release",
        owner_id=owner,
        kind="RELEASE",
        credits=25,
        actor_id=sub_a,
        created_at="2026-09-20 10:20:00+08",
    )

    response = client.get(
        TRANSACTIONS_PATH, headers=master_headers, params={"group_by_sub_account": True}
    )
    assert response.status_code == 200, response.text
    summary = response.json()["sub_account_summary"]
    assert summary is not None, "开启聚合后必须给出 sub_account_summary"
    assert len(summary) == 1, f"只应有一个子账号，实际 {summary}"
    row = summary[0]
    assert row["sub_account_id"] == sub_a
    assert row["sub_account_name"] == "账房甲"
    assert row["debit_total"] == 200, "两笔 SETTLE 共 120+80"
    assert row["credit_total"] == 25, "一笔 RELEASE 退回 25"
    assert row["transaction_count"] == 3


def test_sub_account_summary_honours_the_active_filters(client, registration_dsn) -> None:
    """聚合摘要必须和它下方那张表用同一套筛选，否则两个数字对不上。"""
    master_headers, master = _master_session(client, _username("b3aggf"))
    sub_a = _create_sub(client, master_headers, _username("b3aggfa"), "账房乙")
    owner = master["user_id"]

    _seed_transaction(
        registration_dsn,
        tx_id="aggf-charge",
        owner_id=owner,
        kind="RESERVE",
        credits=300,
        actor_id=sub_a,
        created_at="2026-09-20 10:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="aggf-settle",
        owner_id=owner,
        kind="SETTLE",
        credits=40,
        actor_id=sub_a,
        created_at="2026-09-20 10:30:00+08",
    )

    # 只看 SETTLE：列表只应有一行，摘要也只能统计这一行。
    response = client.get(
        TRANSACTIONS_PATH,
        headers=master_headers,
        params={"group_by_sub_account": True, "transaction_type": "SETTLE"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _transaction_ids(response) == {"aggf-settle"}
    summary = body["sub_account_summary"]
    assert len(summary) == 1
    assert summary[0]["transaction_count"] == 1, "摘要忽略筛选就会把 CHARGE 也算进来"
    assert summary[0]["debit_total"] == 40


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------


def _parse_csv(text: str) -> list[list[str]]:
    # 文件开头的 BOM 是给 Excel 认 UTF-8 用的，解析时剥掉
    return [row for row in csv.reader(io.StringIO(text.lstrip("﻿"))) if row]


def test_csv_export_is_well_formed(client, registration_dsn) -> None:
    """导出的 CSV 必须是**表头与数据同分隔符、同列数**的合法 CSV。

    原实现表头用全角逗号（「时间，类型，金额…」）而数据行用半角逗号，用任何标准
    CSV 解析器读都会得到「表头 1 列 + 数据 10 列」的错位结果——客户拿它去对账
    直接是废的。同时末列把内部 actor_user_id 原样导出，客户既看不懂也不该看到。
    """
    master_headers, master = _master_session(client, _username("b3csv"))
    sub_a = _create_sub(client, master_headers, _username("b3csva"), "导出甲")
    owner = master["user_id"]
    _seed_transaction(
        registration_dsn,
        tx_id="csv-settle",
        owner_id=owner,
        kind="SETTLE",
        credits=66,
        actor_id=sub_a,
        created_at="2026-09-20 10:00:00+08",
    )

    response = client.get(EXPORT_PATH, headers=master_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = _parse_csv(response.text)
    assert rows, "导出不应是空文件"
    header, *data = rows
    assert data, "至少要有一行数据，否则测不出列数是否一致"
    assert len(header) == len(data[0]), (
        f"表头 {len(header)} 列、数据 {len(data[0])} 列——分隔符或列定义不一致：{header}"
    )
    assert all(column.strip() for column in header), f"表头有空列：{header}"
    assert not any("actor_user_id" in column for column in header), (
        f"内部操作者 id 不应出现在客户导出里：{header}"
    )
    assert len(data[0]) == len(header)


def test_csv_export_respects_the_sub_account_filter(client, registration_dsn) -> None:
    """导出同样支持按子账号过滤。"""
    master_headers, master = _master_session(client, _username("b3csvf"))
    sub_a = _create_sub(client, master_headers, _username("b3csvfa"), "导出乙")
    sub_b = _create_sub(client, master_headers, _username("b3csvfb"), "导出丙")
    owner = master["user_id"]

    _seed_transaction(
        registration_dsn,
        tx_id="csvf-a",
        owner_id=owner,
        kind="SETTLE",
        credits=11,
        actor_id=sub_a,
        created_at="2026-09-20 10:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="csvf-b",
        owner_id=owner,
        kind="SETTLE",
        credits=22,
        actor_id=sub_b,
        created_at="2026-09-20 10:01:00+08",
    )

    everything = _parse_csv(client.get(EXPORT_PATH, headers=master_headers).text)
    assert len(everything) == 3, "表头 + 两行数据"

    only_a = _parse_csv(
        client.get(EXPORT_PATH, headers=master_headers, params={"sub_account_id": sub_a}).text
    )
    assert len(only_a) == 2, "表头 + 一行数据"


# ---------------------------------------------------------------------------
# 近 N 天消费构成（方案 F / P1#10）
# ---------------------------------------------------------------------------


def test_consumption_by_business_groups_settled_rows(client, registration_dsn) -> None:
    """按业务汇总只算 SETTLE：预扣与退回都不进这份构成。"""
    master_headers, master = _master_session(client, _username("b3biz"))
    owner = master["user_id"]

    _seed_transaction(
        registration_dsn,
        tx_id="biz-asr-1",
        owner_id=owner,
        kind="SETTLE",
        credits=40,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
        service="asr",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="biz-asr-2",
        owner_id=owner,
        kind="SETTLE",
        credits=60,
        actor_id=None,
        created_at="2026-09-20 11:00:00+08",
        service="asr",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="biz-video",
        owner_id=owner,
        kind="SETTLE",
        credits=25,
        actor_id=None,
        created_at="2026-09-20 12:00:00+08",
        service="video_768p",
    )
    # 退回不是消费、预扣会随后被结算或退回——两者都不该出现在构成里
    _seed_transaction(
        registration_dsn,
        tx_id="biz-release",
        owner_id=owner,
        kind="RELEASE",
        credits=30,
        actor_id=None,
        created_at="2026-09-20 13:00:00+08",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="biz-reserve",
        owner_id=owner,
        kind="RESERVE",
        credits=99,
        actor_id=None,
        created_at="2026-09-20 14:00:00+08",
    )

    response = client.get(
        "/api/customer/wallet/consumption-by-business",
        headers=master_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 30
    assert body["total_credits"] == 125, f"只应有 SETTLE：{body}"
    # 业务键是客户口径的 12 类（SERVICE_FEATURE），不是计费科目名：
    # video_768p / video_2k 归成 video，前端才有一份中文名覆盖它
    # （此前返回原始科目名，消费构成把 "video_768p" 原样显示给客户）。
    assert body["items"] == [
        {"business": "asr", "credits": 100},
        {"business": "video", "credits": 25},
    ], f"按金额倒序：{body['items']}"


def test_consumption_by_business_folds_billing_subjects_into_business_keys(
    client, registration_dsn
) -> None:
    """同一业务的两档科目合成一项，且与筛选下拉的键同名。"""
    master_headers, master = _master_session(client, _username("b3bizfold"))
    owner = master["user_id"]
    _seed_transaction(
        registration_dsn,
        tx_id="bizfold-768",
        owner_id=owner,
        kind="SETTLE",
        credits=25,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
        service="video_768p",
    )
    _seed_transaction(
        registration_dsn,
        tx_id="bizfold-2k",
        owner_id=owner,
        kind="SETTLE",
        credits=75,
        actor_id=None,
        created_at="2026-09-20 11:00:00+08",
        service="video_2k",
    )

    body = client.get(
        "/api/customer/wallet/consumption-by-business",
        headers=master_headers,
    ).json()
    assert body["items"] == [{"business": "video", "credits": 100}], body["items"]


def test_consumption_by_business_requires_a_session(client) -> None:
    response = client.get("/api/customer/wallet/consumption-by-business")
    assert response.status_code == 401


def test_consumption_by_business_excludes_rows_outside_the_window(client, registration_dsn) -> None:
    """窗口外的消费不计入（days 越小越明显）。"""
    master_headers, master = _master_session(client, _username("b3bizw"))
    owner = master["user_id"]
    _seed_transaction(
        registration_dsn,
        tx_id="biz-old",
        owner_id=owner,
        kind="SETTLE",
        credits=50,
        actor_id=None,
        created_at="2020-01-01 10:00:00+08",
    )

    windowed = client.get(
        "/api/customer/wallet/consumption-by-business",
        headers=master_headers,
        params={"days": 30},
    )
    assert windowed.status_code == 200, windowed.text
    assert windowed.json()["items"] == []


# ---------------------------------------------------------------------------
# 代码评审发现的回归锁（2026-09-22）
# ---------------------------------------------------------------------------


def test_csv_export_and_list_agree_on_the_business_filter(client, registration_dsn) -> None:
    """导出「视频生成」必须和列表一样有行。

    此前导出查 ``bo.service = 'video'``（目录里根本没有这个 service）而列表查
    ``task_id IS NOT NULL OR service IN (video_768p, video_2k)``——客户选「视频生成」
    导出得到只有表头的空文件，界面上明明有行。
    """
    master_headers, master = _master_session(client, _username("b3csvbiz"))
    _seed_transaction(
        registration_dsn,
        tx_id="csvbiz-video",
        owner_id=master["user_id"],
        kind="SETTLE",
        credits=42,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
        service="video_768p",
    )

    listed = client.get(TRANSACTIONS_PATH, headers=master_headers, params={"business": "video"})
    exported = _parse_csv(
        client.get(EXPORT_PATH, headers=master_headers, params={"business": "video"}).text
    )
    assert listed.json()["total"] == 1, listed.text
    assert len(exported) == 2, f"表头 + 1 行；实际 {exported}"


def test_csv_export_maps_historical_like_the_list(client, registration_dsn) -> None:
    """``auth_source=historical`` 在导出侧同样映射成 ``IS NULL``（等值比较永远空）。"""
    master_headers, master = _master_session(client, _username("b3csvh"))
    _seed_transaction(
        registration_dsn,
        tx_id="csvh-old",
        owner_id=master["user_id"],
        kind="SETTLE",
        credits=7,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
    )

    exported = _parse_csv(
        client.get(EXPORT_PATH, headers=master_headers, params={"auth_source": "historical"}).text
    )
    assert len(exported) == 2, f"早期版本消费应导得出：{exported}"


def test_csv_carries_both_ledger_columns(client, registration_dsn) -> None:
    """SETTLE 的金额记在「待结算变化」上；只导 ``available_delta`` 会让每笔消费显示 0。"""
    master_headers, master = _master_session(client, _username("b3csvcol"))
    _seed_transaction(
        registration_dsn,
        tx_id="csvcol-settle",
        owner_id=master["user_id"],
        kind="SETTLE",
        credits=66,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
    )

    header, row = _parse_csv(client.get(EXPORT_PATH, headers=master_headers).text)[:2]
    assert header[2] == "可用积分变化" and header[3] == "待结算变化", header
    assert row[2] == "0", row
    assert row[3] == "-66", row


def test_csv_cells_are_spreadsheet_safe() -> None:
    """用户可控单元格以 = + - @ 开头时加前缀，防止 Excel/WPS 执行公式。"""
    from app.recharge_routes import _csv_record

    line = _csv_record(
        ("=cmd|'/c calc'!A1", "+1+1", "-2", "@x", "正常名称", 42),
        spreadsheet_safe=True,
    ).decode("utf-8")
    cells = next(csv.reader(io.StringIO(line)))
    for index in range(4):
        assert cells[index].startswith("'"), cells
    assert cells[4] == "正常名称" and cells[5] == "42"


def test_csv_export_signals_truncation(client, registration_dsn) -> None:
    """行数撞到上限时给出截断信号（客户否则以为拿到的是全量账）。"""
    master_headers, master = _master_session(client, _username("b3csvtrunc"))
    _seed_transaction(
        registration_dsn,
        tx_id="csvtrunc-1",
        owner_id=master["user_id"],
        kind="SETTLE",
        credits=5,
        actor_id=None,
        created_at="2026-09-20 10:00:00+08",
    )

    capped = client.get(EXPORT_PATH, headers=master_headers, params={"limit": 1})
    assert capped.headers["X-Export-Truncated"] == "true"

    full = client.get(EXPORT_PATH, headers=master_headers, params={"limit": 100})
    assert full.headers["X-Export-Truncated"] == "false"
