"""T37 / OPS-02 — single-owner, bounded PostgreSQL anomaly probe.

The two API processes expose process-local metrics only. Cluster-global
business invariants are checked here by one systemd timer. A PostgreSQL
transaction advisory lock makes the single-owner property fail closed even
if the timer is accidentally enabled on more than one host.

Output is transition-only JSON (``fired`` / ``resolved``). It contains fixed
alert names and counts, never row identifiers, DSNs, tokens, signed URLs, or
provider responses. An external journal/metrics collector is still required
before gray release; this script is the repository-side alert source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, SupportsInt, cast

import psycopg

from app.db_pg import DatabaseMode, resolve_database_config, validate_customer_production

ADVISORY_LOCK_KEY = 0x56525433374F5053  # "VRT37OPS", signed-int64 safe.


@dataclass(frozen=True)
class ProbeConfig:
    row_limit: int = 100
    session_window_seconds: int = 120
    fencing_window_seconds: int = 300
    security_window_seconds: int = 300
    starvation_seconds: int = 300
    statement_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        for name, value in (
            ("row_limit", self.row_limit),
            ("session_window_seconds", self.session_window_seconds),
            ("fencing_window_seconds", self.fencing_window_seconds),
            ("security_window_seconds", self.security_window_seconds),
            ("starvation_seconds", self.starvation_seconds),
            ("statement_timeout_ms", self.statement_timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class AlertObservation:
    name: str
    active: bool
    observed_count: int

    def __init__(self, name: str, active: bool, observed_count: int) -> None:
        if not name or observed_count < 0:
            raise ValueError("alert observation is invalid")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "observed_count", observed_count)


@dataclass(frozen=True)
class AlertTransition:
    name: str
    transition: Literal["fired", "resolved"]
    active: bool
    observed_count: int


@dataclass(frozen=True)
class ProbeResult:
    """Transitions and the state that may advance only after publication."""

    lines: tuple[str, ...]
    next_state: dict[str, bool] | None


# Each query returns a single count over a capped anomaly subquery. The
# application role needs no pg_monitor / pg_stat_* privilege. Wallet and queue
# correlated checks happen before the anomaly cap so healthy or idle rows
# cannot hide a real P1 condition. Legacy event timestamps are TEXT, so every
# time comparison below coerces them to ``timestamptz``: PostgreSQL renders a
# TEXT ``CURRENT_TIMESTAMP`` in each writer session's timezone and lexical
# ordering would otherwise silently hide recent events from another offset.
ALERT_QUERIES: dict[str, str] = {
    "double_online": """
        SELECT count(*) FROM (
            SELECT user_id FROM (
                SELECT user_id, session_id, session_epoch
                FROM customer_session_events
                WHERE event = 'HEARTBEAT' AND occurred_at >= %s
                GROUP BY user_id, session_id, session_epoch
                HAVING count(DISTINCT device_id) > 1
                LIMIT %s
            ) AS same_epoch_devices
            UNION ALL
            SELECT user_id FROM (
                SELECT heartbeat.user_id
                FROM customer_session_events AS heartbeat
                JOIN LATERAL (
                    SELECT login.session_epoch
                    FROM customer_session_events AS login
                    WHERE login.user_id = heartbeat.user_id
                      AND login.device_id = heartbeat.device_id
                      AND login.event = 'LOGIN'
                      AND login.occurred_at <= heartbeat.occurred_at
                    ORDER BY login.occurred_at DESC, login.id DESC
                    LIMIT 1
                ) AS latest_login ON true
                WHERE heartbeat.event = 'HEARTBEAT'
                  AND heartbeat.occurred_at >= %s
                  AND latest_login.session_epoch > heartbeat.session_epoch
                ORDER BY heartbeat.occurred_at, heartbeat.id
                LIMIT %s
            ) AS displaced_epoch_heartbeats
            LIMIT %s
        ) AS anomalies
    """,
    "duplicate_charge": """
        SELECT count(*) FROM (
            SELECT recharge_order_id
            FROM wallet_transactions
            WHERE type = 'CHARGE' AND recharge_order_id IS NOT NULL
            GROUP BY recharge_order_id
            HAVING count(*) > 1
            LIMIT %s
        ) AS anomalies
    """,
    "duplicate_provider_trade_no": """
        SELECT count(*) FROM (
            SELECT provider_trade_no
            FROM recharge_orders
            WHERE provider_trade_no IS NOT NULL
            GROUP BY provider_trade_no
            HAVING count(*) > 1
            LIMIT %s
        ) AS anomalies
    """,
    "paid_without_charge": """
        SELECT count(*) FROM (
            SELECT orders.id
            FROM recharge_orders AS orders
            LEFT JOIN wallet_transactions AS charge
              ON charge.recharge_order_id = orders.id AND charge.type = 'CHARGE'
            WHERE orders.status = 'PAID' AND charge.id IS NULL
            ORDER BY orders.paid_at DESC NULLS LAST, orders.id
            LIMIT %s
        ) AS anomalies
    """,
    "charge_without_paid_source": """
        SELECT count(*) FROM (
            SELECT charge.id
            FROM wallet_transactions AS charge
            JOIN recharge_orders AS orders ON orders.id = charge.recharge_order_id
            WHERE charge.type = 'CHARGE'
              AND (
                orders.status <> 'PAID'
                OR orders.paid_at IS NULL
                OR orders.user_id <> charge.user_id
                OR orders.credits <> charge.available_delta
                OR charge.reserved_delta <> 0
              )
            ORDER BY charge.created_at, charge.id
            LIMIT %s
        ) AS anomalies
    """,
    "wallet_balance_mismatch": """
        SELECT count(*) FROM (
            SELECT wallet.user_id
            FROM wallets AS wallet
            LEFT JOIN LATERAL (
                SELECT
                    COALESCE(sum(tx.available_delta), 0) AS available_total,
                    COALESCE(sum(tx.reserved_delta), 0) AS reserved_total
                FROM wallet_transactions AS tx
                WHERE tx.user_id = wallet.user_id
            ) AS ledger ON true
            WHERE wallet.available_credits <> ledger.available_total
               OR wallet.reserved_credits <> ledger.reserved_total
            ORDER BY wallet.updated_at DESC, wallet.user_id
            LIMIT %s
        ) AS anomalies
    """,
    "fencing_rejected": """
        SELECT count(*) FROM (
            SELECT id
            FROM security_auth_failures
            WHERE dimension = 'session:fencing' AND occurred_at >= %s
            ORDER BY occurred_at DESC, id
            LIMIT %s
        ) AS anomalies
    """,
    "stale_write_committed": """
        SELECT count(*) FROM (
            SELECT id
            FROM customer_fencing_write_evidence
            WHERE expected_session_epoch <> verified_session_epoch
            ORDER BY committed_at, id
            LIMIT %s
        ) AS anomalies
    """,
    "cross_user_access": """
        SELECT count(*) FROM (
            SELECT id FROM (
                SELECT id
                FROM customer_authorization_evidence
                WHERE actor_digest <> owner_digest
                ORDER BY observed_at, id
                LIMIT %s
            ) AS successful_mismatches
            UNION ALL
            SELECT id FROM (
                SELECT id
                FROM audit_logs
                WHERE action IN ('security.project_denied', 'security.asset_denied')
                  AND occurred_at >= %s
                ORDER BY occurred_at DESC, id
                LIMIT %s
            ) AS denied_attempts
            LIMIT %s
        ) AS anomalies
    """,
    "queue_starvation": """
        WITH waiting_users AS MATERIALIZED (
            SELECT
                batch.created_by_user_id AS user_id,
                min(task.created_at_utc) AS oldest_pending_at
            FROM generation_batches AS batch
            JOIN generation_tasks AS task ON task.batch_id = batch.id
            WHERE task.status IN ('PENDING', 'QUEUED')
              AND task.created_at_utc < %s
            GROUP BY batch.created_by_user_id
        ), candidate_users AS MATERIALIZED (
            SELECT waiting.user_id
            FROM waiting_users AS waiting
            LEFT JOIN user_queue_cursors AS cursor ON cursor.user_id = waiting.user_id
            WHERE cursor.user_id IS NULL OR cursor.running_tasks_count = 0
            ORDER BY
                COALESCE(cursor.last_dispatched_at, waiting.oldest_pending_at),
                waiting.user_id
            LIMIT %s
        )
        SELECT count(*) FROM (
            SELECT user_id
            FROM candidate_users
            LIMIT %s
        ) AS anomalies
    """,
    "dangling_reserve": """
        SELECT count(*) FROM (
            SELECT reserve.id
            FROM wallet_transactions AS reserve
            JOIN generation_tasks AS task ON task.id = reserve.task_id
            WHERE reserve.type = 'RESERVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM wallet_transactions AS terminal
                  WHERE terminal.task_id = reserve.task_id
                    AND terminal.billing_round = reserve.billing_round
                    AND terminal.type IN ('SETTLE', 'RELEASE')
                  LIMIT 1
              )
              AND (
                  (task.status = 'SUCCEEDED' AND task.archive_status IN ('ARCHIVED', 'DIRECT'))
                  OR task.status IN ('FAILED', 'CANCELLED')
              )
            ORDER BY reserve.created_at, reserve.id
            LIMIT %s
        ) AS anomalies
    """,
}


def _server_now(conn: psycopg.Connection[tuple[object, ...]]) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL server clock is unavailable")
    value = row[0]
    current = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _count_query(
    conn: psycopg.Connection[tuple[object, ...]],
    name: str,
    query: str,
    parameters: tuple[object, ...],
) -> int:
    del name  # fixed call-site key is retained for tests/diagnostics only.
    row = conn.execute(query, parameters).fetchone()
    if row is None:
        raise RuntimeError("anomaly query returned no count")
    count = int(cast(SupportsInt, row[0]))
    if count < 0:
        raise RuntimeError("anomaly query returned an invalid count")
    return count


def _cutoff(current: datetime, seconds: int) -> datetime:
    """Return an aware cutoff for typed comparison with legacy TEXT facts."""
    return current - timedelta(seconds=seconds)


def _security_cutoff(current: datetime, seconds: int) -> str:
    # security_auth_failures facts are written with datetime.isoformat(), so
    # TEXT comparisons must retain the same ``T`` separator.
    return (current - timedelta(seconds=seconds)).isoformat()


def collect_observations(
    conn: psycopg.Connection[tuple[object, ...]],
    config: ProbeConfig | None = None,
) -> tuple[AlertObservation, ...]:
    config = config or ProbeConfig()
    current = _server_now(conn)
    parameters: dict[str, tuple[object, ...]] = {
        "double_online": (
            _cutoff(current, config.session_window_seconds),
            config.row_limit,
            _cutoff(current, config.session_window_seconds),
            config.row_limit,
            config.row_limit,
        ),
        "duplicate_charge": (config.row_limit,),
        "duplicate_provider_trade_no": (config.row_limit,),
        "paid_without_charge": (config.row_limit,),
        "charge_without_paid_source": (config.row_limit,),
        "wallet_balance_mismatch": (config.row_limit,),
        "fencing_rejected": (
            _security_cutoff(current, config.fencing_window_seconds),
            config.row_limit,
        ),
        "stale_write_committed": (config.row_limit,),
        "cross_user_access": (
            config.row_limit,
            _cutoff(current, config.security_window_seconds),
            config.row_limit,
            config.row_limit,
        ),
        "queue_starvation": (
            _cutoff(current, config.starvation_seconds),
            config.row_limit,
            config.row_limit,
        ),
        "dangling_reserve": (config.row_limit,),
    }
    observations: list[AlertObservation] = []
    for name, query in ALERT_QUERIES.items():
        count = _count_query(conn, name, query, parameters[name])
        observations.append(AlertObservation(name, count > 0, count))
    return tuple(observations)


def transition_alerts(
    previous: dict[str, bool],
    observations: tuple[AlertObservation, ...],
) -> tuple[tuple[AlertTransition, ...], dict[str, bool]]:
    transitions: list[AlertTransition] = []
    current: dict[str, bool] = {}
    for observation in observations:
        old_active = previous.get(observation.name, False)
        current[observation.name] = observation.active
        if observation.active and not old_active:
            transitions.append(
                AlertTransition(
                    observation.name,
                    "fired",
                    True,
                    observation.observed_count,
                )
            )
        elif old_active and not observation.active:
            transitions.append(AlertTransition(observation.name, "resolved", False, 0))
    return tuple(transitions), current


def load_state(conn: psycopg.Connection[tuple[object, ...]]) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT alert_name, active FROM ops_alert_state ORDER BY alert_name"
    ).fetchall()
    return {str(row[0]): bool(row[1]) for row in rows}


def save_state(conn: psycopg.Connection[tuple[object, ...]], state: dict[str, bool]) -> None:
    for name, active in sorted(state.items()):
        if name not in ALERT_QUERIES:
            raise RuntimeError("ops alert state contains an unknown alert")
        conn.execute(
            "INSERT INTO ops_alert_state (alert_name, active, updated_at) "
            "VALUES (%s, %s, clock_timestamp()) "
            "ON CONFLICT (alert_name) DO UPDATE "
            "SET active = EXCLUDED.active, updated_at = EXCLUDED.updated_at",
            (name, active),
        )


def render_transition(transition: AlertTransition, *, checked_at: datetime) -> str:
    payload = {
        "active": transition.active,
        "alert": transition.name,
        "checked_at": checked_at.astimezone(UTC).isoformat(),
        "observed_count": transition.observed_count,
        "severity": "P1",
        "transition": transition.transition,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def publish_probe_result(
    conn: psycopg.Connection[tuple[object, ...]],
    result: ProbeResult,
    *,
    emit: Callable[[str], None],
) -> None:
    """Publish every edge before acknowledging it in the durable state.

    A failed emitter leaves the previous state untouched, so the next probe
    retries the P1 transition. Duplicates are safer than permanently losing
    the only fired/resolved notification.
    """
    for line in result.lines:
        emit(line)
    if result.next_state is not None:
        save_state(conn, result.next_state)
        conn.commit()


def run_probe(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    config: ProbeConfig | None = None,
) -> ProbeResult:
    config = config or ProbeConfig()
    conn.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{config.statement_timeout_ms}ms",),
    )
    previous = load_state(conn)
    observations = collect_observations(conn, config)
    transitions, current = transition_alerts(previous, observations)
    checked_at = _server_now(conn)
    return ProbeResult(
        lines=tuple(render_transition(item, checked_at=checked_at) for item in transitions),
        next_state=current,
    )


def run_owned_probe(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    emit: Callable[[str], None],
    config: ProbeConfig | None = None,
) -> tuple[str, ...]:
    """Hold one cluster owner across query commit, publication and state ack."""
    lock_row = conn.execute(
        "SELECT pg_try_advisory_lock(%s)",
        (ADVISORY_LOCK_KEY,),
    ).fetchone()
    if lock_row is None or not bool(lock_row[0]):
        return ()

    failed = False
    try:
        result = run_probe(conn, config=config)
        # Prove all reads completed and the connection can commit before an
        # edge becomes externally visible. The session lock survives commit.
        conn.commit()
        publish_probe_result(conn, result, emit=emit)
        return result.lines
    except Exception:
        failed = True
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()
        except Exception:
            conn.rollback()
            if not failed:
                raise


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def config_from_env() -> ProbeConfig:
    return ProbeConfig(
        row_limit=_positive_int_env("VIDEO_REPLICA_OPS_ALERT_ROW_LIMIT", 100),
        session_window_seconds=_positive_int_env(
            "VIDEO_REPLICA_OPS_DOUBLE_ONLINE_WINDOW_SECONDS", 120
        ),
        fencing_window_seconds=_positive_int_env("VIDEO_REPLICA_OPS_FENCING_WINDOW_SECONDS", 300),
        security_window_seconds=_positive_int_env("VIDEO_REPLICA_OPS_SECURITY_WINDOW_SECONDS", 300),
        starvation_seconds=_positive_int_env("VIDEO_REPLICA_OPS_QUEUE_STARVATION_SECONDS", 300),
        statement_timeout_ms=_positive_int_env("VIDEO_REPLICA_OPS_QUERY_TIMEOUT_MS", 5_000),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the T37 cluster anomaly probe")
    return parser


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        database = resolve_database_config()
        validate_customer_production(database)
        if database.mode is not DatabaseMode.POSTGRESQL or database.dsn is None:
            raise RuntimeError("ops alert probe requires PostgreSQL")
        with psycopg.connect(database.dsn) as conn:
            run_owned_probe(
                conn,
                config=config_from_env(),
                emit=lambda line: print(line, flush=True),
            )
        return 0
    except Exception as exc:
        # Driver messages may contain DSN fragments or business values. Emit
        # only the exception class and a fixed operation name.
        print(
            json.dumps(
                {
                    "event": "ops_alert_probe_failed",
                    "error_type": type(exc).__name__,
                    "severity": "P1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through systemd/CLI.
    raise SystemExit(main())
