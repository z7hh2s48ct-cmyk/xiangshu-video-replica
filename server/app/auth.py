from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from fastapi import Depends, Header, HTTPException, Request

from app.db_pg import DATABASE_URL_ENV, pg_transaction
from app.db_portable import BusinessConnection

Role = Literal["employee", "admin", "auditor", "customer"]
VALID_ROLES: set[str] = {"employee", "admin", "auditor", "customer"}
DESKTOP_USER_ID_ENV = "VIDEO_REPLICA_DESKTOP_USER_ID"
ALLOW_DEV_IDENTITY_HEADER_ENV = "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER"
AUTH_MODE_ENV = "VIDEO_REPLICA_AUTH_MODE"
LEGACY_AUTH_MODES = {"desktop", "development"}


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    display_name: str
    role: Role


def get_database() -> Iterator[BusinessConnection]:
    """One request-scoped business connection.

    Customer production sets only ``VIDEO_REPLICA_DATABASE_URL``: the
    connection is a pooled PostgreSQL one owned by an explicit transaction
    (payment callbacks ride this path — they have no customer session, so
    the fenced writer does not apply; the pool's ``conn.transaction()``
    commits on success and rolls back on error). The SQLite path serves the
    internal/desktop lane.
    """
    if os.environ.get(DATABASE_URL_ENV, "").strip():
        from app.permissions import AuditedSecurityDenial, persist_security_denial

        try:
            with pg_transaction() as pg_conn:
                yield BusinessConnection.postgres(pg_conn)
        except AuditedSecurityDenial as exc:
            # pg_transaction has rolled the denied business request back.
            persist_security_denial(exc)
            raise
        return

    # CW-042-b: the SQLite/desktop lane is physically retired — customer
    # production is PostgreSQL-only and the legacy local backend is gone.
    raise HTTPException(
        status_code=503,
        detail={
            "code": "DATABASE_NOT_CONFIGURED",
            "message": "VIDEO_REPLICA_DATABASE_URL is required for API requests.",
        },
    )


Database = Annotated[BusinessConnection, Depends(get_database)]


def get_current_user(
    request: Request,
    conn: Database,
    dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    from app.api_key_service import touch_last_used
    from app.customer_fence import _verify_api_key_in_transaction, resolve_api_key_user

    principal = resolve_api_key_user(request)
    if principal is not None:
        _verify_api_key_in_transaction(cast(Any, conn.raw), principal)
        touch_last_used(cast(Any, conn.raw), key_id=principal.key_id)
        conn.api_key_id = principal.key_id
        conn.auth_source = "api_key"
        return authenticate_user(conn, principal.user_id)
    return authenticate_request(
        conn,
        authorization=authorization,
        dev_user_id=dev_user_id,
    )


def authenticate_request(
    conn: BusinessConnection,
    *,
    authorization: str | None,
    dev_user_id: str | None,
) -> CurrentUser:
    # CW-026: the converged PostgreSQL lane accepts exactly one identity —
    # the live customer session — in every environment (customer production,
    # staging, dev/test/CI). Internal Bearer tokens, X-Dev-User-Id and the
    # fixed desktop identity are unreachable here: a missing header answers
    # the customer-lane 401 instead of falling through to the internal-lane
    # resolution, and a present header is always resolved as a customer
    # session (fail-closed 503 when the session keys are misconfigured).
    # The legacy internal/desktop lane below is SQLite-only and exits with
    # CW-021/CW-040/CW-041.
    if conn.is_postgres:
        if authorization is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "SESSION_TOKEN_REQUIRED",
                    "message": "A Bearer session token is required on the customer lane.",
                },
            )
        return authenticate_customer_read_session(conn, parse_bearer_token(authorization))
    if internal_auth_required():
        if authorization is not None:
            return authenticate_access_token(conn, parse_bearer_token(authorization))
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_TOKEN_REQUIRED",
                "message": "A valid internal Bearer token is required.",
            },
        )
    return authenticate_user(conn, identity_user_id(dev_user_id))


def internal_access_token_user_id(conn: BusinessConnection, token: str) -> str | None:
    row = conn.execute(
        """
        SELECT user_id
        FROM internal_access_tokens
        WHERE token_digest = %s AND revoked_at IS NULL
        """,
        (digest_access_token(token),),
    ).fetchone()
    return None if row is None else str(row["user_id"])


def authenticate_customer_read_session(
    conn: BusinessConnection,
    token: str,
) -> CurrentUser:
    """Resolve a live customer session for a read-only business route.

    ``get_database`` already owns the PostgreSQL transaction used by the
    route.  Reusing the established verifier here keeps code/device status,
    lease and single-online-session semantics identical to fenced writes and
    avoids a second identity implementation.
    """
    from app.activation_code_service import ActivationKeyError
    from app.customer_auth import SessionFencingError, verify_session_context

    try:
        context = verify_session_context(
            cast(Any, conn.raw),
            presentation_session_token=token,
        )
    except ActivationKeyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SESSION_SERVICE_UNAVAILABLE",
                "message": "Session keys are not configured; customer sessions are refused.",
            },
        ) from exc
    except SessionFencingError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return authenticate_user(conn, context.user_id)


def parse_bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.strip().partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        raise invalid_token_error()
    return token


def digest_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate_access_token(conn: BusinessConnection, token: str) -> CurrentUser:
    user_id = internal_access_token_user_id(conn, token)
    if user_id is None:
        raise invalid_token_error()
    return authenticate_user(conn, user_id)


def invalid_token_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "AUTH_INVALID_TOKEN", "message": "Bearer token is invalid or revoked."},
    )


def internal_auth_required() -> bool:
    return os.environ.get(AUTH_MODE_ENV, "").lower() not in LEGACY_AUTH_MODES


def authenticate_user(conn: BusinessConnection, user_id: str | None) -> CurrentUser:
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_DESKTOP_IDENTITY_REQUIRED",
                "message": "VIDEO_REPLICA_DESKTOP_USER_ID is required for desktop API requests.",
            },
        )

    row = conn.execute(
        """
        SELECT id, username, display_name, role
        FROM users
        WHERE id = %s AND is_active = 1
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": "User is missing or inactive."},
        )

    role = str(row["role"])
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=403,
            detail={"code": "ROLE_INVALID", "message": "User role is not supported."},
        )

    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, role),
    )


AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]


def identity_user_id(dev_user_id: str | None) -> str | None:
    desktop_user_id = os.environ.get(DESKTOP_USER_ID_ENV)
    if desktop_user_id:
        return desktop_user_id
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1":
        return dev_user_id
    return None


def identity_source(dev_user_id: str | None, authorization: str | None = None) -> str:
    if internal_auth_required():
        if authorization is not None:
            return "bearer"
        return "none"
    if os.environ.get(DESKTOP_USER_ID_ENV):
        return "desktop"
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1" and dev_user_id:
        return "dev_header"
    return "none"
