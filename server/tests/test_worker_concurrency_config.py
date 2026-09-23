"""并发改造的配置解析与对账节流（纯单元测试，不依赖数据库）。

覆盖本次改动中不涉及 SQL 的部分：

- ``_worker_concurrency``：CLI 优先 / 环境变量兜底 / 默认串行；
- ``_reconcile_interval_seconds``：默认周期与非法值回退；
- ``_reconcile_due``：进程内节流语义（这是把对账移出 worker 热路径的关键）；
- ``run_forever_pg``：``concurrency<=1`` 时必须走历史串行路径（向后兼容）。

涉及数据库的两处改动——对账候选 SQL 的放宽与加锁、R1 缺秒数兜底结算——
由 PG 套件覆盖，不在本文件的范围内。
"""

from __future__ import annotations

import os

# 必须在导入 app 模块前设置：审计写入依赖该密钥。
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-worker-concurrency-tests-minimum-48-bytes-long-0000",
)

import pytest

import app.generation_worker as generation_worker
from app.generation_worker import (
    _DEFAULT_RECONCILE_INTERVAL_SECONDS,
    _RECONCILE_INTERVAL_ENV,
    _WORKER_CONCURRENCY_ENV,
)


def test_worker_concurrency_defaults_to_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """既无 CLI 也无环境变量时默认串行，与改造前行为一致。"""
    monkeypatch.delenv(_WORKER_CONCURRENCY_ENV, raising=False)
    assert generation_worker._worker_concurrency() == 1


def test_worker_concurrency_prefers_cli_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WORKER_CONCURRENCY_ENV, "6")
    assert generation_worker._worker_concurrency(3) == 3


def test_worker_concurrency_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WORKER_CONCURRENCY_ENV, "4")
    assert generation_worker._worker_concurrency() == 4


def test_worker_concurrency_floors_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法值必须回退到 1，而不是让 worker 起不来。"""
    monkeypatch.setenv(_WORKER_CONCURRENCY_ENV, "not-a-number")
    assert generation_worker._worker_concurrency() == 1


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_worker_concurrency_never_below_one(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_WORKER_CONCURRENCY_ENV, raw)
    assert generation_worker._worker_concurrency() == 1


def test_reconcile_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_RECONCILE_INTERVAL_ENV, raising=False)
    assert generation_worker._reconcile_interval_seconds() == _DEFAULT_RECONCILE_INTERVAL_SECONDS


def test_reconcile_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_RECONCILE_INTERVAL_ENV, "5")
    assert generation_worker._reconcile_interval_seconds() == 5.0


def test_reconcile_interval_floors_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_RECONCILE_INTERVAL_ENV, "abc")
    assert generation_worker._reconcile_interval_seconds() == _DEFAULT_RECONCILE_INTERVAL_SECONDS


def test_reconcile_due_throttles_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """首次到点放行，窗口内不再放行——旧实现是每轮都跑。"""
    monkeypatch.setattr(generation_worker, "_reconcile_deadline", 0.0)
    monkeypatch.setenv(_RECONCILE_INTERVAL_ENV, "3600")
    assert generation_worker._reconcile_due() is True
    assert generation_worker._reconcile_due() is False


def test_reconcile_due_always_true_at_zero_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """周期 0 表示每轮都尝试，保留给排障使用。"""
    monkeypatch.setattr(generation_worker, "_reconcile_deadline", 0.0)
    monkeypatch.setenv(_RECONCILE_INTERVAL_ENV, "0")
    assert generation_worker._reconcile_due() is True
    assert generation_worker._reconcile_due() is True


def test_run_forever_pg_keeps_serial_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """concurrency<=1 必须走历史串行循环，不得改变既有部署行为。"""
    seen: list[tuple[str, float, bool]] = []

    def fake_serial(*, worker_id: str, idle_seconds: float, viral_collection: bool) -> None:
        seen.append((worker_id, idle_seconds, viral_collection))

    def fake_pool(**_: object) -> None:
        raise AssertionError("concurrency<=1 时不应进入线程池路径")

    monkeypatch.setattr(generation_worker, "_run_forever_serial", fake_serial)
    monkeypatch.setattr(generation_worker, "ThreadPoolExecutor", fake_pool)

    generation_worker.run_forever_pg(worker_id="w-1", idle_seconds=0.0, concurrency=1)
    generation_worker.run_forever_pg(worker_id="w-2", idle_seconds=0.0, concurrency=0)

    assert seen == [("w-1", 0.0, False), ("w-2", 0.0, False)]
