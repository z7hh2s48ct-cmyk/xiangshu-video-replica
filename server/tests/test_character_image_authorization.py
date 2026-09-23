"""Image authorization must fail before storage, billing or provider work."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.image_tasks import enqueue_character_sheet_task
from app.simple_character_routes import _enqueue_simple_character_upload


class ForbiddenSideEffect:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Unauthorized request touched {name}")


@pytest.mark.parametrize(
    ("version", "accepted"), [("", False), ("2026-09-14-v1", False), ("old", True)]
)
def test_rejects_missing_or_stale_authorization_before_reading_upload(
    version: str, accepted: bool
) -> None:
    untouched: Any = ForbiddenSideEffect()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            _enqueue_simple_character_upload(
                storage=untouched,
                db=untouched,
                file=untouched,
                display_name="Authorized test portrait",
                idempotency_key="image-authorization-test",
                persona_name="",
                project_id=None,
                image_consent_version=version,
                image_consent_accepted=accepted,
            )
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "IMAGE_AUTHORIZATION_REQUIRED"


def test_authorization_is_durable_and_replay_does_not_duplicate_audit() -> None:
    """A uniquely named database prevents collisions with other PG test suites."""
    base = os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )
    parameters = psycopg.conninfo.conninfo_to_dict(base)
    parameters["dbname"] = "postgres"
    database = "image_authorization_" + uuid4().hex[:16] + "_test"
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(
            psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(database))
        )
    try:
        test_parameters = {**parameters, "dbname": database}
        dsn = psycopg.conninfo.make_conninfo(**test_parameters)
        from sqlalchemy.engine import URL

        server = Path(__file__).resolve().parents[1]
        config = Config(str(server / "alembic.ini"))
        config.set_main_option("script_location", str(server / "migrations"))
        url = URL.create(
            "postgresql+psycopg",
            username=parameters.get("user"),
            password=parameters.get("password"),
            host=parameters.get("host"),
            port=int(parameters.get("port", "5432")),
            database=database,
        )
        config.set_main_option(
            "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
        )
        command.upgrade(config, "head")
        with psycopg.connect(dsn) as raw:
            raw.execute(
                "INSERT INTO users(id,username,display_name,role) "
                "VALUES ('consent-user','consent-user','Consent test','employee')"
            )
            raw.commit()
            conn = BusinessConnection.postgres(raw)
            arguments: dict[str, Any] = dict(
                actor=CurrentUser("consent-user", "consent-user", "Consent test", "employee"),
                operation="CREATE",
                project_id=None,
                identity_id=None,
                display_name="Consent test",
                persona_name="Consent test",
                source_storage_uri="local://consent-test.png",
                source_content_type="image/png",
                source_sha256="a" * 64,
                source_size_bytes=10,
                idempotency_key="consent-replay-test",
                image_consent_version="2026-09-14-v1",
            )
            first = enqueue_character_sheet_task(conn, **arguments)
            replay = enqueue_character_sheet_task(conn, **arguments)
            assert first["id"] == replay["id"]
            request = json.loads(str(replay["request_json"]))
            assert request["image_authorization"]["version"] == "2026-09-14-v1"
            assert request["image_authorization"]["accepted"] is True
            assert replay["created_by_user_id"] == "consent-user"
            assert replay["source_sha256"] == "a" * 64
            assert replay["created_at"]
            audit = raw.execute(
                "SELECT count(*) FROM audit_logs "
                "WHERE action='simple_character.image_authorization.accept' AND entity_id=%s",
                (str(first["id"]),),
            ).fetchone()
            assert audit is not None and audit[0] == 1
    finally:
        with psycopg.connect(**parameters, autocommit=True) as admin:
            admin.execute(
                psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(database))
            )
