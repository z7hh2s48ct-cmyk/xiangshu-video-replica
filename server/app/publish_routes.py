"""C5 publish routes (phase 1): platform account authorization for the studio.

Read routes ride the plain authenticated lane; every write goes through the
T21 fenced ``BusinessDbDep.write()`` exactly like the other studio routes.

Only the four legacy cookie-paste ``/accounts`` endpoints live here: list,
connect, unbind and request an async login-state probe. The delivery path
(``/api/studio/publish/records``) is ``app.publish_record_routes`` and reads
the browser-login accounts (``/api/studio/publish/browser/accounts``).

No endpoint ever returns credential material — the response models in
app.publish carry display_name / status / timestamps only.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi import APIRouter

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.publish import (
    PublishAccountCreateRequest,
    PublishAccountDeleteResponse,
    PublishAccountListResponse,
    PublishAccountResponse,
    PublishVerifyResponse,
    create_account,
    delete_account,
    list_accounts,
    request_account_verify,
)
from app.settings import fernet_from_environment

router = APIRouter(prefix="/api/studio/publish")


def _fernet() -> Fernet:
    return fernet_from_environment()


@router.get("/accounts", response_model=PublishAccountListResponse)
def read_publish_accounts(
    conn: Database,
    actor: AuthenticatedUser,
) -> PublishAccountListResponse:
    return list_accounts(conn, actor_id=actor.id)


@router.post("/accounts", response_model=PublishAccountResponse)
def create_publish_account(
    request: PublishAccountCreateRequest,
    db: BusinessDbDep,
) -> PublishAccountResponse:
    with db.write() as (conn, actor):
        return create_account(conn, actor=actor, fernet=_fernet(), request=request)


@router.delete("/accounts/{account_id}", response_model=PublishAccountDeleteResponse)
def delete_publish_account(
    account_id: str,
    db: BusinessDbDep,
) -> PublishAccountDeleteResponse:
    with db.write() as (conn, actor):
        delete_account(conn, actor_id=actor.id, account_id=account_id)
    return PublishAccountDeleteResponse(deleted=True)


@router.post("/accounts/{account_id}/verify", response_model=PublishVerifyResponse)
def verify_publish_account(
    account_id: str,
    db: BusinessDbDep,
) -> PublishVerifyResponse:
    with db.write() as (conn, actor):
        request_account_verify(conn, actor_id=actor.id, account_id=account_id)
    return PublishVerifyResponse(submitted=True)
