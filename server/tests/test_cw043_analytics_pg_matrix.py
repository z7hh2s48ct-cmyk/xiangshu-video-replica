"""CW-043 Segment 4+ — 数据看板 analytics 域 TEST-PG 矩阵.

CW-043 审计实施：``CW043-PG-COVERAGE-MATRIX`` §5.1.1 #8 把旧
``test_studio_analytics.py`` 的 analytics 全矩阵标为 ⚠️部分（旧文件仅 SQLite）。
本模块把该文件保留的持久化不变量移植到真实 PostgreSQL，复现 ``studio_analytics``
在 PG lane 上的聚合口径：

- 北京日界分桶（daily 序列覆盖整窗口，跨北京午夜的完成时刻归对日）
- 单次时钟读（generated_at 与窗口口径取同一 now，跨午夜不漂移）
- 任务类型分布（kind_breakdown 仅计可见完成项；普通生成 batch_id 去重）
- 最近成片（completed_at 倒序 + task_id 稳定序、属主范围、隐藏批反连接、
  按秒计费 cost_credits NULL 安全、20 条看板容量截断）
- days 窗口钳制（[1, 90]）

关键 PG-lane 事实（CW-043 审计确认）：核心表 ``updated_at`` 是 ``sa.Text()``
（迁移 065/067/070 的 ``_timestamps()``），两 dialect 皆 TEXT，psycopg 读回逐字
返回文本；``studio_analytics`` 的 ``updated_at::timestamptz >= %s::timestamptz``
在 SQLite lane 被 ``db_portable.translate_to_sqlite`` 改写为 ``datetime(...)``，在
PG lane 原样执行真 cast。裸文本按 session TimeZone 解释，故本模块
``SET TIME ZONE 'UTC'`` 使 PG 的 ``::timestamptz`` 比较与 SQLite ``datetime()``
语义一致（``studio_routes.py:27`` 亦声明 CURRENT_TIMESTAMP 两 dialect 皆写 UTC）。

不在本模块的旧用例：路由级 ``test_analytics_route_scopes_by_caller`` 经
``X-Dev-User-Id``——CW-026 收敛后 PG lane 只认客户会话 Bearer，X-Dev-User-Id 在
PG 上不可达（``auth.py:101``）；analytics 的属主范围不变量已由下列函数级用例在
真实 PG 上覆盖（employee/customer/other-employee 三视角）。纯
``studio_task_stats`` 可见性口径已由
``test_cw058_content_asset_pg_matrix.py::test_studio_task_stats_scope_and_hidden_batch_on_real_pg``
覆盖；本模块仅在 analytics 响应层复用其 today_completed/total_completed 聚合
（含口播计入，见 test_daily_series 的 today_completed == 6）。

专属隔离库 ``cw043_analytics_test`` 已登记 ``pg_test_kit.RECORDED_TEST_DATABASES``；
零 SQLite 替代、零缺库 skip（缺 PG 即硬失败，PG-05）；用例间 TRUNCATE 隔离。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app import studio_routes
from app.auth import CurrentUser
from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.studio_routes import studio_analytics

CW043_ANALYTICS_TEST_DB = "cw043_analytics_test"

# 2026-09-06 12:00 UTC = 2026-09-06 20:00 北京；固定“当前时间”防跨天漂移。
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

# 北京日界两侧的完成时刻（TEXT 时间戳，与 CURRENT_TIMESTAMP 写入口径一致）：
#   2026-09-05 15:59 UTC = 09-05 23:59 北京 → 属于 09-05
#   2026-09-05 16:01 UTC = 09-06 00:01 北京 → 属于 09-06
_TODAY = "2026-09-06 03:00:00"
_YESTERDAY_BEIJING = "2026-09-05T15:59:00+00:00"
_TODAY_BEIJING_EARLY = "2026-09-05T09:01:00-07:00"
_INDEPENDENT_COMPLETED = "2026-09-06 02:00:00"
_OUT_OF_RANGE = "2026-08-30 00:00:00"


def actor(user_id: str, role: str) -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def _executemany(pg: psycopg.Connection, sql: str, rows: list[tuple]) -> None:
    """psycopg3 的 ``executemany`` 在 Cursor 上（Connection 无此方法）."""
    with pg.cursor() as cursor:
        cursor.executemany(sql, rows)


# ---------------------------------------------------------------------------
# 座子：专属库 + SET TIME ZONE 'UTC' + TRUNCATE 隔离 + 生产 PG 通道
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cw043_dsn() -> Iterator[str]:
    """专属 analytics 域测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW043_ANALYTICS_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW043_ANALYTICS_TEST_DB)


_TRUNCATED_TABLES = (
    "users, projects, generation_batches, generation_tasks, "
    "customer_batch_visibility, person_identities, oral_avatars, oral_tasks, "
    "wallet_transactions, wallets"
)


@pytest.fixture()
def pg(cw043_dsn: str) -> Iterator[psycopg.Connection]:
    """autocommit 原生连接：负责播种与断言读取；用例间 TRUNCATE 隔离.

    ``SET TIME ZONE 'UTC'`` 使 ``updated_at::timestamptz`` 对裸文本按 UTC 解释，
    与 SQLite lane 的 ``datetime()`` 语义一致（见模块 docstring）。append-only
    审计表的 TRUNCATE 拒绝触发器在 ``session_replication_role = replica`` 下不触发
    （专属 allowlist 测试库内的用例隔离，不影响任何共享库）。
    """
    close_pg_pool()
    conn = psycopg.connect(cw043_dsn, autocommit=True)
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET session_replication_role = replica")
    conn.execute(f"TRUNCATE {_TRUNCATED_TABLES} CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("admin_1", "admin_1", "Admin One", "admin"),
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
def bus(pg: psycopg.Connection) -> BusinessConnection:
    """autocommit 业务门面：与生产 autocommit 池连接同形."""
    return BusinessConnection.postgres(pg)


def _seed_analytics_scene(pg: psycopg.Connection) -> None:
    """``test_studio_analytics.seed_analytics_scene`` 的 PG 移植（5 批 + 12 生成
    任务 + 4 口播任务 + 按秒计费账本）.

    TEXT 时间戳逐字入库（``updated_at`` 列为 ``sa.Text()``），与旧 SQLite 场景同形；
    ``oral_avatars.source_asset_id`` / ``oral_tasks.result_asset_id`` 是无 FK 的纯
    ``Text``，故用裸串即可（不同于 cw058 asset-access 用例需真 assets 行）。
    ``generation_batches.creation_kind`` NOT NULL（迁移 066），显式写入以驱动
    kind_breakdown。
    """
    _executemany(
        pg,
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        [
            ("p-1", "employee_1", "庭院项目"),
            ("p-2", "employee_2", "邻家项目"),
        ],
    )
    _executemany(
        pg,
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status, creation_kind, "
        "display_name, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, '{}', 'SUCCEEDED', %s, %s, %s, %s)",
        [
            ("b-1", "p-1", "employee_1", "ik-1", "h-1", "replica", None, _TODAY, _TODAY),
            ("b-2", "p-1", "employee_1", "ik-2", "h-2", "independent", None, _TODAY, _TODAY),
            ("b-3", "p-2", "employee_2", "ik-3", "h-3", "replacement", None, _TODAY, _TODAY),
            ("b-hidden", "p-1", "employee_1", "ik-4", "h-4", "replica", None, _TODAY, _TODAY),
            (
                "b-independent",
                None,
                "employee_1",
                "ik-independent",
                "h-independent",
                "independent",
                "无项目独立创作",
                _INDEPENDENT_COMPLETED,
                _INDEPENDENT_COMPLETED,
            ),
        ],
    )
    _executemany(
        pg,
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
        "archive_status, superseded_by_task_id, created_at, updated_at) "
        "VALUES (%s, %s, 'metaso', 'MiniMax-H3', %s, 'NONE', %s, %s, %s)",
        [
            ("t1", "b-1", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t2", "b-2", "SUCCEEDED", None, _TODAY, _TODAY_BEIJING_EARLY),
            ("t3", "b-1", "SUCCEEDED", None, _TODAY, _YESTERDAY_BEIJING),
            ("t4", "b-3", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t5-failed", "b-1", "FAILED", None, _TODAY, _TODAY),
            ("t6-superseded", "b-1", "SUCCEEDED", "t1", _TODAY, _TODAY),
            ("t7-hidden", "b-hidden", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t8-running", "b-1", "RUNNING", None, _TODAY, _TODAY),
            ("t9-old", "b-2", "SUCCEEDED", None, _OUT_OF_RANGE, _OUT_OF_RANGE),
            ("t10-submitting", "b-1", "SUBMITTING", None, _TODAY, _TODAY),
            ("t11-archiving", "b-1", "ARCHIVING", None, _TODAY, _TODAY),
            (
                "t-independent",
                "b-independent",
                "SUCCEEDED",
                None,
                _INDEPENDENT_COMPLETED,
                _INDEPENDENT_COMPLETED,
            ),
        ],
    )
    pg.execute(
        "INSERT INTO customer_batch_visibility (user_id, batch_id) VALUES (%s, %s)",
        ("employee_1", "b-hidden"),
    )
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
        "VALUES ('analytics-identity', 'employee_1', '口播人物', 'ACTIVE')"
    )
    pg.execute(
        "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, status, "
        "source_kind, source_asset_id) VALUES ('analytics-avatar', 'analytics-identity', "
        "'employee_1', '口播分身', 'READY', 'IMAGE', 'analytics-source')"
    )
    _executemany(
        pg,
        "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, mode, "
        "title, status, estimated_cost_fen, idempotency_key, created_at, updated_at) "
        "VALUES (%s, 'employee_1', 'analytics-identity', 'analytics-avatar', 'TTS', "
        "%s, %s, 350, %s, %s, %s)",
        [
            (
                "oral-succeeded",
                "院落介绍口播",
                "SUCCEEDED",
                "oral-key-success",
                _TODAY,
                _TODAY_BEIJING_EARLY,
            ),
            ("oral-failed", "失败口播", "FAILED", "oral-key-failed", _TODAY, _TODAY),
            ("oral-queued", "排队口播", "QUEUED", "oral-key-queued", _TODAY, _TODAY),
            ("oral-running", "处理中口播", "RUNNING", "oral-key-running", _TODAY, _TODAY),
        ],
    )
    # 按秒计费账本（057/073 形状约束：recharge_order_id NULL 时 task_id/oral_task_id
    # 恰一非空）：t1 预留 8 秒并成功结算 → 消耗 8 积分；其余成片无计费记录 →
    # cost_credits 为 null（不伪造）。SETTLE 行同存，验证 analytics 只取 type='RESERVE'。
    pg.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
        "VALUES ('employee_1', 100, 0)"
    )
    _executemany(
        pg,
        "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
        "reserved_delta, task_id, billing_round, idempotency_key) "
        "VALUES (%s, 'employee_1', %s, %s, %s, 't1', 1, %s)",
        [
            ("wt-reserve-t1", "RESERVE", -8, 8, "reserve:t1:1"),
            ("wt-settle-t1", "SETTLE", 0, -8, "settle:t1:1"),
        ],
    )


# ===========================================================================
# 每日桶 / 窗口计数（北京日界）
# ===========================================================================


def test_daily_series_covers_full_window_by_beijing_day_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_studio_analytics.py::test_daily_series_covers_full_window_by_beijing_day）：
    daily 序列覆盖整个北京日界窗口；跨午夜完成时刻归对日（t3 北京 09-05 23:59、
    t2/oral-succeeded 北京 09-06 00:01）；range/today/total 计数与去重批次数在真实
    PG 的 ``::timestamptz`` 窗口过滤下与旧 SQLite 一致。"""
    _seed_analytics_scene(pg)

    result = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=7)

    assert result.range_days == 7
    assert [day.day for day in result.daily] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
        "2026-09-06",
    ]
    # 09-05：t3（北京 23:59）；09-06：5 个普通成片 + 1 个成功口播；
    # 失败桶为 t5-failed + oral-failed。
    by_day = {day.day: day for day in result.daily}
    assert by_day["2026-09-05"].completed == 1
    assert by_day["2026-09-05"].failed == 0
    assert by_day["2026-09-06"].completed == 6
    assert by_day["2026-09-06"].failed == 2
    assert result.range_completed == 7
    assert result.today_completed == by_day["2026-09-06"].completed
    assert result.range_completed == sum(day.completed for day in result.daily)
    assert result.range_generation_outputs == 6
    assert result.range_oral_outputs == 1
    assert result.range_generation_batches == 5
    assert result.total_generation_batches == 5
    # t3 完成于北京 09-05，不计入“今日”；t9（08-30）计入全期累计。
    assert result.today_completed == 6
    assert result.total_completed == 8


def test_analytics_uses_one_clock_read_across_beijing_midnight_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧断言（…::test_analytics_uses_one_clock_read_across_beijing_midnight）：
    ``studio_analytics`` 全程只读一次 now（generated_at 与窗口口径同源），跨北京
    午夜不漂移。``datetime`` monkeypatch 与后端无关，在真实 PG 上同样成立。"""
    pg.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'admin_1', 'P')")
    pg.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status, creation_kind, "
        "created_at, updated_at) VALUES ('b-1', 'p-1', 'admin_1', 'ik', 'h', '{}', "
        "'SUCCEEDED', 'replica', %s, %s)",
        ("2026-09-05 15:59:59", "2026-09-05 15:59:59"),
    )
    pg.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
        "archive_status, created_at, updated_at) VALUES ('t-before', 'b-1', 'metaso', "
        "'MiniMax-H3', 'SUCCEEDED', 'NONE', '2026-09-05 15:59:59', '2026-09-05 15:59:59')"
    )

    clock_values = iter(
        [
            datetime(2026, 9, 5, 15, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 9, 5, 16, 0, 0, tzinfo=UTC),
        ]
    )

    class MidnightClock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 - datetime.now 同形
            value = next(clock_values)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(studio_routes, "datetime", MidnightClock)
    result = studio_analytics(bus, actor=actor("admin_1", "admin"), days=1)

    assert result.today_completed == 1
    assert result.range_completed == 1
    assert result.generated_at == "2026-09-05T15:59:59.999999+00:00"
    assert result.daily == [
        studio_routes.StudioAnalyticsDay(day="2026-09-05", completed=1, failed=0)
    ]
    # 只消费了一个时钟读数：第二个仍在迭代器里。
    assert next(clock_values) == datetime(2026, 9, 5, 16, 0, 0, tzinfo=UTC)


# ===========================================================================
# 任务类型分布 / 批次去重 / 属主范围
# ===========================================================================


def test_kind_breakdown_counts_only_visible_completed_tasks_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（…::test_kind_breakdown_counts_only_visible_completed_tasks）：
    kind_breakdown 仅计可见完成项；普通生成 batch_id 去重（b-1 两个产出项只计一次
    批次）；employee/customer 同口径、他员工不见无项目独立批。"""
    _seed_analytics_scene(pg)

    admin_view = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=7)
    assert {(item.kind, item.completed) for item in admin_view.kind_breakdown} == {
        ("replica", 3),
        ("independent", 2),
        ("replacement", 1),
        ("oral", 1),
    }

    employee_view = studio_analytics(bus, actor=actor("employee_1", "employee"), now=_NOW, days=7)
    # employee_1 可见 p-1 与自己创建的无项目批次，且 b-hidden 被本人隐藏。
    assert {(item.kind, item.completed) for item in employee_view.kind_breakdown} == {
        ("replica", 2),
        ("independent", 2),
        ("oral", 1),
    }
    # 普通生成 b-1 有两个产出项，但批次只计一次；b-2 为另一个批次。
    assert employee_view.range_generation_batches == 3
    assert employee_view.total_generation_batches == 3
    assert employee_view.range_generation_outputs == 4
    assert employee_view.range_oral_outputs == 1
    assert employee_view.range_completed == 5
    assert employee_view.total_completed == 6

    customer_view = studio_analytics(bus, actor=actor("employee_1", "customer"), now=_NOW, days=7)
    assert customer_view == employee_view

    other_employee = studio_analytics(bus, actor=actor("employee_2", "employee"), now=_NOW, days=7)
    assert all(work.batch_id != "b-independent" for work in other_employee.recent_works)


def test_recent_works_sorted_scoped_and_capped_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（…::test_recent_works_sorted_scoped_and_capped）：recent_works 按
    completed_at 倒序、同一时刻按 task_id 稳定序；t1 有 RESERVE 计费 → cost_credits
    为 8，口播/无计费成片为 null；口播 batch_id/project_id 皆 null；无项目独立批
    title 回退 display_name；employee 视角隐藏批与他人项目任务不出现。"""
    _seed_analytics_scene(pg)

    admin_view = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=7)
    assert [work.task_id for work in admin_view.recent_works] == [
        "t1",
        "t4",
        "t7-hidden",
        "t-independent",
        "oral-succeeded",
        "t2",
        "t3",
    ]
    first = admin_view.recent_works[0]
    assert (first.title, first.creation_kind, first.project_id) == (
        "庭院项目",
        "replica",
        "p-1",
    )
    # updated_at 是 TEXT 列，psycopg 逐字返回 → 与旧 SQLite 同形。
    assert first.completed_at == _TODAY
    # t1 有按秒计费的 RESERVE 记录 → 消耗 8 积分；无计费记录的成片为 null。
    assert first.cost_credits == 8
    oral = next(work for work in admin_view.recent_works if work.task_id == "oral-succeeded")
    assert oral.task_kind == "oral"
    assert oral.batch_id is None
    assert oral.project_id is None
    assert oral.cost_credits is None
    independent = next(work for work in admin_view.recent_works if work.task_id == "t-independent")
    assert independent.project_id is None
    assert independent.batch_id == "b-independent"
    assert independent.title == "无项目独立创作"

    employee_view = studio_analytics(bus, actor=actor("employee_1", "employee"), now=_NOW, days=7)
    # 隐藏批次与他人的 p-2 任务都不出现。
    assert [work.task_id for work in employee_view.recent_works] == [
        "t1",
        "t-independent",
        "oral-succeeded",
        "t2",
        "t3",
    ]


def test_recent_works_cap_at_twenty_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（…::test_recent_works_cap_at_twenty）：22 个成片全部计入 range 计数，
    但 recent_works 截断到看板容量 20，且首项为完成时刻最新的 t-21。"""
    pg.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'admin_1', 'P')")
    pg.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status, creation_kind, "
        "created_at, updated_at) VALUES ('b-1', 'p-1', 'admin_1', 'ik', 'h', '{}', "
        "'SUCCEEDED', 'replica', %s, %s)",
        (_TODAY, _TODAY),
    )
    _executemany(
        pg,
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
        "archive_status, created_at, updated_at) VALUES (%s, 'b-1', 'metaso', "
        "'MiniMax-H3', 'SUCCEEDED', 'NONE', %s, %s)",
        [(f"t-{index:02d}", _TODAY, f"2026-09-06 01:{index:02d}:00") for index in range(22)],
    )

    result = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=7)

    assert result.range_completed == 22
    assert result.range_generation_batches == 1
    assert len(result.recent_works) == 20
    assert result.recent_works[0].task_id == "t-21"


def test_days_window_is_clamped_on_real_pg(bus: BusinessConnection, pg: psycopg.Connection) -> None:
    """旧断言（…::test_days_window_is_clamped）：days 钳制到 [1, 90]；daily 序列长度
    随钳制值；30 天窗口外的 t9（08-30）在 90 天窗口内计入 range_completed。"""
    _seed_analytics_scene(pg)

    one_day = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=0)
    assert one_day.range_days == 1
    assert len(one_day.daily) == 1
    assert one_day.daily[0].day == "2026-09-06"

    ninety_days = studio_analytics(bus, actor=actor("admin_1", "admin"), now=_NOW, days=999)
    assert ninety_days.range_days == 90
    assert len(ninety_days.daily) == 90
    # 30 天窗口外的 t9（08-30）在 90 天窗口内计入。
    assert ninety_days.range_completed == 8
