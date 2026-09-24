from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

from app.db_portable import BusinessConnection
from app.operation_costs import (
    begin_operation_cost,
    complete_operation_cost,
    record_video_generation_cost,
    record_video_generation_not_called,
    snapshot_generation_rates,
)
from app.storage import FakeStorageAdapter

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
DB_NAME = "operation_costs_test"


@pytest.mark.parametrize(
    "mode", ["success", "quality_failure", "checkpoint_resume", "provider_retry", "receipt_resume"]
)
def test_first_frame_worker_cost_tracks_provider_output_before_checkpoint(
    cost_dsn: str, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from app import generation_worker as worker

    task_id = f"frame-cost-{uuid.uuid4()}"
    user_id = f"frame-owner-{uuid.uuid4()}"
    with psycopg.connect(cost_dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s,%s,'QA')", (user_id, user_id)
        )

    @contextmanager
    def transaction():
        with psycopg.connect(cost_dsn) as conn:
            yield conn

    monkeypatch.setattr(worker, "pg_transaction", transaction)
    for name in (
        "acquire_generation_continuation_lease",
        "acquire_generation_task_lease",
        "acquire_character_generation_task",
        "acquire_analysis_task",
        "acquire_script_rewrite_task",
        "acquire_generation_reconcile_operation",
        "_run_pg_source_frame_once",
    ):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: None)
    lease = SimpleNamespace(id=task_id, attempt=2, created_by_user_id=user_id)
    monkeypatch.setattr(worker, "acquire_first_frame_task", lambda *args, **kwargs: lease)
    provider = SimpleNamespace(provider_name="fake")
    receipt = None
    if mode == "receipt_resume":
        with psycopg.connect(cost_dsn) as conn:
            cost_id = begin_operation_cost(
                BusinessConnection.postgres(conn),
                source_type="first_frame_task",
                source_id=f"{task_id}:1:1",
                subject="first_frame_image",
                user_id=user_id,
            )
        receipt = {"cost_record_id": cost_id}
    prepared = SimpleNamespace(
        provider=provider, plan=SimpleNamespace(model="gpt-image-2"), provider_submission=receipt
    )
    monkeypatch.setattr(worker, "prepare_first_frame_task", lambda *args, **kwargs: prepared)
    for name in ("record_image_task_provider", "complete_first_frame_task", "fail_image_task"):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: None)

    def generate(*args, **kwargs):
        if mode != "checkpoint_resume":
            if mode != "receipt_resume":
                kwargs["before_provider_call"]()
            if mode == "provider_retry":
                kwargs["before_provider_call"]()
            kwargs["on_generated_images"](2)
            if mode == "quality_failure":
                raise RuntimeError("quality inspection failed after paid images were archived")
        # Checkpointed images are already stored: new file count is zero.
        return SimpleNamespace(), SimpleNamespace(created_assets=[])

    monkeypatch.setattr(worker, "run_first_frame_task_outside_transaction", generate)
    assert (
        worker.run_pg_worker_once(
            worker_id="cost-qa",
            storage=FakeStorageAdapter(provider="cos", bucket="qa"),
            image_provider=provider,
            first_frame_quality_inspector=SimpleNamespace(),
            max_tasks=1,
        )
        == 1
    )
    with psycopg.connect(cost_dsn) as conn:
        rows = conn.execute(
            "SELECT status, usage_amount, cost_fen FROM operation_cost_records "
            "WHERE source_type='first_frame_task' AND source_id LIKE %s ORDER BY source_id",
            (f"{task_id}%",),
        ).fetchall()
    expected = [] if mode == "checkpoint_resume" else [("ACTUAL", 2, 10)]
    if mode == "provider_retry":
        expected.insert(0, ("UNKNOWN", None, None))
    assert rows == expected


def _dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.mark.parametrize("kind", ["analysis", "character_sheet"])
def test_worker_keeps_known_cost_when_local_publication_fails(
    cost_dsn: str, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from app import generation_worker as worker

    task_id = f"publication-cost-{uuid.uuid4()}"
    user_id = f"publication-owner-{uuid.uuid4()}"
    with psycopg.connect(cost_dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s,%s,'QA')", (user_id, user_id)
        )

    @contextmanager
    def transaction():
        with psycopg.connect(cost_dsn) as conn:
            yield conn

    monkeypatch.setattr(worker, "pg_transaction", transaction)
    for name in (
        "acquire_generation_continuation_lease",
        "acquire_generation_task_lease",
        "acquire_character_generation_task",
        "acquire_analysis_task",
        "acquire_script_rewrite_task",
        "acquire_generation_reconcile_operation",
        "acquire_first_frame_task",
        "_run_pg_source_frame_once",
    ):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: None)
    lease = SimpleNamespace(id=task_id, created_by_user_id=user_id, duration_seconds=4, attempt=1)
    monkeypatch.setattr(worker, f"acquire_{kind}_task", lambda *args, **kwargs: lease)
    provider = SimpleNamespace(provider_name="fake")
    prepared = SimpleNamespace(provider=provider)
    monkeypatch.setattr(worker, f"prepare_{kind}_task", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(worker, f"perform_{kind}_task", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(worker, "record_image_task_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "fail_analysis_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "fail_image_task", lambda *args, **kwargs: None)

    def fail_publication(*args, **kwargs):
        raise RuntimeError("local publication failed after the provider returned")

    monkeypatch.setattr(worker, f"complete_{kind}_task", fail_publication)
    assert (
        worker.run_pg_worker_once(
            worker_id="publication-qa",
            storage=FakeStorageAdapter(provider="cos", bucket="qa"),
            image_provider=provider,
            max_tasks=1,
        )
        == 1
    )
    with psycopg.connect(cost_dsn) as conn:
        row = conn.execute(
            "SELECT status, usage_amount, cost_fen FROM operation_cost_records WHERE source_id=%s",
            (f"{task_id}:1",),
        ).fetchone()
    assert row == (("ACTUAL", 1, 9) if kind == "analysis" else ("ACTUAL", 1, 5))


def _database_dsn(name: str) -> str:
    return _dsn().rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture(scope="module")
def cost_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    try:
        with psycopg.connect(_dsn(), connect_timeout=3):
            pass
    except Exception:
        require_pg_or_explicit_skip()

    admin_dsn = _database_dsn("postgres")
    target_dsn = _database_dsn(DB_NAME)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", target_dsn.replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(target_dsn) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) VALUES "
            "('video_768p',true,12,9),('video_2k',true,20,15),('analysis',false,NULL,9),"
            "('first_frame',false,NULL,5),('character',false,NULL,5)"
        )
        raw.execute(
            "UPDATE customer_credit_pricing SET config_json=%s", ('{"points_per_yuan":100}',)
        )
    try:
        yield target_dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def _seed_generation_task(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User One')")
    conn.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'P1')")
    conn.execute(
        "INSERT INTO generation_batches "
        "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
        "request_snapshot_json) "
        "VALUES ('b1', 'p1', 'u1', 'idem-b1', 'hash-b1', '{}')"
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model, billed_seconds) "
        "VALUES ('t1', 'b1', 'metaso', 'MiniMax-H3', 15)"
    )


def test_generation_cost_uses_submission_rate_and_real_output_seconds(cost_dsn: str) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        _seed_generation_task(raw)
        conn = BusinessConnection.postgres(raw)
        snapshot = snapshot_generation_rates(
            conn,
            task_id="t1",
            resolution="768P",
            billed_seconds=15,
        )
        assert snapshot.cost_unit_price_fen == 9
        assert snapshot.external_unit_price_fen == 12

        begin_operation_cost(
            conn,
            source_type="generation_task",
            source_id="t1",
            subject="video_generation_768p",
            generation_task_id="t1",
        )
        raw.execute("UPDATE billing_tariffs SET unit_cost_fen=99 WHERE service='video_768p'")
        record_video_generation_cost(conn, task_id="t1", output_seconds=12.5)

        task = raw.execute(
            "SELECT actual_output_seconds, actual_cost, cost_status "
            "FROM generation_tasks WHERE id = 't1'"
        ).fetchone()
        assert float(task["actual_output_seconds"]) == 12.5
        assert float(task["actual_cost"]) == pytest.approx(1.125)
        assert task["cost_status"] == "ACTUAL"

        record = raw.execute(
            "SELECT usage_amount, unit_price_fen, cost_fen, status "
            "FROM operation_cost_records WHERE source_type = 'generation_task' "
            "AND source_id = 't1' AND subject = 'video_generation_768p'"
        ).fetchone()
        assert float(record["usage_amount"]) == 12.5
        assert record["unit_price_fen"] == 9
        assert float(record["cost_fen"]) == pytest.approx(112.5)
        assert record["status"] == "ACTUAL"
        context_ir = raw.execute(
            "SELECT status, usage_amount FROM operation_cost_records "
            "WHERE source_type = 'generation_task' AND source_id = 't1' "
            "AND subject = 'context_ir'"
        ).fetchone()
        assert context_ir is None  # No supplier request was made for this placeholder subject.


def test_missing_provider_usage_is_recorded_as_unknown(cost_dsn: str) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        raw.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, billed_seconds) "
            "VALUES ('t2', 'b1', 'metaso', 'MiniMax-H3', 4)"
        )
        conn = BusinessConnection.postgres(raw)
        snapshot_generation_rates(conn, task_id="t2", resolution="2K", billed_seconds=4)
        record_video_generation_cost(conn, task_id="t2", output_seconds=None)

        task = raw.execute(
            "SELECT actual_output_seconds, actual_cost, cost_status "
            "FROM generation_tasks WHERE id = 't2'"
        ).fetchone()
        assert task["actual_output_seconds"] is None
        assert task["actual_cost"] is None
        assert task["cost_status"] == "UNKNOWN"


def test_provider_not_called_closes_snapshots_as_known_zero(cost_dsn: str) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        raw.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, billed_seconds) "
            "VALUES ('t_not_called', 'b1', 'metaso', 'MiniMax-H3', 4)"
        )
        conn = BusinessConnection.postgres(raw)
        snapshot_generation_rates(conn, task_id="t_not_called", resolution="768P", billed_seconds=4)

        record_video_generation_not_called(conn, task_id="t_not_called")
        record_video_generation_not_called(conn, task_id="t_not_called")

        task = raw.execute(
            "SELECT actual_output_seconds, actual_cost, cost_status "
            "FROM generation_tasks WHERE id = 't_not_called'"
        ).fetchone()
        records = raw.execute(
            "SELECT subject, usage_amount, cost_fen, status "
            "FROM operation_cost_records WHERE source_id = 't_not_called' ORDER BY subject"
        ).fetchall()
        assert dict(task) == {
            "actual_output_seconds": 0,
            "actual_cost": 0,
            "cost_status": "ACTUAL",
        }
        assert records == []  # Preflight without a provider request creates no cost attempt.


def test_pre_migration_task_without_snapshot_is_not_priced_at_current_rate(
    cost_dsn: str,
) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        raw.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, billed_seconds) "
            "VALUES ('t3', 'b1', 'metaso', 'MiniMax-H3', 4)"
        )
        record_video_generation_cost(
            BusinessConnection.postgres(raw), task_id="t3", output_seconds=3.5
        )

        task = raw.execute(
            "SELECT actual_output_seconds, actual_cost, cost_status "
            "FROM generation_tasks WHERE id = 't3'"
        ).fetchone()
        assert float(task["actual_output_seconds"]) == 3.5
        assert task["actual_cost"] is None
        assert task["cost_status"] == "UNKNOWN"
        assert (
            raw.execute(
                "SELECT count(*) FROM operation_cost_records WHERE source_id = 't3'"
            ).fetchone()[0]
            == 0
        )


def test_generic_operation_cost_is_idempotent_and_uses_rate_snapshot(cost_dsn: str) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        conn = BusinessConnection.postgres(raw)
        record_id = begin_operation_cost(
            conn,
            source_type="analysis_task",
            source_id=f"analysis-{uuid.uuid4()}",
            subject="video_analysis_768p",
            user_id="u1",
            resolution="768P",
        )
        complete_operation_cost(conn, record_id=record_id, usage_amount=1)
        complete_operation_cost(conn, record_id=record_id, usage_amount=1)
        row = raw.execute(
            "SELECT unit_price_fen, usage_amount, cost_fen, status "
            "FROM operation_cost_records WHERE id = %s",
            (record_id,),
        ).fetchone()
        assert row["unit_price_fen"] == 9
        assert float(row["usage_amount"]) == 1
        assert float(row["cost_fen"]) == 9
        assert row["status"] == "ACTUAL"


def test_completed_operation_cost_rejects_conflicting_replay(cost_dsn: str) -> None:
    with psycopg.connect(cost_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as raw:
        conn = BusinessConnection.postgres(raw)
        record_id = begin_operation_cost(
            conn,
            source_type="analysis_task",
            source_id=f"analysis-conflict-{uuid.uuid4()}",
            subject="video_analysis_768p",
            resolution="768P",
        )
        complete_operation_cost(conn, record_id=record_id, usage_amount=8.25)

        with pytest.raises(RuntimeError, match="different result"):
            complete_operation_cost(conn, record_id=record_id, usage_amount=9)
        with pytest.raises(RuntimeError, match="different result"):
            complete_operation_cost(conn, record_id=record_id, usage_amount=None)


def test_operation_cost_migration_downgrade_roundtrip(cost_dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", cost_dsn.replace("postgresql://", "postgresql+psycopg://")
    )
    with psycopg.connect(cost_dsn, autocommit=True) as raw:
        assert (
            raw.execute("SELECT to_regclass('idx_wallet_transactions_type_created_at')").fetchone()[
                0
            ]
            is not None
        )
    command.downgrade(config, "058_daily_external_prices")
    try:
        with psycopg.connect(cost_dsn, autocommit=True) as raw:
            assert raw.execute("SELECT to_regclass('operation_cost_records')").fetchone()[0] is None
            assert (
                raw.execute(
                    "SELECT to_regclass('idx_wallet_transactions_type_created_at')"
                ).fetchone()[0]
                is None
            )
            columns = {
                row[0]
                for row in raw.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'generation_tasks'
                    """
                ).fetchall()
            }
            assert "actual_output_seconds" not in columns
            assert "cost_rate_subject_snapshot" not in columns
    finally:
        command.upgrade(config, "head")
