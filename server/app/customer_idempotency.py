"""T14 / ACT-07 — the customer idempotency-envelope engine.

The extracted, reusable core of the T13 activation envelope (dev doc
§11.2/§12.1), so the later customer write paths (T17 second-device enroll,
T19 login, T22 recharge) can share one idempotency contract:

- operation/scope/key digest — exactly one envelope per triple (the raw
  client key never reaches the database, only a domain-separated keyed
  digest; legacy SHA-256 rows remain readable during the migration window);
- request hash — the frozen fingerprint of the *normalized* request; a
  retry with the same key and the same parameters replays the sealed
  response, a retry with different parameters answers 409;
- AEAD response recovery — the one-time response is sealed with
  AES-256-GCM under ``VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY`` with
  ``operation/scope/key_digest`` bound as AAD, so a ciphertext can never be
  replayed against a different envelope row, and no directly usable
  plaintext secret ever persists (ACT-07 red line — the ciphertext is the
  only stored copy);
- expiry and cleanup (the T14 story) — every completed envelope carries a
  recovery window (default 24 h, env-overridable); once it lapses the key
  is spent and the route answers 409, and ``purge_expired_envelopes``
  nulls the ciphertext under ``purged_at`` so the table cannot grow
  unboundedly (the 029 CHECK coupling keeps a purged row payload-free).

Keys are versioned like the activation-code keys (``_V<n>``; version 1
also accepts the bare name). During a rotation window several versions
stay configured at once: recovery opens under the envelope's own recorded
version, new envelopes seal under the highest configured version.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY"
CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS"

DEFAULT_RECOVERY_WINDOW_SECONDS = 24 * 60 * 60
AESGCM_KEY_BYTES = 32
AESGCM_NONCE_BYTES = 12
MAX_KEY_VERSION = 64

ENVELOPES_TABLE = "customer_idempotency_envelopes"


class IdempotencyKeyError(Exception):
    """A server-side key/ciphertext problem — the route answers 503, not 500.

    Raised when the AEAD key for a version is missing or malformed, or a
    stored ciphertext fails verification (an envelope's key version was
    retired inside the recovery window, or the ciphertext is corrupt).
    """


@dataclass(frozen=True)
class EnvelopeRecord:
    """The loaded state of one envelope row (never the raw client key)."""

    request_hash: str
    ciphertext: str | None
    key_version: int | None
    recovery_expires_at: str | None
    purged_at: str | None


# ---------------------------------------------------------------------------
# Versioned AEAD keys (T11 activation-code-service precedent)
# ---------------------------------------------------------------------------


def _env_key_candidates(base_env: str, key_version: int) -> list[str]:
    candidates = [f"{base_env}_V{key_version}"]
    if key_version == 1:
        candidates.append(base_env)
    return candidates


def configured_aead_key_versions() -> list[int]:
    return [
        version
        for version in range(1, MAX_KEY_VERSION + 1)
        if any(
            os.environ.get(name, "").strip()
            for name in _env_key_candidates(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, version)
        )
    ]


def customer_aead_key(key_version: int) -> bytes:
    """The response-envelope AEAD key (base64url, exactly 32 decoded bytes)."""
    for name in _env_key_candidates(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, key_version):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except ValueError as exc:
            raise IdempotencyKeyError(f"{name} is not valid base64") from exc
        if len(raw) != AESGCM_KEY_BYTES:
            raise IdempotencyKeyError(f"{name} must decode to exactly {AESGCM_KEY_BYTES} bytes")
        return raw
    raise IdempotencyKeyError(
        f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV} for key version {key_version} is not configured"
    )


def highest_customer_aead_key() -> tuple[int, bytes]:
    configured = configured_aead_key_versions()
    if not configured:
        raise IdempotencyKeyError(
            f"no {CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV} key version is configured"
        )
    version = max(configured)
    return version, customer_aead_key(version)


def recovery_window_seconds() -> int:
    """How long a completed envelope stays recoverable (server clock only)."""
    raw = os.environ.get(CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_RECOVERY_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r, using default %d",
            CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV,
            raw,
            DEFAULT_RECOVERY_WINDOW_SECONDS,
        )
        return DEFAULT_RECOVERY_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_RECOVERY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Digests and the request hash (the normalized-request fingerprint)
# ---------------------------------------------------------------------------


def idempotency_key_digest(key: str) -> str:
    """Return the current, domain-separated keyed digest of a client key."""
    _version, digest_key = highest_customer_aead_key()
    return _idempotency_hmac(key, digest_key)


def idempotency_key_digests(key: str) -> list[str]:
    """Accepted digests during rotation, newest first, plus legacy SHA-256."""
    digests = [
        _idempotency_hmac(key, customer_aead_key(version))
        for version in reversed(configured_aead_key_versions())
    ]
    legacy = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return list(dict.fromkeys([*digests, legacy]))


def _idempotency_hmac(key: str, digest_key: bytes) -> str:
    message = f"customer-idempotency-key:v1:{key}".encode()
    return hmac.new(digest_key, message, hashlib.sha256).hexdigest()


def request_hash(payload: Mapping[str, str]) -> str:
    """Freeze the *normalized* request: values are stripped, keys sorted.

    A retry that only differs in surrounding whitespace replays instead of
    burning the key on a 409; any different business parameter changes the
    hash and surfaces as a conflict.
    """
    normalized = {key: str(value).strip() for key, value in payload.items()}
    serialized = json.dumps(
        normalized,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The AEAD response envelope (§7 / §12.1)
# ---------------------------------------------------------------------------


def envelope_aad(operation: str, scope: str, key_digest: str) -> bytes:
    return f"customer-idempotency:{operation}:{scope}:{key_digest}".encode()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def seal_response(payload: dict[str, object], *, key: bytes, aad: bytes) -> str:
    plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    nonce = secrets.token_bytes(AESGCM_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _b64encode(nonce + sealed)


def open_response(ciphertext: str, *, key: bytes, aad: bytes) -> dict[str, object]:
    try:
        blob = _b64decode(ciphertext)
        nonce, sealed = blob[:AESGCM_NONCE_BYTES], blob[AESGCM_NONCE_BYTES:]
        plaintext = AESGCM(key).decrypt(nonce, sealed, aad)
    except (InvalidTag, ValueError) as exc:
        # A sealed envelope that this key/AAD pair cannot open is a server
        # configuration or integrity failure, not a client error — refuse
        # loudly instead of replaying garbage.
        raise IdempotencyKeyError("idempotency envelope ciphertext verification failed") from exc
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise IdempotencyKeyError("idempotency envelope payload is malformed") from exc
    if not isinstance(decoded, dict):
        raise IdempotencyKeyError("idempotency envelope payload is malformed")
    return decoded


# ---------------------------------------------------------------------------
# Envelope persistence (revision 029, `customer_idempotency_envelopes`)
# ---------------------------------------------------------------------------


def insert_envelope(
    conn: psycopg.Connection,
    *,
    operation: str,
    scope: str,
    key_digest: str,
    request_hash: str,
) -> str | None:
    """Insert the placeholder; returns its id, or ``None`` on key reuse.

    ``ON CONFLICT DO NOTHING`` keeps concurrent same-key writers serialized
    on the unique (operation, scope, key_digest) index; the loser reloads
    the committed envelope.
    """
    envelope_id = str(uuid.uuid4())
    inserted = conn.execute(
        f"INSERT INTO {ENVELOPES_TABLE} "
        "(id, operation, scope, key_digest, request_hash) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (operation, scope, key_digest) DO NOTHING",
        (envelope_id, operation, scope, key_digest, request_hash),
    ).rowcount
    return envelope_id if inserted == 1 else None


def load_envelope(
    conn: psycopg.Connection,
    *,
    operation: str,
    scope: str,
    key_digest: str,
) -> EnvelopeRecord | None:
    row = conn.execute(
        f"SELECT request_hash, ciphertext, key_version, recovery_expires_at, purged_at "
        f"FROM {ENVELOPES_TABLE} "
        "WHERE operation = %s AND scope = %s AND key_digest = %s",
        (operation, scope, key_digest),
    ).fetchone()
    if row is None:
        return None
    return EnvelopeRecord(
        request_hash=str(row[0]),
        ciphertext=row[1],
        key_version=row[2],
        recovery_expires_at=row[3],
        purged_at=row[4],
    )


def complete_envelope(
    conn: psycopg.Connection,
    envelope_id: str,
    *,
    ciphertext: str,
    key_version: int,
    recovery_expires_at: str,
) -> None:
    """Back-fill the sealed response before the transaction commits."""
    conn.execute(
        f"UPDATE {ENVELOPES_TABLE} "
        "SET ciphertext = %s, key_version = %s, recovery_expires_at = %s "
        "WHERE id = %s",
        (ciphertext, key_version, recovery_expires_at, envelope_id),
    )


# ---------------------------------------------------------------------------
# Expiry cleanup (the T14 story — dev doc §11.2 recovery window)
# ---------------------------------------------------------------------------


def _as_utc(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).replace(microsecond=0).isoformat()


def count_expired_envelopes(conn: psycopg.Connection, *, now: datetime) -> int:
    """How many completed envelopes have lapsed their recovery window."""
    row = conn.execute(
        f"SELECT count(*) FROM {ENVELOPES_TABLE} "
        "WHERE purged_at IS NULL "
        "AND ciphertext IS NOT NULL "
        "AND recovery_expires_at IS NOT NULL "
        "AND recovery_expires_at::timestamptz <= %s::timestamptz",
        (_as_utc(now),),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def purge_expired_envelopes(conn: psycopg.Connection, *, now: datetime) -> int:
    """Null the payload of every lapsed envelope; returns the purged count.

    The 029 CHECK coupling (``purged_at IS NULL OR ciphertext IS NULL`` and
    the payload three-state coupling) forces ciphertext/key_version/
    recovery_expires_at to leave together with purged_at arriving — a purged
    row is structurally unable to carry a recoverable secret. Idempotent:
    already-purged rows never match again.
    """
    purged = conn.execute(
        f"UPDATE {ENVELOPES_TABLE} "
        "SET ciphertext = NULL, key_version = NULL, recovery_expires_at = NULL, "
        "purged_at = %s "
        "WHERE purged_at IS NULL "
        "AND ciphertext IS NOT NULL "
        "AND recovery_expires_at IS NOT NULL "
        "AND recovery_expires_at::timestamptz <= %s::timestamptz",
        (_as_utc(now), _as_utc(now)),
    ).rowcount
    return int(purged)
