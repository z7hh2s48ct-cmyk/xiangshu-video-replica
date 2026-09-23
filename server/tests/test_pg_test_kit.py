"""CW-007 acceptance tests for the shared PG test foundation.

Covers the hard gate (unreachable PG must fail, not skip), the test-database
allowlist (non-test names are refused for create/drop), the repeatable
two-customer / three-device seed, rerun-after-interruption consistency,
independent-suite isolation, and the shared-suite single-runner lock.

These tests exercise real PostgreSQL (the local fixture on 5433 or whatever
TEST_POSTGRESQL_URL points at).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from pg_test_kit import (
    ALLOW_PG_SKIP_ENV,
    PgPreflightError,
    admin_dsn_of,
    assert_safe_test_database,
    create_test_database,
    database_name_of,
    drop_test_database,
    require_pg_or_explicit_skip,
    seed_customer_scenario,
    seed_scenario_facts,
    shared_suite_lock,
    upgrade_test_database_to_head,
)


@pytest.fixture(scope="module", autouse=True)
def _pg_live_hard_gate() -> Iterator[None]:
    """Runtime hard gate for the live-fixture tests in this module.

    Collection-time ``skipif`` probes are exactly the pattern CW-007
    removes, so reachability is asserted here instead: unreachable PG
    fails the suite unless ``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1``.
    """
    require_pg_or_explicit_skip()
    yield


# ---------------------------------------------------------------------------
# Hard gate behaviour (runs after the module live-gate; real DSN names used
# so the allowlist check is exercised before reachability)
# ---------------------------------------------------------------------------


def test_preflight_fails_on_unreachable_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_PG_SKIP_ENV, raising=False)
    dead_dsn = "postgresql://postgres:postgres@127.0.0.1:1/cw007_kit_alpha_test"
    with pytest.raises(pytest.fail.Exception) as excinfo:
        require_pg_or_explicit_skip(dead_dsn)
    assert "not reachable" in str(excinfo.value)


def test_preflight_explicit_optin_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_PG_SKIP_ENV, "1")
    dead_dsn = "postgresql://postgres:postgres@127.0.0.1:1/cw007_kit_alpha_test"
    with pytest.raises(pytest.skip.Exception):  # type: ignore[attr-defined]
        require_pg_or_explicit_skip(dead_dsn)


def test_preflight_rejects_non_test_database_names() -> None:
    for bad in ("postgres", "template0", "video_replica_prod", "customer_v3"):
        with pytest.raises(PgPreflightError):
            assert_safe_test_database(bad)
    # allowlisted names pass
    assert_safe_test_database("customer_v3_test")
    assert_safe_test_database("cw007_kit_alpha_test")


def test_create_and_drop_refuse_unregistered_names() -> None:
    with pytest.raises(PgPreflightError):
        create_test_database("customer_v3")
    with pytest.raises(PgPreflightError):
        drop_test_database("postgres")
    with pytest.raises(PgPreflightError):
        create_test_database("totally_unknown_db_test")


# ---------------------------------------------------------------------------
# Live-database behaviour
# ---------------------------------------------------------------------------


@pytest.fixture()
def alpha_dsn() -> str:
    dsn = create_test_database("cw007_kit_alpha_test")
    try:
        yield dsn
    finally:
        drop_test_database("cw007_kit_alpha_test")


@pytest.fixture()
def beta_dsn() -> str:
    dsn = create_test_database("cw007_kit_beta_test")
    try:
        yield dsn
    finally:
        drop_test_database("cw007_kit_beta_test")


def test_database_name_extraction() -> None:
    dsn = "postgresql://u:p@127.0.0.1:5433/cw007_kit_alpha_test"
    assert database_name_of(dsn) == "cw007_kit_alpha_test"
    assert admin_dsn_of(dsn).endswith("/postgres")


def test_seed_is_repeatable_two_customers_three_devices(alpha_dsn: str) -> None:
    upgrade_test_database_to_head(alpha_dsn)
    first = seed_customer_scenario(alpha_dsn)
    facts_one = seed_scenario_facts(alpha_dsn)
    second = seed_customer_scenario(alpha_dsn)
    facts_two = seed_scenario_facts(alpha_dsn)

    # seed returns measured facts (not hardcoded counts)
    assert first == {"users": 3, "devices": 3, "codes": 2, "wallets": 2}
    assert second == first
    assert facts_one == facts_two
    assert facts_two == {"users": 3, "devices": 3, "codes": 2, "wallets": 2}


def test_rerun_after_interrupted_create_is_consistent(alpha_dsn: str) -> None:
    # Simulate an interrupted run: database created and upgraded, seed half
    # applied, then the process "dies".  A fresh create must wipe every
    # trace and produce the canonical state.
    upgrade_test_database_to_head(alpha_dsn)
    seed_customer_scenario(alpha_dsn)
    with psycopg.connect(alpha_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('interrupted-ghost', 'ghost', 'Ghost', 'customer')"
        )
    recreated = create_test_database("cw007_kit_alpha_test")
    upgrade_test_database_to_head(recreated)
    seed_customer_scenario(recreated)
    facts = seed_scenario_facts(recreated)
    assert facts["users"] == 3  # ghost row gone with the wipe
    assert facts["devices"] == 3


def test_independent_suites_do_not_cross_talk(alpha_dsn: str, beta_dsn: str) -> None:
    assert alpha_dsn != beta_dsn
    upgrade_test_database_to_head(alpha_dsn)
    upgrade_test_database_to_head(beta_dsn)
    seed_customer_scenario(alpha_dsn)
    seed_customer_scenario(beta_dsn)

    with psycopg.connect(alpha_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('alpha-only-marker', 'marker', 'Marker', 'customer')"
        )
    with psycopg.connect(beta_dsn) as conn:
        alpha_rows = conn.execute(
            "SELECT count(*) FROM users WHERE id = 'alpha-only-marker'"
        ).fetchone()
        beta_devices = conn.execute(
            "SELECT count(*) FROM customer_devices WHERE id LIKE 'cw007-dev-%'"
        ).fetchone()
    assert alpha_rows is not None and alpha_rows[0] == 0
    assert beta_devices is not None and beta_devices[0] == 3


def test_shared_suite_lock_is_exclusive() -> None:
    import threading
    import time

    acquired_inside: list[float] = []
    with shared_suite_lock():

        def contender() -> None:
            with shared_suite_lock():
                acquired_inside.append(time.monotonic())

        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.2)
        assert acquired_inside == []  # contender blocked while we hold it
    thread.join(timeout=5)
    assert len(acquired_inside) == 1  # contender proceeded after release
