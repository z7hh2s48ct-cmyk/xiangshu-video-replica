"""T11 / ACT-02 + ACT-03 — activation code generation, digests and AEAD export.

Unit cases (no PG): CSPRNG code shape and entropy, human-input normalization,
stable masking, keyed digests with a key-rotation verification window, the
six-state transition matrix (acceptance spec §2.1) and the AEAD export
envelope. PG cases (skip without the fixture): batch generation lands codes
as GENERATED with append-only events, and the one-time audited export
download (ACT-03).

No-Go red lines locked here: no predictable codes, no reversible database
fields and no plaintext codes in the database, events or logs.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import secrets
import string
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    ACTIVATION_EXPORT_AEAD_KEY_ENV,
    ALLOWED_CODE_TRANSITIONS,
    CODE_RANDOM_CHAR_COUNT,
    ActivationCodeError,
    ActivationExportError,
    ActivationKeyError,
    InvalidActivationCodeError,
    InvalidCodeTransitionError,
    activation_code_hmac_key,
    assert_code_transition,
    compute_code_digest,
    create_batch_export,
    decrypt_code_package,
    encrypt_code_package,
    export_aead_key,
    fetch_export_package,
    generate_activation_code,
    generate_batch_codes,
    is_valid_activation_code_format,
    iter_code_digests,
    mask_activation_code,
    normalize_activation_code,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

TEST_HMAC_KEY_V1 = secrets.token_urlsafe(48)  # str env value, never a real secret
TEST_HMAC_KEY_V2 = secrets.token_urlsafe(48)
TEST_AEAD_KEY = secrets.token_bytes(32)  # exactly 32 bytes, never a real secret


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Unit: CSPRNG generation, normalization, masking (ACT-02)
# ---------------------------------------------------------------------------


def test_generated_codes_match_format_and_are_unique() -> None:
    codes = [generate_activation_code() for _ in range(500)]
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert is_valid_activation_code_format(code)
        assert normalize_activation_code(code) == code


def test_generated_code_entropy_structure() -> None:
    # 28 random Crockford-base32 characters carry 140 bits >= the required 128.
    assert CODE_RANDOM_CHAR_COUNT * math.log2(32) >= 128
    sample = "".join(generate_activation_code() for _ in range(500))
    alphabet = {character for character in sample if character != "-"}
    # Every alphabet character occurs across the sample: no degenerate subset.
    for digit in "0123456789":
        assert digit in alphabet
    for letter in "ABCDEFGHJKMNPQRSTVWXYZ":
        assert letter in alphabet
    # Confusable glyphs never appear.
    for forbidden in "ILOU":
        assert forbidden not in alphabet


def test_normalize_accepts_human_variants() -> None:
    canonical = generate_activation_code()
    compact = canonical.replace("-", "")
    variants = [
        canonical,
        canonical.lower(),
        compact,
        " ".join(compact[i : i + 4] for i in range(0, len(compact), 4)),
    ]
    for variant in variants:
        assert normalize_activation_code(variant) == canonical
    # Typing O/I/L where 0/1 was meant still resolves to the same code —
    # deterministically seeded so the confusable targets provably exist.
    chars = list(compact)
    chars[4], chars[5] = "0", "1"
    rebuilt = "-".join(["XS04"] + ["".join(chars[4 + i : 4 + i + 7]) for i in range(0, 28, 7)])
    swapped = rebuilt.replace("0", "O").replace("1", "I", 1).replace("1", "L", 1)
    assert normalize_activation_code(swapped) == rebuilt


def test_invalid_formats_rejected() -> None:
    canonical = generate_activation_code()
    # Swap one character that provably exists for a letter outside the alphabet.
    malformed = [
        "",
        "   ",
        canonical.replace("XS04", "XS05"),  # wrong product prefix
        canonical[:-1],  # too short
        canonical + "A",  # too long
        canonical.replace(canonical[10], "U", 1),  # letter outside the alphabet
        "XS04-" + "A" * 7 + "-" + "A" * 7 + "-" + "A" * 7 + "-" + "A" * 8,  # ragged group
    ]
    for candidate in malformed:
        assert not is_valid_activation_code_format(candidate)
        with pytest.raises(InvalidActivationCodeError):
            normalize_activation_code(candidate)


def test_mask_is_stable_and_hides_randomness() -> None:
    code = generate_activation_code()
    masked = mask_activation_code(code)
    assert masked == mask_activation_code(code)
    assert len(masked) == len(code)
    groups = code.split("-")
    masked_groups = masked.split("-")
    assert masked_groups[0] == groups[0] == "XS04"
    assert masked_groups[1] == f"{groups[1][:4]}***"
    assert masked_groups[2] == "*" * 7
    assert masked_groups[3] == "*" * 7
    assert masked_groups[4] == f"***{groups[4][3:]}"
    # The 20 hidden characters never leak into the masked form: every visible
    # character comes from the prefix or the two exposed 4-character ends.
    visible = {character for character in masked if character not in {"*", "-"}}
    assert visible <= set("XS04" + groups[1][:4] + groups[4][3:])


# ---------------------------------------------------------------------------
# Unit: keyed digests and key rotation (ACT-02)
# ---------------------------------------------------------------------------


def test_digest_deterministic_keyed_hex() -> None:
    code = generate_activation_code()
    first = compute_code_digest(code, key=TEST_HMAC_KEY_V1.encode())
    assert first == compute_code_digest(code, key=TEST_HMAC_KEY_V1.encode())
    other_key = secrets.token_bytes(48)
    assert first != compute_code_digest(code, key=other_key)
    assert len(first) == 64
    assert all(c in string.hexdigits for c in first)
    # Human variants digest identically after normalization.
    assert first == compute_code_digest(
        code.lower().replace("-", " "), key=TEST_HMAC_KEY_V1.encode()
    )


def test_hmac_key_env_resolution() -> None:
    environ = {f"{ACTIVATION_CODE_HMAC_KEY_ENV}_V2": TEST_HMAC_KEY_V2}
    assert activation_code_hmac_key(2, environ=environ) == TEST_HMAC_KEY_V2.encode()
    # Version 1 also accepts the un-suffixed variable (T09 precedent).
    environ = {ACTIVATION_CODE_HMAC_KEY_ENV: TEST_HMAC_KEY_V1}
    assert activation_code_hmac_key(1, environ=environ) == TEST_HMAC_KEY_V1.encode()
    # A too-short key fails closed.
    environ = {f"{ACTIVATION_CODE_HMAC_KEY_ENV}_V1": "short"}
    with pytest.raises(ActivationKeyError, match="at least"):
        activation_code_hmac_key(1, environ=environ)
    # A missing version is an explicit error, never a silent fallback.
    with pytest.raises(ActivationKeyError, match="not configured"):
        activation_code_hmac_key(9, environ={})


def test_key_rotation_verification_window() -> None:
    code = generate_activation_code()
    v1_digest = compute_code_digest(code, key=TEST_HMAC_KEY_V1.encode())
    environ = {
        f"{ACTIVATION_CODE_HMAC_KEY_ENV}_V1": TEST_HMAC_KEY_V1,
        f"{ACTIVATION_CODE_HMAC_KEY_ENV}_V2": TEST_HMAC_KEY_V2,
    }
    candidates = {digest: version for digest, version in iter_code_digests(code, environ=environ)}
    # The old-version digest stays verifiable while both keys are configured.
    assert candidates[v1_digest] == 1
    # New codes are digested with the highest configured version.
    assert compute_code_digest(code, key=TEST_HMAC_KEY_V2.encode()) in candidates
    # Highest version is yielded first.
    versions = [version for _, version in iter_code_digests(code, environ=environ)]
    assert versions == sorted(versions, reverse=True)
    with pytest.raises(ActivationKeyError):
        list(iter_code_digests(code, environ={}))


# ---------------------------------------------------------------------------
# Unit: six-state transition matrix (acceptance spec §2.1)
# ---------------------------------------------------------------------------


def test_status_transition_matrix() -> None:
    states = {"GENERATED", "ISSUED", "ACTIVE", "SUSPENDED", "REVOKED", "EXPIRED"}
    assert set(ALLOWED_CODE_TRANSITIONS) == states
    legal = {
        "GENERATED": {"ISSUED", "EXPIRED", "REVOKED"},
        "ISSUED": {"ACTIVE", "SUSPENDED", "EXPIRED", "REVOKED"},
        "ACTIVE": {"SUSPENDED", "REVOKED"},
        # M2 review M6: no SUSPENDED→EXPIRED — 027's shape CHECK couples
        # EXPIRED with suspended_at IS NULL, so a direct transition would
        # either violate the database or erase the suspension audit. A
        # suspended code reaches its end state via REVOKED (which may carry
        # suspended_at) or after an explicit resume.
        "SUSPENDED": {"ACTIVE", "REVOKED"},
        "REVOKED": set(),
        "EXPIRED": set(),
    }
    for state in states:
        assert set(ALLOWED_CODE_TRANSITIONS[state]) == legal[state]
        for target in states:
            if target in legal[state]:
                assert_code_transition(state, target)
            else:
                with pytest.raises(InvalidCodeTransitionError):
                    assert_code_transition(state, target)
    for bogus in ("", "DRAFT", "issued"):
        with pytest.raises(InvalidCodeTransitionError):
            assert_code_transition("ISSUED", bogus)


# ---------------------------------------------------------------------------
# Unit: AEAD export envelope (ACT-03)
# ---------------------------------------------------------------------------


def test_export_encrypt_roundtrip_and_no_plaintext() -> None:
    codes = [generate_activation_code() for _ in range(3)]
    ciphertext, digest = encrypt_code_package(codes, key=TEST_AEAD_KEY, batch_id="batch-x")
    for code in codes:
        assert code not in ciphertext
        assert code.replace("-", "") not in ciphertext
    assert len(digest) == 64
    assert all(c in string.hexdigits for c in digest)
    assert decrypt_code_package(ciphertext, key=TEST_AEAD_KEY, batch_id="batch-x") == codes


def test_export_tamper_and_wrong_context_rejected() -> None:
    codes = [generate_activation_code()]
    ciphertext, _ = encrypt_code_package(codes, key=TEST_AEAD_KEY, batch_id="batch-a")
    # Flip one payload byte inside the base64 body.
    raw = bytearray(base64.urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    with pytest.raises(ActivationExportError, match="verification failed"):
        decrypt_code_package(tampered, key=TEST_AEAD_KEY, batch_id="batch-a")
    # The envelope is bound to its batch: replay under another batch id fails.
    with pytest.raises(ActivationExportError):
        decrypt_code_package(ciphertext, key=TEST_AEAD_KEY, batch_id="batch-b")


def test_export_aead_key_resolution() -> None:
    environ = {f"{ACTIVATION_EXPORT_AEAD_KEY_ENV}_V1": _b64key(TEST_AEAD_KEY)}
    assert export_aead_key(1, environ=environ) == TEST_AEAD_KEY
    with pytest.raises(ActivationKeyError):
        export_aead_key(2, environ=environ)
    environ = {f"{ACTIVATION_EXPORT_AEAD_KEY_ENV}_V1": "not-base64-!!!"}
    with pytest.raises(ActivationKeyError):
        export_aead_key(1, environ=environ)
    environ = {f"{ACTIVATION_EXPORT_AEAD_KEY_ENV}_V1": _b64key(b"short")}
    with pytest.raises(ActivationKeyError, match="at least"):
        export_aead_key(1, environ=environ)
    # PR #42 P2: AES-GCM accepts only 16/24/32-byte keys and this is the
    # AES-256 path — a longer key must fail at configuration resolution,
    # not as a raw ValueError inside AESGCM().
    environ = {f"{ACTIVATION_EXPORT_AEAD_KEY_ENV}_V1": _b64key(secrets.token_bytes(48))}
    with pytest.raises(ActivationKeyError, match="exactly 32 bytes"):
        export_aead_key(1, environ=environ)


# ---------------------------------------------------------------------------
# PG integration: generation, export persistence and one-time download
# ---------------------------------------------------------------------------


T11_DB_NAME = "t11_activation_code_service"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t11_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T11_DB_NAME}"


@pytest.fixture(scope="module")
def service_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with an operator and a customer user."""
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T11_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T11_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t11_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t11_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u-admin', 'u-admin', 'Admin', 'admin')"
        )
    try:
        yield _t11_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T11_DB_NAME}" WITH (FORCE)')


def _insert_batch(conn: psycopg.Connection, batch_id: str, quantity: int = 10) -> None:
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES (%s, %s, 1500, 1500, 100, %s, '2099-01-01T00:00:00+00:00', 'OPEN', 'u-admin')",
        (batch_id, f"Batch {batch_id}", quantity),
    )


@pytest.fixture()
def catalog_db(service_pg_dsn: str) -> Iterator[psycopg.Connection]:
    """Per-test connection; every test rolls back to isolate the catalog."""
    conn = psycopg.connect(service_pg_dsn)
    try:
        yield conn
        conn.rollback()
    finally:
        conn.close()


def test_generate_batch_codes_lands_generated_state(catalog_db: psycopg.Connection) -> None:
    _insert_batch(catalog_db, "batch-1", quantity=10)
    generated = generate_batch_codes(
        catalog_db,
        "batch-1",
        quantity=10,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
        request_id="req-1",
    )
    assert len(generated) == 10
    assert len({record.code_id for record in generated}) == 10
    rows = catalog_db.execute(
        "SELECT id, code_digest, digest_key_version, masked_code, status, issued_at "
        "FROM activation_codes WHERE batch_id = %s",
        ("batch-1",),
    ).fetchall()
    assert len(rows) == 10
    expected = {record.code_id: record for record in generated}
    for code_id, digest, key_version, masked, status, issued_at in rows:
        record = expected[code_id]
        assert digest == record.code_digest
        assert key_version == 1
        assert masked == record.masked_code
        assert status == "GENERATED"
        assert issued_at is None
    events = catalog_db.execute(
        "SELECT code_id, event, actor_user_id, request_id FROM activation_code_events "
        "WHERE code_id = ANY(%s) ORDER BY code_id",
        ([record.code_id for record in generated],),
    ).fetchall()
    assert len(events) == 10
    for code_id, event, actor, request_id in events:
        assert event == "GENERATED"
        assert actor == "u-admin"
        assert request_id == "req-1"
    # Red line: no plaintext code anywhere in the catalog tables.
    plaintext_hits = catalog_db.execute(
        "SELECT count(*) FROM activation_codes WHERE masked_code LIKE %s OR code_digest LIKE %s",
        (f"%{generated[0].plaintext_code}%", f"%{generated[0].plaintext_code}%"),
    ).fetchone()[0]
    assert plaintext_hits == 0


def test_generate_batch_codes_rejects_unknown_batch(catalog_db: psycopg.Connection) -> None:
    with pytest.raises(ActivationCodeError, match="unknown batch"):
        generate_batch_codes(
            catalog_db,
            "batch-ghost",
            quantity=5,
            key_version=1,
            hmac_key=TEST_HMAC_KEY_V1.encode(),
            actor_user_id="u-admin",
        )


def test_generate_batch_codes_rejects_overrun(catalog_db: psycopg.Connection) -> None:
    # The batch quantity snapshot is the issuance budget: filling it and
    # asking for one more code must fail closed without minting anything.
    _insert_batch(catalog_db, "batch-cap", quantity=3)
    generate_batch_codes(
        catalog_db,
        "batch-cap",
        quantity=3,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    with pytest.raises(ActivationCodeError, match="budget exceeded"):
        generate_batch_codes(
            catalog_db,
            "batch-cap",
            quantity=1,
            key_version=1,
            hmac_key=TEST_HMAC_KEY_V1.encode(),
            actor_user_id="u-admin",
        )
    landed = catalog_db.execute(
        "SELECT count(*) FROM activation_codes WHERE batch_id = %s", ("batch-cap",)
    ).fetchone()[0]
    assert landed == 3


def test_generate_batch_codes_serializes_on_batch_row_lock(service_pg_dsn: str) -> None:
    # PR #42 P1: two concurrent generators must not read the same budget and
    # jointly overshoot. The in-flight transaction holds the batch row lock,
    # so a second generator waits on that lock (lock-timeout proves it) and,
    # once the first commits, sees the spent budget and fails closed.
    first = psycopg.connect(service_pg_dsn)
    second = psycopg.connect(service_pg_dsn)
    try:
        _insert_batch(first, "batch-lock", quantity=3)
        first.commit()  # the batch row itself must be visible before racing
        generate_batch_codes(
            first,
            "batch-lock",
            quantity=3,
            key_version=1,
            hmac_key=TEST_HMAC_KEY_V1.encode(),
            actor_user_id="u-admin",
        )
        second.execute("SET LOCAL lock_timeout = '2s'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            generate_batch_codes(
                second,
                "batch-lock",
                quantity=1,
                key_version=1,
                hmac_key=TEST_HMAC_KEY_V1.encode(),
                actor_user_id="u-admin",
            )
        second.rollback()
        first.commit()
        with pytest.raises(ActivationCodeError, match="budget exceeded"):
            generate_batch_codes(
                second,
                "batch-lock",
                quantity=1,
                key_version=1,
                hmac_key=TEST_HMAC_KEY_V1.encode(),
                actor_user_id="u-admin",
            )
    finally:
        second.rollback()
        second.close()
        first.rollback()
        first.close()


def test_generated_digests_unique_across_batches(catalog_db: psycopg.Connection) -> None:
    _insert_batch(catalog_db, "batch-a", quantity=60)
    _insert_batch(catalog_db, "batch-b", quantity=60)
    first = generate_batch_codes(
        catalog_db,
        "batch-a",
        quantity=60,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    second = generate_batch_codes(
        catalog_db,
        "batch-b",
        quantity=60,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    digests = [record.code_digest for record in first + second]
    assert len(set(digests)) == len(digests)


def test_export_one_time_download_audited(
    catalog_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _insert_batch(catalog_db, "batch-1", quantity=5)
    generated = generate_batch_codes(
        catalog_db,
        "batch-1",
        quantity=5,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    # caplog spans the package's whole life: create plus both downloads.
    with caplog.at_level(logging.DEBUG):
        export_id = create_batch_export(
            catalog_db,
            "batch-1",
            generated,
            requested_by_user_id="u-admin",
            ttl_seconds=600,
            key_version=1,
            aead_key=TEST_AEAD_KEY,
        )
    row = catalog_db.execute(
        "SELECT ciphertext_sha256, expires_at, downloaded_at, downloaded_by_user_id "
        "FROM activation_code_exports WHERE id = %s",
        (export_id,),
    ).fetchone()
    assert row is not None
    _, expires_at, downloaded_at, downloaded_by = row
    assert downloaded_at is None
    assert downloaded_by is None
    assert datetime.fromisoformat(expires_at) > datetime.now(UTC)

    with caplog.at_level(logging.DEBUG):
        codes = fetch_export_package(
            catalog_db,
            export_id,
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-1",
        )
        assert codes == [record.plaintext_code for record in generated]
        audited = catalog_db.execute(
            "SELECT downloaded_at, downloaded_by_user_id, download_reason, download_request_id "
            "FROM activation_code_exports WHERE id = %s",
            (export_id,),
        ).fetchone()
        assert audited is not None and audited[0] is not None
        assert audited[1] == "u-admin"
        assert audited[2] == "渠道取件"
        assert audited[3] == "req-download-1"

        # One-time: a second download is refused and never rewrites the audit.
        with pytest.raises(ActivationExportError, match="already downloaded"):
            fetch_export_package(
                catalog_db,
                export_id,
                downloaded_by_user_id="u-admin",
                aead_keys={1: TEST_AEAD_KEY},
                download_reason="渠道取件",
                download_request_id="req-download-2",
            )
    # Red line: the plaintext never leaks into any log record.
    for record in generated:
        assert record.plaintext_code not in caplog.text


def test_expired_export_rejected(catalog_db: psycopg.Connection) -> None:
    _insert_batch(catalog_db, "batch-1", quantity=2)
    generated = generate_batch_codes(
        catalog_db,
        "batch-1",
        quantity=2,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    export_id = create_batch_export(
        catalog_db,
        "batch-1",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=60,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
        now=now,
    )
    future = now + timedelta(seconds=61)
    with pytest.raises(ActivationExportError, match="expired"):
        fetch_export_package(
            catalog_db,
            export_id,
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-expired",
            now=future,
        )
    row = catalog_db.execute(
        "SELECT downloaded_at FROM activation_code_exports WHERE id = %s", (export_id,)
    ).fetchone()
    assert row is not None and row[0] is None


def test_export_error_taxonomy_is_typed_not_text_matched(
    catalog_db: psycopg.Connection,
) -> None:
    """A7（2026-09-02 评估）：路由层按异常类型分派状态码，禁止匹配消息文本.

    服务层改一条错误文案不再可能把 404/409 静默变成 503。
    """
    from app.activation_code_service import (
        ExportAlreadyDownloadedError,
        ExportKeyUnavailableError,
        ExportPackageNotFoundError,
    )

    with pytest.raises(ExportPackageNotFoundError):
        fetch_export_package(
            catalog_db,
            "export-ghost",
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-ghost",
        )

    _insert_batch(catalog_db, "batch-tax", quantity=2)
    generated = generate_batch_codes(
        catalog_db,
        "batch-tax",
        quantity=2,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    export_id = create_batch_export(
        catalog_db,
        "batch-tax",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=600,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
    )

    # 密钥版本缺失 → 503 专属类型。
    with pytest.raises(ExportKeyUnavailableError):
        fetch_export_package(
            catalog_db,
            export_id,
            downloaded_by_user_id="u-admin",
            aead_keys={2: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-key",
        )

    # 已下载 → 409 专属类型。
    fetch_export_package(
        catalog_db,
        export_id,
        downloaded_by_user_id="u-admin",
        aead_keys={1: TEST_AEAD_KEY},
        download_reason="渠道取件",
        download_request_id="req-download-first",
    )
    with pytest.raises(ExportAlreadyDownloadedError):
        fetch_export_package(
            catalog_db,
            export_id,
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-second",
        )


def test_expired_export_error_is_typed(catalog_db: psycopg.Connection) -> None:
    from app.activation_code_service import ExportExpiredError

    _insert_batch(catalog_db, "batch-exp", quantity=1)
    generated = generate_batch_codes(
        catalog_db,
        "batch-exp",
        quantity=1,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    export_id = create_batch_export(
        catalog_db,
        "batch-exp",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=60,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
        now=now,
    )
    with pytest.raises(ExportExpiredError):
        fetch_export_package(
            catalog_db,
            export_id,
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-expired-typed",
            now=now + timedelta(seconds=61),
        )


def test_export_events_recorded(catalog_db: psycopg.Connection) -> None:
    _insert_batch(catalog_db, "batch-1", quantity=3)
    generated = generate_batch_codes(
        catalog_db,
        "batch-1",
        quantity=3,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    create_batch_export(
        catalog_db,
        "batch-1",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=600,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
    )
    rows = catalog_db.execute(
        "SELECT event, actor_user_id FROM activation_code_events "
        "WHERE code_id = ANY(%s) AND event = 'EXPORTED'",
        ([record.code_id for record in generated],),
    ).fetchall()
    assert len(rows) == 3
    for event, actor in rows:
        assert event == "EXPORTED"
        assert actor == "u-admin"


def test_export_rejects_cross_batch_codes(catalog_db: psycopg.Connection) -> None:
    # Sealing batch-a codes under batch-b would poison the AAD binding and
    # the EXPORTED trail with contradictory facts; it must fail before the
    # envelope or any row is written.
    _insert_batch(catalog_db, "batch-a", quantity=1)
    _insert_batch(catalog_db, "batch-b", quantity=1)
    foreign = generate_batch_codes(
        catalog_db,
        "batch-a",
        quantity=1,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    with pytest.raises(ActivationExportError, match="belong to the batch"):
        create_batch_export(
            catalog_db,
            "batch-b",
            foreign,
            requested_by_user_id="u-admin",
            ttl_seconds=600,
            key_version=1,
            aead_key=TEST_AEAD_KEY,
        )
    exports = catalog_db.execute(
        "SELECT count(*) FROM activation_code_exports WHERE batch_id = %s", ("batch-b",)
    ).fetchone()[0]
    assert exports == 0


def test_fetch_unknown_export_rejected(catalog_db: psycopg.Connection) -> None:
    with pytest.raises(ActivationExportError, match="unknown"):
        fetch_export_package(
            catalog_db,
            "export-ghost",
            downloaded_by_user_id="u-admin",
            aead_keys={1: TEST_AEAD_KEY},
            download_reason="渠道取件",
            download_request_id="req-download-unknown",
        )


# ---------------------------------------------------------------------------
# M2 review M2 — expired export ciphertext purge
# ---------------------------------------------------------------------------


def test_expired_export_ciphertext_purged_but_audit_kept(
    catalog_db: psycopg.Connection,
) -> None:
    """M2 review M2: an export package's ciphertext is sealed plaintext —
    once the retention window past expiry lapses, the purge must null the
    payload (ciphertext, digest, key version) while the audit columns stay
    intact, and the CHECK coupling must keep a purged row structurally
    unable to carry a secret again."""
    from app.activation_code_service import (
        count_expired_export_ciphertexts,
        purge_expired_export_ciphertexts,
    )

    _insert_batch(catalog_db, "batch-purge", quantity=2)
    generated = generate_batch_codes(
        catalog_db,
        "batch-purge",
        quantity=2,
        key_version=1,
        hmac_key=TEST_HMAC_KEY_V1.encode(),
        actor_user_id="u-admin",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    # One export lapsed past the retention window, one still inside it.
    # create_batch_export stamps created_at from the DB clock, so the
    # lapsed row is rewound afterwards (expires stays after created).
    lapsed = create_batch_export(
        catalog_db,
        "batch-purge",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=60,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
        now=now,
    )
    catalog_db.execute(
        "UPDATE activation_code_exports SET created_at = %s, expires_at = %s WHERE id = %s",
        (
            (now - timedelta(hours=3)).isoformat(),
            (now - timedelta(hours=2)).isoformat(),
            lapsed,
        ),
    )
    fresh = create_batch_export(
        catalog_db,
        "batch-purge",
        generated,
        requested_by_user_id="u-admin",
        ttl_seconds=3600,
        key_version=1,
        aead_key=TEST_AEAD_KEY,
        now=now,
    )
    # A usable activation code still needs to be viewable and copied again,
    # so an old export must remain sealed while any code in the batch is
    # non-terminal.
    assert count_expired_export_ciphertexts(catalog_db, now=now, retention_seconds=3600) == 0
    assert purge_expired_export_ciphertexts(catalog_db, now=now, retention_seconds=3600) == 0
    catalog_db.execute(
        "UPDATE activation_codes SET status = 'EXPIRED', expired_at = %s WHERE batch_id = %s",
        (now.isoformat(), "batch-purge"),
    )
    assert count_expired_export_ciphertexts(catalog_db, now=now, retention_seconds=3600) == 1
    assert purge_expired_export_ciphertexts(catalog_db, now=now, retention_seconds=3600) == 1
    row = catalog_db.execute(
        "SELECT ciphertext, ciphertext_sha256, key_version, purged_at, "
        "requested_by_user_id, created_at "
        "FROM activation_code_exports WHERE id = %s",
        (lapsed,),
    ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] is None and row[2] is None
    assert row[3] is not None, "the purge must stamp purged_at"
    assert row[4] == "u-admin" and row[5] is not None  # audit columns intact
    kept = catalog_db.execute(
        "SELECT ciphertext IS NOT NULL, purged_at FROM activation_code_exports WHERE id = %s",
        (fresh,),
    ).fetchone()
    assert kept is not None and kept[0] and kept[1] is None

    # The purge is idempotent and the CHECK coupling refuses re-arming a
    # purged row with a payload.
    assert purge_expired_export_ciphertexts(catalog_db, now=now, retention_seconds=3600) == 0
    with pytest.raises(psycopg.errors.CheckViolation):
        catalog_db.execute(
            "UPDATE activation_code_exports SET ciphertext = %s WHERE id = %s",
            ("re-armed", lapsed),
        )
