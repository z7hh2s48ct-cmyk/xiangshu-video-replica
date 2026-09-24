from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool, text

config = context.config

if config.config_file_name is not None:
    # Alembic is normally a separate process, but tests and administrative
    # tooling also invoke it in-process after application modules have already
    # created their loggers.  The logging module's default would disable every
    # existing non-Alembic logger, silently suppressing request/fencing audit
    # events for the rest of that process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None

DATABASE_URL_ENV = "VIDEO_REPLICA_DATABASE_URL"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_PG_URL_PREFIXES = ("postgresql://", "postgres://")
# Same truthy convention as app.db_pg and the other customer-fenced modules.
_TRUTHY_CUSTOMER_PRODUCTION = frozenset({"1", "true", "yes", "on"})


def _is_customer_production() -> bool:
    return (
        os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY_CUSTOMER_PRODUCTION
    )


def _is_postgres_url(url: str) -> bool:
    """Accept every PostgreSQL URL form an operator may legitimately export,
    including an already driver-qualified ``postgresql+psycopg://`` DSN."""
    return url.startswith(_PG_URL_PREFIXES) or url.startswith("postgresql+")


def _reject_non_postgres_migration_target(url: str) -> None:
    """Fail closed when customer production resolves to a non-PostgreSQL target.

    The accident this prevents: an operator runs ``alembic upgrade head`` on the
    customer host without exporting ``VIDEO_REPLICA_DATABASE_URL``. Alembic then
    falls back to the ``alembic.ini`` default (``sqlite:///data/app.db``), the
    whole chain "succeeds" against a throwaway SQLite file and returns 0, while
    the customer PostgreSQL database is never upgraded. A green exit code over an
    un-upgraded production database is worse than a failure, because nothing
    downstream re-checks it.

    Deliberately scoped to customer production. The internal/desktop lane reaches
    Alembic through ``app/db.py:initialize_database``, which explicitly sets a
    SQLite URL on the config object (``app/db.py:alembic_config``) and stays
    supported until CW-043 lets CW-042 retire that compatibility layer
    (CW-053 §3 E2). A global refusal here would break that lane and 30+ tests.
    """
    if not _is_customer_production() or _is_postgres_url(url):
        return
    raise RuntimeError(
        "customer production requires PostgreSQL: the migration target resolved to "
        f"{url!r}. Set {DATABASE_URL_ENV} to the customer PostgreSQL DSN. The "
        "alembic.ini SQLite default exists only for the internal/desktop lane and "
        "must never be the customer migration target."
    )


def _to_psycopg_url(url: str) -> str:
    """Alembic runs on the psycopg3 driver; bare ``postgresql://`` URLs
    resolve to the psycopg2 dialect, which is not installed."""
    for prefix in _PG_URL_PREFIXES:
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+psycopg://", 1)
    return url


def resolve_migration_url() -> str:
    """Resolve the migration target URL (M0 review H3).

    ``VIDEO_REPLICA_DATABASE_URL`` — the same variable ``app.db_pg`` uses to
    pick the runtime database mode — takes precedence over the ``alembic.ini``
    default, so operators can run ``alembic upgrade head`` against PostgreSQL
    without editing the ini file. A URL explicitly set on the Alembic config
    object (tests use ``config.set_main_option``) is still honoured when the
    environment variable is unset.

    In customer production the resolved URL is rejected unless it is PostgreSQL,
    so the ``alembic.ini`` SQLite default can never be migrated by accident.
    """
    env_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    raw_url = env_url or (config.get_main_option("sqlalchemy.url") or "")
    # Checked on the raw URL, before the driver suffix is rewritten, so the
    # rejection names exactly what the operator (or the ini default) supplied.
    _reject_non_postgres_migration_target(raw_url)
    return _to_psycopg_url(raw_url)


def ensure_sqlite_parent_directory(url: str) -> None:
    """Make Alembic's standalone SQLite default work on a fresh checkout."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url.endswith(":memory:"):
        return
    Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)


def widen_postgres_version_table(connection) -> None:
    """Pre-create/width the alembic_version table on PostgreSQL.

    Alembic creates ``alembic_version.version_num`` as VARCHAR(32), but this
    project's revision ids (e.g. ``017_generation_task_retry_lineage``)
    exceed 32 characters. SQLite does not enforce the declared width, so the
    issue only surfaces on PostgreSQL. Creating the table first with a wider
    column is enough: Alembic's own ``_ensure_version_table`` skips tables
    that already exist. Existing narrower tables (interrupted upgrades) are
    widened in place. SQLite databases are untouched.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    connection.execute(
        text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    )


def run_migrations_offline() -> None:
    """Refuse to emit SQL: offline output is not a deliverable upgrade script.

    ``alembic upgrade head --sql`` cannot produce an executable script for this
    chain, for two independent reasons:

    1. Alembic hardcodes the offline ``alembic_version.version_num`` column as
       VARCHAR(32) (M0 review M7), while this project's revision ids reach 33+
       characters, so the stamped version row cannot be inserted.
       ``widen_postgres_version_table`` can only help online, where a live
       connection exists to widen the column.
    2. Offline mode hands migrations a ``MockConnection``. Revisions that reflect
       a live table — 009's ``conn.exec_driver_sql`` rebuild, and every
       ``batch_alter_table`` without ``copy_from`` — raise instead of emitting
       SQL, so the run dies partway and has already leaked a **partial** script
       to stdout.

    Both facts used to live only in this docstring and the ``alembic.ini`` note.
    Documenting is not enforcing: the partial output still looks like a migration
    script, and the ``--sql`` path is exactly the one a DBA reaches for when
    asked to review an upgrade. CW-056 therefore fails closed *before*
    ``context.configure``, so no SQL is emitted at all.

    ``resolve_migration_url()`` runs first on purpose: in customer production the
    SQLite-target refusal is the more actionable diagnosis and must not be masked
    by this generic offline refusal.
    """
    url = resolve_migration_url()
    raise RuntimeError(
        "offline mode (alembic upgrade --sql) is not a deliverable upgrade script "
        f"for this chain (resolved target: {url!r}). Alembic hardcodes the offline "
        "alembic_version column as VARCHAR(32) while these revision ids exceed it, "
        "and table-reflecting revisions cannot run against a MockConnection, so the "
        "emitted SQL is partial and cannot be applied. Run the online path instead: "
        f"set {DATABASE_URL_ENV} and run `alembic upgrade head`, or use "
        "deploy/postgres/migrate.sh, which also verifies the applied head."
    )


def run_migrations_online() -> None:
    url = resolve_migration_url()
    ensure_sqlite_parent_directory(url)
    connectable = engine_from_config(
        {**config.get_section(config.config_ini_section, {}), "sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        widen_postgres_version_table(connection)
        # Commit the DDL explicitly: otherwise the connection stays inside an
        # implicit autobegin transaction, Alembic then reuses that "external"
        # transaction, and the whole upgrade chain is rolled back when the
        # connection closes.
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
