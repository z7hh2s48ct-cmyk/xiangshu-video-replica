from __future__ import annotations

import hashlib
import hmac
import os
import string
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request

from app.auth import CurrentUser, Database, Role, authenticate_user

CONTROL_PROXY_TOKEN_DIGEST_ENV = "CONTROL_PROXY_TOKEN_DIGEST"
CONTROL_ADMIN_USER_ID_ENV = "CONTROL_ADMIN_USER_ID"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_TRUTHY = {"1", "true", "yes", "on"}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def get_control_user(
    conn: Database,
    proxy_token: Annotated[str | None, Header(alias="X-Control-Proxy-Token")] = None,
) -> CurrentUser:
    # T09 / DB-08: the single-admin proxy-token identity is the internal P0
    # compatibility path only. Customer production must authenticate every
    # operator through per-operator admin sessions (admin_auth_routes); a
    # misconfigured boot that slips past the startup gate is still rejected
    # here before any token comparison or database lookup.
    if _is_customer_production():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "LEGACY_CONTROL_IDENTITY_FORBIDDEN",
                "message": (
                    "The legacy single-admin control identity is not available in "
                    "customer production; use a per-operator admin session."
                ),
            },
        )
    expected_digest = os.environ.get(CONTROL_PROXY_TOKEN_DIGEST_ENV, "").strip().lower()
    admin_user_id = os.environ.get(CONTROL_ADMIN_USER_ID_ENV, "").strip()
    if not _valid_sha256_digest(expected_digest) or not admin_user_id:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CONTROL_AUTH_NOT_CONFIGURED",
                "message": "Control proxy authentication is not configured.",
            },
        )
    if proxy_token is None:
        raise control_auth_error()

    supplied_digest = hashlib.sha256(proxy_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise control_auth_error()

    actor = authenticate_user(conn, admin_user_id)
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONTROL_ADMIN_REQUIRED",
                "message": "The configured control identity must be an active admin.",
            },
        )
    return actor


def get_control_route_user(
    request: Request,
    conn: Database,
    proxy_token: Annotated[str | None, Header(alias="X-Control-Proxy-Token")] = None,
) -> CurrentUser:
    """Resolve the control-plane actor for the route's deployment lane.

    Internal P0 deployments retain their proxy-token boundary unchanged.  In
    customer production the very same operational routes must instead use the
    per-operator ``admin_session`` cookie established by the ASX1 exchange.
    That preserves CSRF and auditor read-only enforcement for every legacy
    account and billing endpoint without ever reviving the retired shared
    control identity.  Bulk exports do not ride this read-level adapter: they
    take :func:`get_control_writer` instead (see its docstring).
    """
    if not _is_customer_production():
        return get_control_user(conn, proxy_token)

    # Local import keeps the two compatibility modules acyclic: the session
    # module documents the legacy identity, while this adapter only chooses it
    # outside customer production.
    from app.admin_auth_routes import get_admin_actor, get_admin_writer

    admin_actor = get_admin_actor(request)
    if request.method.upper() in _WRITE_METHODS:
        get_admin_writer(admin_actor)
    return CurrentUser(
        id=admin_actor.user_id,
        username=admin_actor.username,
        display_name=admin_actor.display_name,
        role=cast(Role, admin_actor.role),
    )


def get_control_writer(
    request: Request,
    conn: Database,
    proxy_token: Annotated[str | None, Header(alias="X-Control-Proxy-Token")] = None,
) -> CurrentUser:
    """Resolve the control-plane actor for one bulk export — write-level role.

    A CSV export is a **data-egress action, not a view**: one request pulls the
    entire (filtered) ledger off the platform in bulk, and the auditor's
    read-only views of those very same rows are untouched by gating it here —
    what the auditor loses is only the one-shot full dump.  The policy is
    therefore uniform with ``customers.csv`` (``AdminWriter``): the role that
    may mutate control data is the role that may extract it in bulk.  The
    reverse — leaving exports on the read path — would newly hand auditors a
    bulk form of customer PII/ledger data, so it is the strictly more exposing
    choice.

    Note the gate is the *route's* intent, not its HTTP verb: these endpoints
    are GET, so the read-level :func:`get_control_route_user` would let an
    auditor through on the customer-production lane purely because of the
    method (the internal lane already requires an active ``admin`` actor, see
    :func:`get_control_user`).
    """
    if not _is_customer_production():
        # Internal P0 keeps the proxy-token identity; get_control_user already
        # refuses any actor whose role is not ``admin``.
        return get_control_user(conn, proxy_token)

    from app.admin_auth_routes import get_admin_actor, get_admin_writer

    admin_actor = get_admin_actor(request)
    get_admin_writer(admin_actor)
    return CurrentUser(
        id=admin_actor.user_id,
        username=admin_actor.username,
        display_name=admin_actor.display_name,
        role=cast(Role, admin_actor.role),
    )


def control_auth_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "CONTROL_AUTH_INVALID",
            "message": "A valid control proxy token is required.",
        },
    )


def _valid_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in string.hexdigits for character in value)


ControlUser = Annotated[CurrentUser, Depends(get_control_route_user)]
# Bulk exports (CSV dumps) — write-level authority on the read path.
ControlWriter = Annotated[CurrentUser, Depends(get_control_writer)]
