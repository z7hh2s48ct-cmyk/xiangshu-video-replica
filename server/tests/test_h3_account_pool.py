"""Per-account limits, durable routing and shared-worker capacity on real PG."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from test_customer_queue_fairness import (
    _acquire,
    _complete_and_release,
    _seed,
    fair_dsn,  # noqa: F401
    fair_state,  # noqa: F401
)

from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.h3_account_pool import account_api_key, read_accounts, save_account


@pytest.fixture()
def pool_state(fair_state: str, monkeypatch: pytest.MonkeyPatch) -> str:  # noqa: F811
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    _seed(fair_state, user_ids=["u1"], tasks_per_user=30, fair_queue_enabled=False)
    with pg_transaction() as raw:
        raw.execute("TRUNCATE h3_provider_accounts CASCADE")
        raw.execute("DELETE FROM provider_settings WHERE provider='metaso'")
    return fair_state


def save(account_id: str, limit: int, *, version: int = 0, enabled: bool = True) -> dict:
    with pg_transaction() as raw:
        return save_account(
            BusinessConnection.postgres(raw),
            account_id=account_id,
            name=account_id,
            api_key=f"synthetic-{account_id}" if version == 0 else "",
            concurrency_limit=limit,
            enabled=enabled,
            expected_version=version,
        )


def test_unequal_limits_are_additive_and_round_robin(pool_state: str) -> None:
    save("a", 6)
    save("b", 12)
    leases = [_acquire(pool_state, f"worker-{i}") for i in range(18)]
    assert all(leases)
    assert _acquire(pool_state, "full") is None
    with pg_transaction() as raw:
        counts = dict(
            raw.execute(
                "SELECT account_id,count(*) FROM h3_provider_task_accounts GROUP BY account_id"
            ).fetchall()
        )
        assert counts == {"a": 6, "b": 12}
        first = [
            raw.execute(
                "SELECT account_id FROM h3_provider_task_accounts WHERE task_id=%s", (lease["id"],)
            ).fetchone()[0]
            for lease in leases[:4]
            if lease
        ]
        assert first == ["a", "b", "a", "b"]
    assert leases[0] is not None
    _complete_and_release(pool_state, leases[0])
    assert _acquire(pool_state, "released") is not None


def test_workers_do_not_oversubscribe_and_paused_account_still_routes(pool_state: str) -> None:
    save("a", 2)
    save("b", 3)
    with ThreadPoolExecutor(max_workers=8) as workers:
        leases = list(workers.map(lambda i: _acquire(pool_state, f"worker-{i}"), range(12)))
    assert len([lease for lease in leases if lease]) == 5
    save("a", 2, version=1, enabled=False)
    with pg_transaction() as raw:
        task = raw.execute(
            "SELECT task_id FROM h3_provider_task_accounts WHERE account_id='a' LIMIT 1"
        ).fetchone()[0]
        assert account_api_key(BusinessConnection.postgres(raw), task_id=task) == "synthetic-a"
        from app.generation import MetasoH3Provider, h3_provider_for_task

        provider = h3_provider_for_task(BusinessConnection.postgres(raw), "metaso", task_id=task)
        assert isinstance(provider, MetasoH3Provider) and provider.api_key == "synthetic-a"
        assert read_accounts(BusinessConnection.postgres(raw))["total_concurrency"] == 3
        other_ids = {
            row[0]
            for row in raw.execute(
                "SELECT task_id FROM h3_provider_task_accounts WHERE account_id='b'"
            )
        }
    for lease in leases:
        if lease and lease["id"] in other_ids:
            _complete_and_release(pool_state, lease)
    # Paused-account jobs continue running without blocking another account's slots.
    assert all(_acquire(pool_state, f"replacement-{i}") for i in range(3))
    assert _acquire(pool_state, "still-full") is None


def test_duplicate_key_version_and_used_credentials_are_protected(pool_state: str) -> None:
    save("a", 3)
    assert _acquire(pool_state, "worker") is not None
    for account_id, key, version in [
        ("b", "synthetic-a", 0),
        ("a", "replacement", 1),
        ("a", "", 0),
    ]:
        with pytest.raises(HTTPException):
            with pg_transaction() as raw:
                save_account(
                    BusinessConnection.postgres(raw),
                    account_id=account_id,
                    name="test",
                    api_key=key,
                    concurrency_limit=7,
                    enabled=True,
                    expected_version=version,
                )
    with pg_transaction() as raw:
        snapshot = str(read_accounts(BusinessConnection.postgres(raw)))
        assert "synthetic-a" not in snapshot
        encrypted = raw.execute(
            "SELECT encrypted_api_key FROM h3_provider_accounts WHERE id='a'"
        ).fetchone()[0]
        assert "synthetic-a" not in encrypted


def test_initial_pool_pins_legacy_tasks(pool_state: str) -> None:
    from app.settings import SettingsRepository

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        SettingsRepository(conn).save_provider_config(
            "metaso", {"api_key": "synthetic-legacy"}, actor_user_id="u1"
        )
        raw.execute("UPDATE generation_tasks SET status='RUNNING', attempt=1 WHERE id='task-u1-0'")
    save("b", 7)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        assert account_api_key(conn, task_id="task-u1-0") == "synthetic-legacy"
        assert len(read_accounts(conn)["accounts"]) == 2


def test_uncertain_submission_keeps_account_slot_until_reconciled(pool_state: str) -> None:
    save("a", 1)
    lease = _acquire(pool_state, "crashed-worker")
    assert lease is not None
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE generation_tasks SET locked_until=now()-interval '1 minute' WHERE id=%s",
            (lease["id"],),
        )
    assert _acquire(pool_state, "next-worker") is None
    with pg_transaction() as raw:
        assert (
            raw.execute(
                "SELECT status FROM generation_tasks WHERE id=%s", (lease["id"],)
            ).fetchone()[0]
            == "SUBMISSION_UNCERTAIN"
        )
        assert read_accounts(BusinessConnection.postgres(raw))["accounts"][0]["active_tasks"] == 1
        raw.execute("UPDATE generation_tasks SET status='FAILED' WHERE id=%s", (lease["id"],))
    assert _acquire(pool_state, "after-reconciliation") is not None
