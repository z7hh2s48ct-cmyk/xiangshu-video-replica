"""T14 / ACT-07 — the AEAD idempotent-recovery engine for the customer lane.

Fail-first tests for the frozen file ``server/app/customer_idempotency.py``:
the envelope engine extracted from T13's activation route (operation/scope/
key digest, request hash, same-key conflict evidence, AES-GCM response
recovery) plus the T14 cleanup story — purging expired ciphertext so the
envelope table cannot grow unboundedly while the 029 CHECK coupling
(purged ⇒ ciphertext gone) and the recovery-window semantics stay intact.

Red lines (dev doc §7 / ACT-07): the envelope never stores a directly usable
plaintext secret — the AEAD ciphertext is the only persisted copy of the
one-time response; the raw client key never reaches the database; expiry is
decided on the server clock, never the client's.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

from app.customer_idempotency import (
    CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV,
    DEFAULT_RECOVERY_WINDOW_SECONDS,
    envelope_aad,
    idempotency_key_digest,
    open_response,
    purge_expired_envelopes,
    request_hash,
    seal_response,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

ENVELOPES_TABLE = "customer_idempotency_envelopes"

_V1_AEAD_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"  # 32 bytes, urlsafe b64
_V2_AEAD_KEY = "ISIjJCUmJygpKissLS4vMDEyMzQ1Njc4Ojo7PDw-PD8"  # 32 bytes, urlsafe b64


@pytest.fixture(autouse=True)
def configured_customer_aead_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every database-bound envelope test needs the same synthetic key.

    The PostgreSQL cases used to skip before reaching the digest helper on a
    machine without the fixture, hiding this missing test precondition.
    """
    monkeypatch.setenv(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, _V1_AEAD_KEY)


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Module-level units (no database) — these always run; only the
# database-bound cases skip without the fixture (session review P3).
# ---------------------------------------------------------------------------


def test_request_hash_is_stable_across_whitespace() -> None:
    """A retry differing only in surrounding whitespace replays, not 409s."""
    padded = request_hash(
        {
            "activation_code": " XS04-AAAA-BBBB-0001 ",
            "device_fingerprint": " fp-1 ",
            "device_name": " Device ",
            "device_platform": " windows ",
        }
    )
    tight = request_hash(
        {
            "activation_code": "XS04-AAAA-BBBB-0001",
            "device_fingerprint": "fp-1",
            "device_name": "Device",
            "device_platform": "windows",
        }
    )
    assert padded == tight


def test_request_hash_distinguishes_different_params() -> None:
    """Different business parameters must produce a different hash."""
    first = request_hash({"activation_code": "XS04-AAAA-BBBB-0001", "device_fingerprint": "fp-1"})
    second = request_hash({"activation_code": "XS04-AAAA-BBBB-0002", "device_fingerprint": "fp-1"})
    assert first != second


def test_idempotency_key_digest_is_keyed_and_hides_the_raw_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, _V1_AEAD_KEY)
    digest = idempotency_key_digest("client-secret-key")
    assert digest != "client-secret-key"
    assert "client-secret-key" not in digest
    assert digest != hashlib.sha256(b"client-secret-key").hexdigest()
    assert len(digest) == 64
    assert idempotency_key_digest("client-secret-key") == digest


def test_seal_open_roundtrip_restores_payload() -> None:
    key = b"k" * 32
    aad = envelope_aad("activate", "scope-1", "digest-1")
    payload = {
        "username": "customer-abc",
        "device_token": "one-time-token",
        "session_epoch": 1,
    }
    ciphertext = seal_response(payload, key=key, aad=aad)
    assert open_response(ciphertext, key=key, aad=aad) == payload


def test_open_response_rejects_wrong_aad() -> None:
    """A ciphertext bound to one envelope row must never open under another."""
    key = b"k" * 32
    payload = {"device_token": "one-time-token"}
    ciphertext = seal_response(
        payload, key=key, aad=envelope_aad("activate", "scope-1", "digest-1")
    )
    with pytest.raises(Exception, match="verification failed"):
        open_response(ciphertext, key=key, aad=envelope_aad("activate", "scope-2", "digest-1"))


def test_sealed_ciphertext_holds_no_plaintext_secret() -> None:
    """ACT-07 red line: the envelope must not store usable plaintext."""
    key = b"k" * 32
    aad = envelope_aad("activate", "scope-1", "digest-1")
    secret_value = "one-time-device-token-9f8e7d6c"
    ciphertext = seal_response({"device_token": secret_value}, key=key, aad=aad)
    assert secret_value not in ciphertext
    assert "device_token" not in ciphertext


def test_aead_key_rotation_window_resolves_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.customer_idempotency import customer_aead_key

    monkeypatch.setenv(f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV}_V1", _V1_AEAD_KEY)
    monkeypatch.setenv(f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV}_V2", _V2_AEAD_KEY)
    assert customer_aead_key(1) != customer_aead_key(2)


def test_retired_aead_key_version_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.customer_idempotency import customer_aead_key

    monkeypatch.delenv(f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV}_V1", raising=False)
    monkeypatch.delenv(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, raising=False)
    monkeypatch.setenv(f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV}_V2", _V2_AEAD_KEY)
    with pytest.raises(Exception, match="not configured"):
        customer_aead_key(1)


def test_recovery_window_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.customer_idempotency import recovery_window_seconds

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS", "60")
    assert recovery_window_seconds() == 60
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS", "not-a-number")
    assert recovery_window_seconds() == DEFAULT_RECOVERY_WINDOW_SECONDS
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS", raising=False)
    assert recovery_window_seconds() == DEFAULT_RECOVERY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.fixture()
def envelope_dsn() -> str:
    """Fresh database at head; the envelope table carries no FK dependencies."""
    if not _pg_available(_pg_dsn()):
        # Only the database-bound cases skip — the module-level units and the
        # missing-DSN CLI case must always run, or a machine without the
        # fixture reports a vacuous all-green (session review P3).
        require_pg_or_explicit_skip()
    from alembic import command
    from alembic.config import Config

    db_name = "t14_customer_idempotency"
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    command.upgrade(config, "head")
    try:
        yield dsn
    finally:
        _drop_database(db_name)


def _insert_completed_envelope(
    conn: psycopg.Connection,
    *,
    seq: int,
    scope: str,
    key_digest: str,
    req_hash: str,
    ciphertext: str,
    recovery_expires_at: str,
    created_at: str,
) -> str:
    """Seed a completed envelope with an explicit created_at.

    The 029 CHECK demands recovery_expires_at > created_at, so back-dating
    the recovery window (to simulate expiry) requires back-dating created_at
    as well — the same trick T13's route tests use.
    """
    envelope_id = f"env-{seq}"
    conn.execute(
        f"INSERT INTO {ENVELOPES_TABLE} "
        "(id, operation, scope, key_digest, request_hash, ciphertext, key_version, "
        " recovery_expires_at, created_at) "
        "VALUES (%s, 'activate', %s, %s, %s, %s, 1, %s, %s)",
        (envelope_id, scope, key_digest, req_hash, ciphertext, recovery_expires_at, created_at),
    )
    return envelope_id


def _fetch_envelope(conn: psycopg.Connection, envelope_id: str) -> tuple[object, ...]:
    row = conn.execute(
        f"SELECT ciphertext, key_version, recovery_expires_at, purged_at "
        f"FROM {ENVELOPES_TABLE} WHERE id = %s",
        (envelope_id,),
    ).fetchone()
    assert row is not None
    return row


def test_envelope_lifecycle_same_key_recovers_same_payload(envelope_dsn: str) -> None:
    """Insert → complete → load → open returns the sealed one-time response."""
    from app.customer_idempotency import complete_envelope, insert_envelope, load_envelope

    key = b"k" * 32
    scope = "scope-lifecycle"
    key_digest = idempotency_key_digest("key-lifecycle")
    req_hash = request_hash(
        {"activation_code": "XS04-AAAA-BBBB-0001", "device_fingerprint": "fp-1"}
    )
    aad = envelope_aad("activate", scope, key_digest)
    payload = {"username": "customer-x", "device_token": "tok", "session_epoch": 1}

    with psycopg.connect(envelope_dsn) as conn:
        envelope_id = insert_envelope(
            conn, operation="activate", scope=scope, key_digest=key_digest, request_hash=req_hash
        )
        assert envelope_id is not None
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=seal_response(payload, key=key, aad=aad),
            key_version=1,
            recovery_expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        record = load_envelope(conn, operation="activate", scope=scope, key_digest=key_digest)

    assert record is not None
    assert record.request_hash == req_hash
    assert record.ciphertext is not None
    assert record.key_version == 1
    assert record.purged_at is None
    assert open_response(record.ciphertext, key=key, aad=aad) == payload


def test_same_key_different_request_hash_is_conflict_evidence(envelope_dsn: str) -> None:
    """The loaded request_hash differing from the retry's is the 409 trigger."""
    from app.customer_idempotency import insert_envelope, load_envelope

    scope = "scope-conflict"
    key_digest = idempotency_key_digest("key-conflict")
    stored_hash = request_hash(
        {"activation_code": "XS04-AAAA-BBBB-0001", "device_fingerprint": "fp-1"}
    )
    retry_hash = request_hash(
        {"activation_code": "XS04-AAAA-BBBB-0002", "device_fingerprint": "fp-1"}
    )

    with psycopg.connect(envelope_dsn) as conn:
        insert_envelope(
            conn, operation="activate", scope=scope, key_digest=key_digest, request_hash=stored_hash
        )
        record = load_envelope(conn, operation="activate", scope=scope, key_digest=key_digest)

    assert record is not None
    assert record.request_hash == stored_hash
    assert record.request_hash != retry_hash


def test_expired_window_is_visible_to_the_caller(envelope_dsn: str) -> None:
    """A back-dated recovery window must be recognizable server-side."""
    from app.customer_idempotency import load_envelope

    now = datetime.now(UTC)
    scope = "scope-expired-window"
    key_digest = idempotency_key_digest("key-expired-window")
    req_hash = request_hash(
        {"activation_code": "XS04-AAAA-BBBB-0001", "device_fingerprint": "fp-1"}
    )

    with psycopg.connect(envelope_dsn) as conn:
        _insert_completed_envelope(
            conn,
            seq=9,
            scope=scope,
            key_digest=key_digest,
            req_hash=req_hash,
            ciphertext="x",
            recovery_expires_at=(now - timedelta(hours=2)).isoformat(),
            created_at=(now - timedelta(hours=4)).isoformat(),
        )
        record = load_envelope(conn, operation="activate", scope=scope, key_digest=key_digest)

    assert record is not None
    assert record.recovery_expires_at is not None
    expires_at = datetime.fromisoformat(record.recovery_expires_at)
    assert expires_at <= datetime.now(UTC)


def test_purge_clears_only_expired_envelopes(envelope_dsn: str) -> None:
    """Purge nulls the payload of expired rows only, once, CHECK-coupled."""
    key = b"k" * 32
    now = datetime.now(UTC)
    expired_id = "env-expired"
    live_id = "env-live"
    already_purged_id = "env-purged"

    with psycopg.connect(envelope_dsn) as conn:
        _insert_completed_envelope(
            conn,
            seq=1,
            scope="scope-purge-expired",
            key_digest=idempotency_key_digest("key-purge-expired"),
            req_hash="hash-1",
            ciphertext=seal_response(
                {"a": 1}, key=key, aad=envelope_aad("activate", "scope-purge-expired", "d1")
            ),
            recovery_expires_at=(now - timedelta(hours=1)).isoformat(),
            created_at=(now - timedelta(hours=4)).isoformat(),
        )
        conn.execute(
            f"UPDATE {ENVELOPES_TABLE} SET id = %s WHERE scope = 'scope-purge-expired'",
            (expired_id,),
        )
        _insert_completed_envelope(
            conn,
            seq=2,
            scope="scope-purge-live",
            key_digest=idempotency_key_digest("key-purge-live"),
            req_hash="hash-2",
            ciphertext=seal_response(
                {"b": 2}, key=key, aad=envelope_aad("activate", "scope-purge-live", "d2")
            ),
            recovery_expires_at=(now + timedelta(hours=1)).isoformat(),
            created_at=(now - timedelta(hours=4)).isoformat(),
        )
        conn.execute(
            f"UPDATE {ENVELOPES_TABLE} SET id = %s WHERE scope = 'scope-purge-live'",
            (live_id,),
        )
        _insert_completed_envelope(
            conn,
            seq=3,
            scope="scope-purge-done",
            key_digest=idempotency_key_digest("key-purge-done"),
            req_hash="hash-3",
            ciphertext="legacy",
            recovery_expires_at=(now - timedelta(hours=3)).isoformat(),
            created_at=(now - timedelta(hours=5)).isoformat(),
        )
        conn.execute(
            f"UPDATE {ENVELOPES_TABLE} SET id = %s, ciphertext = NULL, key_version = NULL, "
            "recovery_expires_at = NULL, purged_at = %s WHERE scope = 'scope-purge-done'",
            (already_purged_id, (now - timedelta(hours=2)).isoformat()),
        )
        conn.commit()

        purged = purge_expired_envelopes(conn, now=now)
        assert purged == 1

        expired_row = _fetch_envelope(conn, expired_id)
        assert expired_row[0] is None  # ciphertext
        assert expired_row[1] is None  # key_version
        assert expired_row[2] is None  # recovery_expires_at
        assert expired_row[3] is not None  # purged_at

        live_row = _fetch_envelope(conn, live_id)
        assert live_row[0] is not None
        assert live_row[1] == 1
        assert live_row[3] is None

        already_purged_row = _fetch_envelope(conn, already_purged_id)
        assert already_purged_row[3] is not None

        # Idempotent: a second run finds nothing new to purge.
        assert purge_expired_envelopes(conn, now=now) == 0


def test_purged_envelope_is_no_longer_recoverable(envelope_dsn: str) -> None:
    """After the purge, load shows no ciphertext — the route answers 409."""
    from app.customer_idempotency import load_envelope

    now = datetime.now(UTC)
    scope = "scope-purge-recovery"
    key_digest = idempotency_key_digest("key-purge-recovery")
    req_hash = request_hash(
        {"activation_code": "XS04-AAAA-BBBB-0001", "device_fingerprint": "fp-1"}
    )

    with psycopg.connect(envelope_dsn) as conn:
        _insert_completed_envelope(
            conn,
            seq=1,
            scope=scope,
            key_digest=key_digest,
            req_hash=req_hash,
            ciphertext="x",
            recovery_expires_at=(now - timedelta(minutes=1)).isoformat(),
            created_at=(now - timedelta(hours=2)).isoformat(),
        )
        conn.commit()

        assert purge_expired_envelopes(conn, now=now) == 1
        record = load_envelope(conn, operation="activate", scope=scope, key_digest=key_digest)

    assert record is not None
    assert record.ciphertext is None
    assert record.key_version is None


def test_recovery_scan_index_exists(envelope_dsn: str) -> None:
    """029's plain btree on the text column must stay in place.

    Session review asked for a sargable expression index on
    ``recovery_expires_at::timestamptz`` for the daily cleanup scan. That is
    a PostgreSQL dead end, verified against the fixture (PG16):
    ``functions in index expression must be marked IMMUTABLE`` — the
    ``text -> timestamptz`` cast consults the TimeZone GUC, so PG refuses it
    in any index expression. Wrapping the cast in a hand-rolled IMMUTABLE
    function would lie to the planner (the result still depends on the
    session TimeZone) and silently corrupt the index when the GUC changes.

    The cleanup runs once per day on a table bounded by the number of
    successful activations (review's own sizing), so the sequential scan is
    an accepted known limitation; revisit together with a column-type
    migration to a real timestamptz column, where this plain index would
    carry the scan natively.
    """
    with psycopg.connect(envelope_dsn) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s", (ENVELOPES_TABLE,)
            ).fetchall()
        }
    assert "idx_customer_idempotency_envelopes_recovery" in indexes


# ---------------------------------------------------------------------------
# The maintenance CLI (scripts/purge_idempotency_envelopes.py)
# ---------------------------------------------------------------------------


def test_cli_purges_expired_envelopes(envelope_dsn: str) -> None:
    from scripts.purge_idempotency_envelopes import main as purge_main

    now = datetime.now(UTC)
    with psycopg.connect(envelope_dsn) as conn:
        envelope_id = _insert_completed_envelope(
            conn,
            seq=1,
            scope="scope-cli",
            key_digest=idempotency_key_digest("key-cli"),
            req_hash="hash-cli",
            ciphertext="x",
            recovery_expires_at=(now - timedelta(hours=1)).isoformat(),
            created_at=(now - timedelta(hours=3)).isoformat(),
        )
        conn.commit()

    assert purge_main(["--database-url", envelope_dsn]) == 0

    with psycopg.connect(envelope_dsn) as conn:
        row = _fetch_envelope(conn, envelope_id)
    assert row[0] is None
    assert row[3] is not None


def test_cli_dry_run_keeps_rows(envelope_dsn: str) -> None:
    from scripts.purge_idempotency_envelopes import main as purge_main

    now = datetime.now(UTC)
    with psycopg.connect(envelope_dsn) as conn:
        envelope_id = _insert_completed_envelope(
            conn,
            seq=1,
            scope="scope-dry",
            key_digest=idempotency_key_digest("key-dry"),
            req_hash="hash-dry",
            ciphertext="x",
            recovery_expires_at=(now - timedelta(hours=1)).isoformat(),
            created_at=(now - timedelta(hours=3)).isoformat(),
        )
        conn.commit()

    assert purge_main(["--database-url", envelope_dsn, "--dry-run"]) == 0

    with psycopg.connect(envelope_dsn) as conn:
        row = _fetch_envelope(conn, envelope_id)
    assert row[0] == "x"
    assert row[3] is None


def test_cli_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.purge_idempotency_envelopes import main as purge_main

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    assert purge_main([]) == 1
