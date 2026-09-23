"""Customer-facing password hashing (CW-076 self-service registration).

Mirrors the memory-hard scrypt scheme the administrator login already uses in
``admin_auth_routes``, but as a standalone, customer-appropriately-named module
so the registration/login lanes never import the admin auth file (which is
under separate active development — importing it would couple this lane to
another task's in-flight edits).

Only a salted scrypt digest is ever produced: plaintext passwords are hashed
at the boundary and never persisted, logged or returned. The scheme is
stdlib-only (``hashlib.scrypt`` + ``secrets`` + ``hmac``) — no new third-party
dependency — and uses identical parameters to the admin store so the two
password databases stay cost-consistent.

The encoded format is ``scrypt$N$r$p$<b64 salt>$<b64 digest>`` (url-safe
base64, padding stripped). ``verify_password`` fails closed on any malformed
encoding and compares in constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Identical to the administrator scrypt parameters so both password stores are
# memory-hard at the same cost. N=2**14 (16 MiB), r=8, p=1, 32-byte digest.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

# Customer policy confirmed 2026-09-12; administrator policy stays independent.
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128

_SCRYPT_ALGORITHM = "scrypt"


class PasswordPolicyError(ValueError):
    """Raised when a password violates the registration policy."""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def validate_password_policy(password: str) -> None:
    """Reject passwords outside the length floor/ceiling or blank-only."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must contain {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters"
        )
    if not password.strip():
        raise PasswordPolicyError("password must not contain only whitespace")


def _scrypt_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_password(password: str) -> str:
    """Return a salted memory-hard password hash; plaintext is never retained."""
    validate_password_policy(password)
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt_digest(password, salt)
    return "$".join(
        (
            _SCRYPT_ALGORITHM,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a stored password hash and fail closed on malformed encodings."""
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if algorithm != _SCRYPT_ALGORITHM:
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        if len(salt) != SCRYPT_SALT_BYTES or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)
