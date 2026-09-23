"""CW-057 — the Gate 1 seed only accepts a migrated, empty PostgreSQL database.

TEST-PG. The historical SQLite form (``--db-path`` + ``initialize_database``)
is retired: the seed resolves its DSN through
``app.db_pg.resolve_cli_pg_dsn``, refuses anything but a pristine migrated
database, and redacts the DSN in its summary. The rejection cases run with no
PostgreSQL reachable at all — a misconfigured seed must fail closed without
creating any database file (PG-01 缺/错/SQLite DSN 非零失败且不创建数据库文件).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from cryptography.fernet import Fernet

from app.db_pg import redact_postgres_dsn
from app.gate1_bootstrap import bootstrap_gate1_database
from app.gate1_bootstrap import main as gate1_main
from tests.pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

CW057_TEST_DB = "cw057_gate1_seed_test"


@pytest.fixture(scope="module")
def cw057_pg_dsn() -> Iterator[str]:
    """A dedicated migrated database for the Gate 1 seed contract."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW057_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW057_TEST_DB)


@pytest.fixture(autouse=True)
def _settings_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime settings write needs the Fernet root key, like the API."""
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture()
def pristine_dsn(cw057_pg_dsn: str) -> Iterator[str]:
    """The migrated database reset to its pristine (zero users) state.

    Row-level ``DELETE`` rather than ``TRUNCATE``: the schema refuses to
    truncate audit tables (``refuse_truncate_of_audit_tables``), and the
    ``CASCADE`` from ``users`` would reach them.
    """
    with psycopg.connect(cw057_pg_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM runtime_settings")
        conn.execute("DELETE FROM billing_credit_lots WHERE user_id LIKE 'gate1%'")
        conn.execute("DELETE FROM wallet_transactions WHERE user_id LIKE 'gate1%'")
        conn.execute("DELETE FROM recharge_orders WHERE user_id LIKE 'gate1%'")
        conn.execute("DELETE FROM wallets WHERE user_id LIKE 'gate1%'")
        conn.execute("DELETE FROM users WHERE id LIKE 'gate1%'")
        remaining = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert remaining == 0
    yield cw057_pg_dsn


def test_gate1_bootstrap_seeds_identity_funded_wallet_and_runtime(
    pristine_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))

    summary = bootstrap_gate1_database(
        pristine_dsn,
        user_id="gate1_admin",
        display_name="Gate 1 Admin",
    )

    with psycopg.connect(pristine_dsn) as conn:
        user = conn.execute("SELECT id, role FROM users WHERE id = 'gate1_admin'").fetchone()
        runtime = conn.execute(
            """
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks,
                   active_storage_provider
            FROM runtime_settings WHERE id = 1
            """
        ).fetchone()
        wallet = conn.execute(
            """
            SELECT available_credits, reserved_credits
            FROM wallets WHERE user_id = 'gate1_admin'
            """
        ).fetchone()
        recharge = conn.execute(
            """
            SELECT status, credits FROM recharge_orders
            WHERE user_id = 'gate1_admin'
            """
        ).fetchone()
        charge = conn.execute(
            """
            SELECT type, available_delta, reserved_delta
            FROM wallet_transactions WHERE user_id = 'gate1_admin'
            """
        ).fetchone()
        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        version_count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        provider_count = conn.execute("SELECT COUNT(*) FROM provider_settings").fetchone()[0]

    assert summary["desktop_user_id"] == "gate1_admin"
    assert summary["active_storage_provider"] == "local"
    # The summary must never carry credentials (CW-057 日志脱敏).
    assert "gate1_admin@" not in summary["database"]
    assert summary["database"] == redact_postgres_dsn(pristine_dsn)

    assert user == ("gate1_admin", "admin")
    assert runtime == (6, 2, "local")
    assert wallet == (10, 0)
    assert recharge == ("PAID", 10)
    assert charge == ("CHARGE", 10, 0)
    assert project_count == 0
    assert version_count == 0
    assert provider_count == 0


def test_gate1_bootstrap_refuses_an_already_seeded_database(pristine_dsn: str) -> None:
    bootstrap_gate1_database(
        pristine_dsn,
        user_id="gate1_admin",
        display_name="Gate 1 Admin",
    )

    with pytest.raises(RuntimeError, match="not pristine"):
        bootstrap_gate1_database(
            pristine_dsn,
            user_id="second_admin",
            display_name="Second Admin",
        )

    # The refused re-seed must not have written anything.
    with psycopg.connect(pristine_dsn) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_gate1_bootstrap_requires_a_migrated_schema(
    cw057_pg_dsn: str,
) -> None:
    """A database without the schema fails with the migrate-first pointer.

    Uses a dedicated throwaway database name through the admin DSN so the
    allowlisted kit name keeps its schema for the other cases.
    """
    admin_dsn = cw057_pg_dsn.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute('DROP DATABASE IF EXISTS "cw057_unmigrated_test" WITH (FORCE)')
        conn.execute('CREATE DATABASE "cw057_unmigrated_test"')
    try:
        unmigrated_dsn = admin_dsn.rsplit("/", 1)[0] + "/cw057_unmigrated_test"
        with pytest.raises(RuntimeError, match="migrate"):
            bootstrap_gate1_database(
                unmigrated_dsn,
                user_id="gate1_admin",
                display_name="Gate 1 Admin",
            )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute('DROP DATABASE IF EXISTS "cw057_unmigrated_test" WITH (FORCE)')


@pytest.mark.parametrize(
    ("argv", "env_db_path"),
    [
        ([], False),  # no --database-url, no env
        (["--database-url", ""], False),
        (["--database-url", "sqlite:///./gate1-should-not-exist.sqlite3"], False),
        (["--database-url", "sqlite://./gate1-should-not-exist.sqlite3"], False),
        (["--database-url", "mysql://user:pass@localhost/db"], False),
        (["--database-url", "postgresql://user:pass@localhost/db"], True),  # DB_PATH leftover
    ],
)
def test_gate1_bootstrap_cli_rejects_non_pg_configuration_without_creating_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env_db_path: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    if env_db_path:
        monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "leftover.sqlite3"))
    else:
        monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)

    assert gate1_main(argv) == 1

    assert list(tmp_path.iterdir()) == []
