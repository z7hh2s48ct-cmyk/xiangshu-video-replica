"""T37 / OPS-02 — single-owner cluster anomaly probe contracts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def test_cluster_probe_covers_the_frozen_alert_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    counts = {
        "double_online": 1,
        "duplicate_charge": 2,
        "duplicate_provider_trade_no": 3,
        "paid_without_charge": 4,
        "charge_without_paid_source": 5,
        "wallet_balance_mismatch": 6,
        "fencing_rejected": 7,
        "stale_write_committed": 8,
        "cross_user_access": 9,
        "queue_starvation": 10,
        "dangling_reserve": 11,
    }
    observed_queries: list[tuple[str, tuple[object, ...]]] = []

    def fake_count(
        _conn: object,
        name: str,
        _query: str,
        parameters: tuple[object, ...],
    ) -> int:
        observed_queries.append((name, parameters))
        return counts[name]

    monkeypatch.setattr(alerts, "_count_query", fake_count)
    monkeypatch.setattr(
        alerts,
        "_server_now",
        lambda _conn: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    observations = alerts.collect_observations(object(), alerts.ProbeConfig(row_limit=100))

    assert {item.name: item.observed_count for item in observations} == counts
    assert {name for name, _parameters in observed_queries} == set(counts)
    assert all(item.active for item in observations)


def test_every_cluster_query_is_bounded_and_uses_only_application_tables() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    assert ALERT_QUERIES
    for name, query in ALERT_QUERIES.items():
        normalized = " ".join(query.lower().split())
        assert "limit %s" in normalized, name
        assert "pg_stat" not in normalized, name
        assert "pg_locks" not in normalized, name
        assert "information_schema" not in normalized, name


def test_double_online_uses_indexed_typed_event_timestamps() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["double_online"].lower().split())
    assert "group by user_id, session_id, session_epoch" in normalized
    assert "latest_login.session_epoch > heartbeat.session_epoch" in normalized
    assert "login.event = 'login'" in normalized
    assert "login.device_id = heartbeat.device_id" in normalized
    assert "heartbeat.occurred_at" in normalized
    assert "login.occurred_at" in normalized
    assert "created_at::timestamptz" not in normalized


def test_cross_user_alert_combines_success_mismatches_and_durable_denials() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["cross_user_access"].lower().split())
    assert "from audit_logs" in normalized
    assert "from customer_authorization_evidence" in normalized
    assert "actor_digest \u003c\u003e owner_digest" in normalized
    assert "security.project_denied" in normalized
    assert "security.asset_denied" in normalized
    assert "metadata_json" not in normalized
    assert "entity_id" not in normalized
    assert "occurred_at >= %s" in normalized
    assert "created_at::timestamptz" not in normalized


def test_probe_uses_typed_cutoffs_for_legacy_timestamp_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    observed_parameters: dict[str, tuple[object, ...]] = {}

    def fake_count(
        _conn: object,
        name: str,
        _query: str,
        parameters: tuple[object, ...],
    ) -> int:
        observed_parameters[name] = parameters
        return 0

    monkeypatch.setattr(alerts, "_count_query", fake_count)
    monkeypatch.setattr(
        alerts,
        "_server_now",
        lambda _conn: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    alerts.collect_observations(object(), alerts.ProbeConfig(fencing_window_seconds=300))

    assert observed_parameters["fencing_rejected"][0] == "2026-08-26T11:55:00+00:00"
    assert observed_parameters["double_online"][0] == datetime(2026, 8, 26, 11, 58, tzinfo=UTC)


def test_committed_stale_write_alert_uses_durable_epoch_evidence() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["stale_write_committed"].lower().split())
    assert "from customer_fencing_write_evidence" in normalized
    assert "expected_session_epoch <> verified_session_epoch" in normalized


def test_queue_starvation_starts_from_waiting_work_and_keeps_missing_cursors() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["queue_starvation"].lower().split())
    assert "left join user_queue_cursors" in normalized
    assert "cursor.user_id is null" in normalized
    assert normalized.index("task.status in ('pending', 'queued')") < normalized.index("limit %s")
    assert "task.created_at_utc" in normalized
    assert "task.created_at::timestamptz" not in normalized


def test_wallet_mismatch_filters_anomalies_before_candidate_limit() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["wallet_balance_mismatch"].lower().split())
    assert normalized.index("where wallet.available_credits") < normalized.index("limit %s")


def test_transition_engine_emits_only_fired_and_resolved_edges() -> None:
    from scripts.check_ops_alerts import AlertObservation, transition_alerts

    first = (
        AlertObservation("double_online", True, 1),
        AlertObservation("fencing_rejected", False, 0),
    )
    transitions, state = transition_alerts({}, first)
    assert [(item.name, item.transition) for item in transitions] == [("double_online", "fired")]
    assert state == {"double_online": True, "fencing_rejected": False}

    transitions, state = transition_alerts(state, first)
    assert transitions == ()

    recovered = (
        AlertObservation("double_online", False, 0),
        AlertObservation("fencing_rejected", True, 2),
    )
    transitions, state = transition_alerts(state, recovered)
    assert [(item.name, item.transition) for item in transitions] == [
        ("double_online", "resolved"),
        ("fencing_rejected", "fired"),
    ]
    assert state == {"double_online": False, "fencing_rejected": True}


def test_alert_state_is_loaded_and_saved_in_shared_postgresql() -> None:
    from scripts.check_ops_alerts import load_state, save_state

    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def fetchall(self) -> list[tuple[str, bool]]:
            return [("double_online", True), ("wallet_balance_mismatch", False)]

    class _Connection:
        def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Cursor:
            calls.append((" ".join(query.split()), parameters))
            return _Cursor()

    conn = _Connection()
    assert load_state(conn) == {
        "double_online": True,
        "wallet_balance_mismatch": False,
    }
    save_state(conn, {"double_online": False, "fencing_rejected": True})

    assert "FROM ops_alert_state" in calls[0][0]
    writes = calls[1:]
    assert len(writes) == 2
    assert all("INSERT INTO ops_alert_state" in query for query, _ in writes)
    assert all("ON CONFLICT (alert_name) DO UPDATE" in query for query, _ in writes)
    assert {parameters for _, parameters in writes} == {
        ("double_online", False),
        ("fencing_rejected", True),
    }


def test_alert_state_advances_only_after_every_transition_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    saved: list[dict[str, bool]] = []
    monkeypatch.setattr(
        alerts,
        "save_state",
        lambda _conn, state: saved.append(dict(state)),
    )

    class _Connection:
        commits = 0

        def commit(self) -> None:
            self.commits += 1

    conn = _Connection()
    result = alerts.ProbeResult(
        lines=("first-transition", "second-transition"),
        next_state={"double_online": True},
    )
    emitted: list[str] = []

    def fail_during_publish(line: str) -> None:
        emitted.append(line)
        if line == "second-transition":
            raise RuntimeError("receiver unavailable")

    with pytest.raises(RuntimeError, match="receiver unavailable"):
        alerts.publish_probe_result(conn, result, emit=fail_during_publish)

    assert emitted == ["first-transition", "second-transition"]
    assert saved == []
    assert conn.commits == 0

    alerts.publish_probe_result(conn, result, emit=lambda _line: None)
    assert saved == [{"double_online": True}]
    assert conn.commits == 1


def test_transition_output_never_contains_subjects_or_connection_details() -> None:
    from scripts.check_ops_alerts import AlertObservation, render_transition, transition_alerts

    transitions, _state = transition_alerts(
        {}, (AlertObservation("wallet_balance_mismatch", True, 1),)
    )
    payload = json.loads(
        render_transition(
            transitions[0],
            checked_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        )
    )

    assert payload == {
        "active": True,
        "alert": "wallet_balance_mismatch",
        "checked_at": "2026-08-26T12:00:00+00:00",
        "observed_count": 1,
        "severity": "P1",
        "transition": "fired",
    }
    rendered = json.dumps(payload)
    for forbidden in ("postgresql://", "password", "user_id", "device_id", "session_id"):
        assert forbidden not in rendered


def test_advisory_lock_loss_skips_without_collecting_or_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    class _Cursor:
        def fetchone(self) -> tuple[bool]:
            return (False,)

    class _Connection:
        def execute(self, _query: str, _parameters: tuple[object, ...]) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(
        alerts,
        "run_probe",
        lambda *_args, **_kwargs: pytest.fail("non-owner must not collect observations"),
    )
    assert alerts.run_owned_probe(_Connection(), emit=lambda _line: None) == ()


def test_all_cluster_queries_execute_on_a_fresh_pg16_database(tmp_path: Path) -> None:
    require_pg_or_explicit_skip(_pg_dsn())

    from alembic import command
    from alembic.config import Config

    from scripts import check_ops_alerts as alerts

    database_name = "t37_ops_alerts_test"
    admin_dsn = _pg_dsn().rsplit("/", 1)[0] + "/postgres"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{database_name}"
    server_dir = Path(__file__).resolve().parents[1]
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        dsn.replace("postgresql://", "postgresql+psycopg://"),
    )

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        command.upgrade(config, "head")
        with psycopg.connect(dsn) as conn:
            observations = alerts.collect_observations(conn)
            assert {item.name for item in observations} == set(alerts.ALERT_QUERIES)
            assert all(not item.active and item.observed_count == 0 for item in observations)
            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('old-fence-t37', 'session:fencing', 'old-digest', "
                "'old-request-t37', replace((clock_timestamp() - interval '1 hour')::text, "
                "' ', 'T'))"
            )
            conn.commit()
            expired = alerts.collect_observations(conn)
            assert (
                next(item for item in expired if item.name == "fencing_rejected").observed_count
                == 0
            )
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES "
                "('idle-cursor-t37', 'idle-cursor-t37', 'Idle cursor'), "
                "('starved-user-t37', 'starved-user-t37', 'Starved user'), "
                "('recent-wallet-t37', 'recent-wallet-t37', 'Recent wallet'), "
                "('old-bad-wallet-t37', 'old-bad-wallet-t37', 'Old bad wallet')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) "
                "VALUES ('starved-project-t37', 'starved-user-t37', 'Starvation probe')"
            )
            conn.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) VALUES "
                "('idle-cursor-t37', clock_timestamp() - interval '2 hours', 0), "
                "('starved-user-t37', clock_timestamp() - interval '1 hour', 0)"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
                "request_snapshot_json, status) VALUES "
                "('starved-batch-t37', 'starved-project-t37', 'starved-user-t37', "
                "'starved-key-t37', 'starved-hash-t37', '{}', 'QUEUED')"
            )
            conn.execute(
                "INSERT INTO generation_tasks "
                "(id, batch_id, provider, model, status, created_at, created_at_utc) VALUES "
                "('starved-task-t37', 'starved-batch-t37', 'fake_h3', 'MiniMax-H3', "
                "'PENDING', (clock_timestamp() - interval '1 hour')::text, "
                "clock_timestamp() - interval '1 hour')"
            )
            conn.execute(
                "INSERT INTO wallets "
                "(user_id, available_credits, reserved_credits, updated_at) VALUES "
                "('recent-wallet-t37', 0, 0, clock_timestamp()::text), "
                "('old-bad-wallet-t37', 1, 0, "
                "(clock_timestamp() - interval '2 hours')::text)"
            )
            conn.commit()
            capped = alerts.collect_observations(
                conn,
                alerts.ProbeConfig(row_limit=1, starvation_seconds=300),
            )
            assert (
                next(item for item in capped if item.name == "queue_starvation").observed_count == 1
            )
            assert (
                next(
                    item for item in capped if item.name == "wallet_balance_mismatch"
                ).observed_count
                == 1
            )
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES "
                "('missing-cursor-t37', 'missing-cursor-t37', 'Missing cursor')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES "
                "('missing-cursor-project-t37', 'missing-cursor-t37', "
                "'Missing cursor starvation probe')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
                "request_snapshot_json, status) VALUES "
                "('missing-cursor-batch-t37', 'missing-cursor-project-t37', "
                "'missing-cursor-t37', 'missing-cursor-key-t37', "
                "'missing-cursor-hash-t37', '{}', 'QUEUED')"
            )
            conn.execute(
                "INSERT INTO generation_tasks "
                "(id, batch_id, provider, model, status, created_at, created_at_utc) VALUES "
                "('missing-cursor-task-t37', 'missing-cursor-batch-t37', "
                "'fake_h3', 'MiniMax-H3', 'PENDING', "
                "(clock_timestamp() - interval '1 hour')::text, "
                "clock_timestamp() - interval '1 hour')"
            )
            conn.commit()
            missing_cursor = alerts.collect_observations(
                conn,
                alerts.ProbeConfig(row_limit=2, starvation_seconds=300),
            )
            assert (
                next(
                    item for item in missing_cursor if item.name == "queue_starvation"
                ).observed_count
                == 2
            )
            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('fence-t37', 'session:fencing', 'digest', "
                "'request-t37', replace(clock_timestamp()::text, ' ', 'T'))"
            )
            conn.execute(
                "INSERT INTO customer_fencing_write_evidence "
                "(id, request_id, subject_digest, expected_session_epoch, "
                "verified_session_epoch) VALUES "
                "('stale-write-t37', 'stale-write-request-t37', %s, 1, 2)",
                ("b" * 64,),
            )
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at) "
                "VALUES ('cross-user-t37', NULL, 'security.project_denied', "
                "'project', 'redacted-by-alert-query', '{}', clock_timestamp()::text)"
            )
            conn.execute(
                "INSERT INTO customer_authorization_evidence "
                "(id, request_id, resource_type, actor_digest, owner_digest) "
                "VALUES ('cross-user-success-t37', 'cross-user-success-request-t37', "
                "'project', %s, %s)",
                ("c" * 64, "d" * 64),
            )
            conn.commit()

        emitted: list[str] = []
        with psycopg.connect(dsn) as conn:
            assert alerts.run_owned_probe(conn, emit=emitted.append) == tuple(emitted)
        payloads = [json.loads(line) for line in emitted]
        assert any(
            item["alert"] == "fencing_rejected" and item["transition"] == "fired"
            for item in payloads
        )
        assert any(
            item["alert"] == "stale_write_committed" and item["transition"] == "fired"
            for item in payloads
        )
        assert any(
            item["alert"] == "cross_user_access" and item["transition"] == "fired"
            for item in payloads
        )
        with psycopg.connect(dsn) as conn:
            shared_state = alerts.load_state(conn)
            assert shared_state["fencing_rejected"] is True
            assert shared_state["stale_write_committed"] is True
            assert shared_state["cross_user_access"] is True
            staggered: list[str] = []
            assert alerts.run_owned_probe(conn, emit=staggered.append) == ()
            assert staggered == []
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
