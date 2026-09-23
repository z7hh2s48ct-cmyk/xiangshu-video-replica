from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

BUSY_TIMEOUT_MS = 5000
SERVER_DIR = Path(__file__).resolve().parent.parent

# CW-042-a (owner-signed split, CW042-SCOPE-INVENTORY §2.4): the internal /
# desktop SQLite lane stays for now (042-b defers physical deletion behind
# CW-039), but customer production must fail closed on every process-level
# SQLite entry. The HTTP surface is guarded by the CW-025 lifespan check in
# app.main; these guards close the entries that never run a lifespan —
# operator CLIs and bootstrap helpers. The env name mirrors the existing
# declarations in admin_auth_routes/control_routes/control_auth (importing
# app.bootstrap here would be circular: bootstrap imports this module).
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_TRUTHY = {"1", "true", "yes", "on"}


def _refuse_sqlite_in_customer_production() -> None:
    if os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() not in _TRUTHY:
        return
    raise RuntimeError(
        "customer production is PostgreSQL-only: the SQLite lane "
        "(VIDEO_REPLICA_DB_PATH / app.db entry points) is not available here; "
        "configure VIDEO_REPLICA_DATABASE_URL instead"
    )


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    _refuse_sqlite_in_customer_production()
    path = Path(db_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def initialize_database(db_path: str | Path) -> sqlite3.Connection:
    # :memory: cannot survive Alembic's separate connection; reject it so a
    # caller gets a clear error instead of an empty, table-less database.
    if str(db_path) == ":memory:":
        raise ValueError("initialize_database does not support ':memory:'; use a temp file path")
    upgrade_database(db_path)
    return connect_database(db_path)


def upgrade_database(db_path: str | Path, revision: str = "head") -> None:
    _refuse_sqlite_in_customer_production()
    path = Path(db_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)


def alembic_config(db_path: str | Path) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", _sqlite_url(db_path))
    return config


def _sqlite_url(db_path: str | Path) -> str:
    if db_path == ":memory:":
        return "sqlite:///:memory:"
    return f"sqlite:///{Path(db_path).resolve()}"
