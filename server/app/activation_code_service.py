"""T11 / ACT-02 + ACT-03 — activation code generation, digests and AEAD export.

Application layer on top of the catalog schema published by revision 027
(``activation_code_batches`` / ``activation_codes`` / ``activation_code_exports``
/ ``activation_code_events``):

- CSPRNG codes in the ``XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX`` shape — 28
  Crockford-base32 characters carry 140 bits of entropy, comfortably above
  the 128-bit floor of acceptance spec §2.1;
- human-input normalization (case, spacing, O/I/L confusables) feeding a
  keyed HMAC-SHA256 digest — the database only ever stores the digest plus
  its key version (ACT-01 No-Go), and a stable masked display form;
- versioned HMAC / AEAD keys resolved from the environment exactly like the
  T09 admin-session precedent (``..._V{N}``, version 1 also accepting the
  un-suffixed name), so keys rotate by configuring a higher version: new
  codes digest with the highest configured version while ``iter_code_digests``
  keeps verifying codes digested under older versions;
- the six-state transition matrix from acceptance spec §2.1 for T12/T13 to
  reuse;
- one-time AEAD exports (ACT-03): a batch export is an AES-GCM envelope bound
  to its batch id (AAD), stored with a SHA-256 integrity digest and a short
  expiry; ``fetch_export_package`` is the single audited download path — one
  download, second attempts and expired packages fail closed, and the
  download actor is persisted on the export row.

No-Go red lines: no predictable codes, no reversible database fields and no
plaintext codes in the database, events or logs — plaintext lives only in
return values handed to the operator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ACTIVATION_CODE_HMAC_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"
ACTIVATION_EXPORT_AEAD_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY"

# Code shape: product prefix + 4 groups of 7 Crockford-base32 characters.
# 28 random characters x log2(32) = 140 bits of entropy (>= 128 required).
CODE_PREFIX = "XS04"
CODE_GROUP_COUNT = 4
CODE_GROUP_LENGTH = 7
CODE_RANDOM_CHAR_COUNT = CODE_GROUP_COUNT * CODE_GROUP_LENGTH
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})

MIN_HMAC_KEY_BYTES = 32
MIN_AEAD_KEY_BYTES = 32
# AES-GCM accepts exactly 16/24/32-byte keys and this module is the AES-256
# export path, so the AEAD key must decode to exactly 32 bytes (PR #42 P2).
AESGCM_KEY_BYTES = 32
MAX_KEY_VERSION = 64
AESGCM_NONCE_BYTES = 12

CODE_STATUSES = ("GENERATED", "ISSUED", "ACTIVE", "SUSPENDED", "REVOKED", "EXPIRED")
# Acceptance spec §2.1: GENERATED is the pre-delivery landing state, ISSUED
# follows delivery, activation flips ISSUED to ACTIVE, SUSPENDED/REVOKED are
# operator side states and EXPIRED is the unactivated end state (a bound code
# never expires — the 027 status-shape matrix enforces that at the database).
ALLOWED_CODE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "GENERATED": frozenset({"ISSUED", "EXPIRED", "REVOKED"}),
    "ISSUED": frozenset({"ACTIVE", "SUSPENDED", "EXPIRED", "REVOKED"}),
    "ACTIVE": frozenset({"SUSPENDED", "REVOKED"}),
    # M2 review M6: SUSPENDED→EXPIRED is deliberately absent — 027's shape
    # CHECK couples EXPIRED with suspended_at IS NULL, so a direct
    # transition would violate the database or erase the suspension audit.
    # A suspended code ends via REVOKED (which may carry suspended_at) or
    # after an explicit resume.
    "SUSPENDED": frozenset({"ACTIVE", "REVOKED"}),
    "REVOKED": frozenset(),
    "EXPIRED": frozenset(),
}


class ActivationCodeError(ValueError):
    """Base error for activation-code domain failures."""


class InvalidActivationCodeError(ActivationCodeError):
    """Raised when input cannot be a well-formed activation code."""


class InvalidCodeTransitionError(ActivationCodeError):
    """Raised when a status transition is not in the legal matrix."""


class ActivationKeyError(ActivationCodeError):
    """Raised when a versioned HMAC/AEAD key is missing or malformed."""


class ActivationExportError(ActivationCodeError):
    """Raised when an export package cannot be read, was used or expired."""


class ExportPackageNotFoundError(ActivationExportError):
    """The export id does not exist (maps to 404 at the route layer)."""


class ExportAlreadyDownloadedError(ActivationExportError):
    """The one-time package was already downloaded (maps to 409)."""


class ExportExpiredError(ActivationExportError):
    """The package outlived its TTL (maps to 409)."""


class ExportKeyUnavailableError(ActivationExportError):
    """The AEAD key version for this package is not configured (maps to 503)."""


@dataclass(frozen=True)
class GeneratedCode:
    """One freshly minted code: plaintext only in memory, never persisted."""

    code_id: str
    plaintext_code: str
    code_digest: str
    masked_code: str


# ---------------------------------------------------------------------------
# Normalization, generation, validation, masking (ACT-02)
# ---------------------------------------------------------------------------


def normalize_activation_code(raw: str) -> str:
    """Return the canonical form of a user-typed activation code.

    Accepts case differences, stripped separators and the O/I/L confusables;
    anything that cannot be the fixed 32-character Crockford shape fails
    closed. Error messages never echo the input.
    """
    if not isinstance(raw, str):
        raise InvalidActivationCodeError("activation code must be text")
    compact = "".join(character for character in raw.upper() if character.isalnum())
    compact = compact.translate(_CODE_CONFUSABLES)
    expected_length = len(CODE_PREFIX) + CODE_RANDOM_CHAR_COUNT
    if len(compact) != expected_length:
        raise InvalidActivationCodeError(
            f"activation code must be {expected_length} alphanumeric characters"
        )
    prefix, random_part = compact[: len(CODE_PREFIX)], compact[len(CODE_PREFIX) :]
    if prefix != CODE_PREFIX:
        raise InvalidActivationCodeError("activation code prefix is invalid")
    if any(character not in CODE_ALPHABET for character in random_part):
        raise InvalidActivationCodeError("activation code contains an invalid character")
    groups = "-".join(
        random_part[i : i + CODE_GROUP_LENGTH]
        for i in range(0, len(random_part), CODE_GROUP_LENGTH)
    )
    return f"{CODE_PREFIX}-{groups}"


def generate_activation_code(*, rng: random.Random | None = None) -> str:
    """Mint one activation code with the module CSPRNG.

    ``rng`` exists for deterministic fixtures (``random.Random(seed)``);
    production paths always use ``secrets.SystemRandom``.
    """
    random_source = rng if rng is not None else secrets.SystemRandom()
    characters = [random_source.choice(CODE_ALPHABET) for _ in range(CODE_RANDOM_CHAR_COUNT)]
    groups = "-".join(
        "".join(characters[i : i + CODE_GROUP_LENGTH])
        for i in range(0, len(characters), CODE_GROUP_LENGTH)
    )
    return f"{CODE_PREFIX}-{groups}"


def is_valid_activation_code_format(code: str) -> bool:
    try:
        normalize_activation_code(code)
    except InvalidActivationCodeError:
        return False
    return True


def mask_activation_code(code: str) -> str:
    """Stable display form: first 4 and last 4 random characters visible."""
    canonical = normalize_activation_code(code)
    groups = canonical.split("-")
    masked_groups = [
        groups[0],  # product prefix
        f"{groups[1][:4]}***",
        "*" * CODE_GROUP_LENGTH,
        "*" * CODE_GROUP_LENGTH,
        f"***{groups[4][3:]}",
    ]
    return "-".join(masked_groups)


# ---------------------------------------------------------------------------
# Versioned keys (T09 admin_hmac_key precedent) and keyed digests
# ---------------------------------------------------------------------------


def _env_key_candidates(base_env: str, key_version: int) -> list[str]:
    candidates = [f"{base_env}_V{key_version}"]
    if key_version == 1:
        candidates.append(base_env)
    return candidates


def _resolve_env_bytes(
    base_env: str,
    key_version: int,
    *,
    environ: Mapping[str, str] | None,
    minimum_bytes: int,
    decode: bool,
    exact_bytes: int | None = None,
) -> bytes:
    source = os.environ if environ is None else environ
    for name in _env_key_candidates(base_env, key_version):
        value = source.get(name, "").strip()
        if not value:
            continue
        try:
            raw = (
                base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                if decode
                else value.encode()
            )
        except ValueError as exc:
            raise ActivationKeyError(f"{name} is not valid base64") from exc
        if len(raw) < minimum_bytes:
            raise ActivationKeyError(
                f"{name} must decode to at least {minimum_bytes} bytes, got {len(raw)}"
            )
        if exact_bytes is not None and len(raw) != exact_bytes:
            raise ActivationKeyError(
                f"{name} must decode to exactly {exact_bytes} bytes, got {len(raw)}"
            )
        return raw
    raise ActivationKeyError(
        f"{base_env} for key version {key_version} is not configured "
        f"(expected {base_env}_V{key_version})"
    )


def activation_code_hmac_key(
    key_version: int, *, environ: Mapping[str, str] | None = None
) -> bytes:
    """Resolve the versioned code-digest HMAC key (raw bytes)."""
    return _resolve_env_bytes(
        ACTIVATION_CODE_HMAC_KEY_ENV,
        key_version,
        environ=environ,
        minimum_bytes=MIN_HMAC_KEY_BYTES,
        decode=False,
    )


def export_aead_key(key_version: int, *, environ: Mapping[str, str] | None = None) -> bytes:
    """Resolve the versioned AEAD export key (base64, exactly 32 decoded bytes).

    AES-GCM only accepts 16/24/32-byte keys and this is the AES-256 export
    path, so anything other than exactly 32 decoded bytes is a configuration
    error, not a longer-is-safer bonus (PR #42 P2).
    """
    return _resolve_env_bytes(
        ACTIVATION_EXPORT_AEAD_KEY_ENV,
        key_version,
        environ=environ,
        minimum_bytes=MIN_AEAD_KEY_BYTES,
        decode=True,
        exact_bytes=AESGCM_KEY_BYTES,
    )


def compute_code_digest(code: str, *, key: bytes) -> str:
    """HMAC-SHA256 of the normalized code, hex-encoded (64 characters).

    Key/version pairing is the caller's contract (``activation_code_hmac_key``
    resolves one from the other); the digest itself never depends on the
    version number, so the parameter is deliberately absent here.
    """
    canonical = normalize_activation_code(code)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def iter_code_digests(
    code: str, *, environ: Mapping[str, str] | None = None
) -> Iterator[tuple[str, int]]:
    """Yield ``(digest, key_version)`` for every configured key version, highest first.

    This is the rotation window: while an old version stays configured, codes
    digested under it remain verifiable; dropping the variable retires it.
    """
    canonical = normalize_activation_code(code)
    source = os.environ if environ is None else environ
    configured: list[int] = []
    for version in range(1, MAX_KEY_VERSION + 1):
        if any(
            source.get(name, "").strip()
            for name in _env_key_candidates(ACTIVATION_CODE_HMAC_KEY_ENV, version)
        ):
            configured.append(version)
    if not configured:
        raise ActivationKeyError(f"no {ACTIVATION_CODE_HMAC_KEY_ENV} key version is configured")
    for version in sorted(configured, reverse=True):
        key = activation_code_hmac_key(version, environ=source)
        yield hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest(), version


# ---------------------------------------------------------------------------
# Six-state transition matrix (acceptance spec §2.1)
# ---------------------------------------------------------------------------


def assert_code_transition(current: str, target: str) -> None:
    if current not in ALLOWED_CODE_TRANSITIONS:
        raise InvalidCodeTransitionError(f"unknown activation code status {current!r}")
    if target not in ALLOWED_CODE_TRANSITIONS[current]:
        raise InvalidCodeTransitionError(f"activation code cannot move from {current} to {target}")


# ---------------------------------------------------------------------------
# AEAD export envelope (ACT-03)
# ---------------------------------------------------------------------------


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _export_aad(batch_id: str) -> bytes:
    return f"activation-code-export:{batch_id}".encode()


def encrypt_code_package(codes: Sequence[str], *, key: bytes, batch_id: str) -> tuple[str, str]:
    """Encrypt normalized codes into one AES-GCM envelope bound to the batch.

    Returns ``(ciphertext_base64, sha256_hex)`` where the ciphertext is
    ``urlsafe_b64(nonce || ciphertext+tag)``. The plaintext codes never appear
    in either value.
    """
    if not codes:
        raise ActivationExportError("an export package requires at least one code")
    canonical_codes = [normalize_activation_code(code) for code in codes]
    payload = json.dumps(
        {"batch_id": batch_id, "codes": canonical_codes},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    nonce = secrets.token_bytes(AESGCM_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, payload, _export_aad(batch_id))
    ciphertext = _b64encode(nonce + sealed)
    return ciphertext, hashlib.sha256(ciphertext.encode("ascii")).hexdigest()


def decrypt_code_package(ciphertext: str, *, key: bytes, batch_id: str) -> list[str]:
    """Open an export envelope; tampering or wrong batch fails closed."""
    try:
        blob = _b64decode(ciphertext)
        nonce, sealed = blob[:AESGCM_NONCE_BYTES], blob[AESGCM_NONCE_BYTES:]
        payload = AESGCM(key).decrypt(nonce, sealed, _export_aad(batch_id))
    except (InvalidTag, ValueError) as exc:
        raise ActivationExportError("ciphertext verification failed") from exc
    try:
        decoded = json.loads(payload)
        batch = decoded["batch_id"]
        codes = decoded["codes"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ActivationExportError("export package payload is malformed") from exc
    if batch != batch_id or not isinstance(codes, list) or not codes:
        raise ActivationExportError("export package payload is malformed")
    return [normalize_activation_code(code) for code in codes]


# ---------------------------------------------------------------------------
# Persistence: generation lands GENERATED; export is one-time audited
# ---------------------------------------------------------------------------


def generate_batch_codes(
    conn: psycopg.Connection,
    batch_id: str,
    *,
    quantity: int,
    key_version: int,
    hmac_key: bytes,
    actor_user_id: str,
    request_id: str | None = None,
    reason: str = "",
    rng: random.Random | None = None,
) -> list[GeneratedCode]:
    """Mint ``quantity`` codes for a batch and land them as GENERATED.

    Each code row stores only the keyed digest and the masked form; the
    plaintext exists solely in the returned records. A GENERATED event per
    code keeps the append-only trail (027) — carrying the write-contract
    ``reason`` (M2 review H1: minting must not drop the audited "why").
    The caller owns the transaction.
    """
    if quantity < 1:
        raise ActivationCodeError("quantity must be at least 1")
    # The batch row is locked for the whole transaction (PR #42 P1): two API
    # instances generating for the same batch otherwise read the same budget
    # and pre-insert count, both pass this check and jointly overshoot the
    # frozen quantity. FOR UPDATE serializes the check-then-insert sequence.
    batch_row = conn.execute(
        "SELECT quantity FROM activation_code_batches WHERE id = %s FOR UPDATE",
        (batch_id,),
    ).fetchone()
    if batch_row is None:
        raise ActivationCodeError(f"unknown batch {batch_id!r}")
    count_row = conn.execute(
        "SELECT count(*) FROM activation_codes WHERE batch_id = %s", (batch_id,)
    ).fetchone()
    already_generated = 0 if count_row is None else count_row[0]
    if already_generated + quantity > batch_row[0]:
        # The batch snapshot is the frozen issuance budget (dev doc §5);
        # minting past it would sell codes the batch never priced.
        raise ActivationCodeError(
            f"batch {batch_id!r} budget exceeded: {already_generated} generated, "
            f"batch quantity is {batch_row[0]}"
        )
    generated: list[GeneratedCode] = []
    for _ in range(quantity):
        code = generate_activation_code(rng=rng)
        digest = compute_code_digest(code, key=hmac_key)
        masked_code = mask_activation_code(code)
        code_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status) "
            "VALUES (%s, %s, %s, %s, %s, 'GENERATED')",
            (code_id, batch_id, digest, key_version, masked_code),
        )
        conn.execute(
            "INSERT INTO activation_code_events "
            "(id, code_id, event, actor_user_id, request_id, reason) "
            "VALUES (%s, %s, 'GENERATED', %s, %s, %s)",
            (str(uuid.uuid4()), code_id, actor_user_id, request_id, reason),
        )
        generated.append(
            GeneratedCode(
                code_id=code_id,
                plaintext_code=code,
                code_digest=digest,
                masked_code=masked_code,
            )
        )
    return generated


def create_batch_export(
    conn: psycopg.Connection,
    batch_id: str,
    codes: Sequence[GeneratedCode],
    *,
    requested_by_user_id: str,
    ttl_seconds: int,
    key_version: int,
    aead_key: bytes,
    request_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Seal freshly generated codes into a one-time AEAD export package.

    Writes the ``activation_code_exports`` row (ciphertext, SHA-256, key
    version, short expiry) plus an EXPORTED event per code. The plaintext is
    only inside the envelope — never in a column, event or log. The 027
    exports table is the storage carrier for this task; private-COS object
    delivery is layered on top by T36 / COS-01 (task-list boundary, PR #42).
    """
    if not codes:
        raise ActivationExportError("an export requires at least one generated code")
    if ttl_seconds <= 0:
        raise ActivationExportError("export ttl must be positive")
    stray = conn.execute(
        "SELECT 1 FROM activation_codes WHERE id = ANY(%s) AND batch_id <> %s LIMIT 1",
        ([record.code_id for record in codes], batch_id),
    ).fetchone()
    if stray is not None:
        # A mixed-batch envelope would poison the AAD binding, the export row
        # and every EXPORTED event with contradictory facts.
        raise ActivationExportError("export codes must all belong to the batch")
    current = now if now is not None else datetime.now(UTC)
    expires_at = (current + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
    ciphertext, ciphertext_sha256 = encrypt_code_package(
        [record.plaintext_code for record in codes], key=aead_key, batch_id=batch_id
    )
    export_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO activation_code_exports "
        "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
        " requested_by_user_id, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            export_id,
            batch_id,
            ciphertext,
            ciphertext_sha256,
            key_version,
            requested_by_user_id,
            expires_at,
        ),
    )
    for record in codes:
        conn.execute(
            "INSERT INTO activation_code_events (id, code_id, event, actor_user_id, request_id) "
            "VALUES (%s, %s, 'EXPORTED', %s, %s)",
            (str(uuid.uuid4()), record.code_id, requested_by_user_id, request_id),
        )
    return export_id


def fetch_export_package(
    conn: psycopg.Connection,
    export_id: str,
    *,
    downloaded_by_user_id: str,
    aead_keys: Mapping[int, bytes],
    download_reason: str,
    download_request_id: str,
    now: datetime | None = None,
) -> list[str]:
    """The single audited download path for an export package.

    One-time (``downloaded_at IS NULL`` enforced under row lock) and
    expiry-checked against real timestamps; the download actor is persisted on
    the export row. ``download_reason`` and ``download_request_id`` complete
    the durable audit record (PR #43 review P1: the reason used to be
    validated and dropped). The caller owns the transaction. Private-COS
    object delivery for the controlled operator flow is layered on by T36 /
    COS-01; the audit semantics implemented here stay authoritative (PR #42).
    """
    row = conn.execute(
        "SELECT batch_id, ciphertext, key_version, expires_at, downloaded_at "
        "FROM activation_code_exports WHERE id = %s FOR UPDATE",
        (export_id,),
    ).fetchone()
    if row is None:
        raise ExportPackageNotFoundError("unknown activation code export")
    batch_id, ciphertext, key_version, expires_at, downloaded_at = row
    if downloaded_at is not None:
        raise ExportAlreadyDownloadedError("export package was already downloaded")
    current = now if now is not None else datetime.now(UTC)
    if datetime.fromisoformat(expires_at) <= current:
        raise ExportExpiredError("export package has expired")
    key = aead_keys.get(key_version)
    if key is None:
        raise ExportKeyUnavailableError(f"AEAD key for key version {key_version} is not available")
    codes = decrypt_code_package(ciphertext, key=key, batch_id=batch_id)
    updated = conn.execute(
        "UPDATE activation_code_exports "
        "SET downloaded_at = %s, downloaded_by_user_id = %s, "
        "download_reason = %s, download_request_id = %s "
        "WHERE id = %s AND downloaded_at IS NULL",
        (
            current.replace(microsecond=0).isoformat(),
            downloaded_by_user_id,
            download_reason,
            download_request_id,
            export_id,
        ),
    ).rowcount
    if updated != 1:
        raise ExportAlreadyDownloadedError("export package was already downloaded")
    return codes


# ---------------------------------------------------------------------------
# Highest configured key versions (T12 admin generation / download paths)
# ---------------------------------------------------------------------------


def _configured_key_versions(base_env: str, environ: Mapping[str, str] | None) -> list[int]:
    """Scan ``_V1.._V64`` (plus the un-suffixed version-1 alias)."""
    source = os.environ if environ is None else environ
    return [
        version
        for version in range(1, MAX_KEY_VERSION + 1)
        if any(source.get(name, "").strip() for name in _env_key_candidates(base_env, version))
    ]


def highest_code_hmac_key_version(*, environ: Mapping[str, str] | None = None) -> int:
    """Highest configured HMAC key version — new codes digest with it."""
    configured = _configured_key_versions(ACTIVATION_CODE_HMAC_KEY_ENV, environ)
    if not configured:
        raise ActivationKeyError(f"no {ACTIVATION_CODE_HMAC_KEY_ENV} key version is configured")
    return max(configured)


def highest_export_aead_key_version(*, environ: Mapping[str, str] | None = None) -> int:
    """Highest configured AEAD key version — new export packages seal with it."""
    configured = _configured_key_versions(ACTIVATION_EXPORT_AEAD_KEY_ENV, environ)
    if not configured:
        raise ActivationKeyError(f"no {ACTIVATION_EXPORT_AEAD_KEY_ENV} key version is configured")
    return max(configured)


def configured_export_aead_keys(*, environ: Mapping[str, str] | None = None) -> dict[int, bytes]:
    """Resolve every configured AEAD key, versioned.

    The download path must open export packages sealed under older key
    versions during the rotation window, so it resolves the full mapping
    rather than a single version.
    """
    source = os.environ if environ is None else environ
    keys: dict[int, bytes] = {}
    for version in _configured_key_versions(ACTIVATION_EXPORT_AEAD_KEY_ENV, source):
        keys[version] = export_aead_key(version, environ=source)
    if not keys:
        raise ActivationKeyError(f"no {ACTIVATION_EXPORT_AEAD_KEY_ENV} key version is configured")
    return keys


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC (naive input is treated as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# M2 review M2 — expired export ciphertext purge (maintenance sweep)
# ---------------------------------------------------------------------------

EXPORT_CIPHERTEXT_RETENTION_ENV = "VIDEO_REPLICA_EXPORT_CIPHERTEXT_RETENTION_SECONDS"
DEFAULT_EXPORT_CIPHERTEXT_RETENTION_SECONDS = 7 * 86400


def _export_retention_seconds(retention_seconds: int | None) -> int:
    """Retention past expiry before the ciphertext sweep may null a row.

    An explicit argument wins (CLI/tests); otherwise the env override, with
    the 7-day default sized to outlast key-version retirement windows.
    """
    if retention_seconds is not None:
        return retention_seconds
    raw = os.environ.get(EXPORT_CIPHERTEXT_RETENTION_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_EXPORT_CIPHERTEXT_RETENTION_SECONDS


def count_expired_export_ciphertexts(
    conn: psycopg.Connection, *, now: datetime, retention_seconds: int | None = None
) -> int:
    """Count lapsed exports whose whole batch is no longer usable.

    A retained envelope is the administrator's only reversible source for
    viewing and copying a code again. It must therefore survive while any
    code in the batch can still be issued, activated, resumed or revoked.
    """
    row = conn.execute(
        "SELECT count(*) FROM activation_code_exports "
        "WHERE purged_at IS NULL "
        "AND ciphertext IS NOT NULL "
        "AND (expires_at::timestamptz + make_interval(secs => %s)) <= %s::timestamptz "
        "AND NOT EXISTS ("
        "SELECT 1 FROM activation_codes "
        "WHERE activation_codes.batch_id = activation_code_exports.batch_id "
        "AND activation_codes.status NOT IN ('REVOKED', 'EXPIRED')"
        ")",
        (_export_retention_seconds(retention_seconds), _as_utc(now)),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def purge_expired_export_ciphertexts(
    conn: psycopg.Connection, *, now: datetime, retention_seconds: int | None = None
) -> int:
    """Null every lapsed payload after all codes in its batch are terminal.

    The 035 CHECK coupling (``purged_at IS NULL OR ciphertext IS NULL
    AND ...``) forces the ciphertext, its digest and the key version to
    leave together with ``purged_at`` arriving — a purged row is
    structurally unable to carry a redeemable secret while the audit
    columns (batch, requester, download moments, reason, request id) stay.
    Idempotent: already-purged rows never match again.
    """
    purged = conn.execute(
        "UPDATE activation_code_exports "
        "SET ciphertext = NULL, ciphertext_sha256 = NULL, key_version = NULL, "
        "purged_at = %s "
        "WHERE purged_at IS NULL "
        "AND ciphertext IS NOT NULL "
        "AND (expires_at::timestamptz + make_interval(secs => %s)) <= %s::timestamptz "
        "AND NOT EXISTS ("
        "SELECT 1 FROM activation_codes "
        "WHERE activation_codes.batch_id = activation_code_exports.batch_id "
        "AND activation_codes.status NOT IN ('REVOKED', 'EXPIRED')"
        ")",
        (
            _as_utc(now).isoformat(),
            _export_retention_seconds(retention_seconds),
            _as_utc(now),
        ),
    ).rowcount
    return int(purged)
