"""Authenticated streaming QR login. The HTTP stream owns browser lifetime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep, BusinessReadConn
from app.db_pg import pg_transaction
from app.media_routes import get_media_storage
from app.permissions import require_not_auditor
from app.publish_browser import (
    BrowserAccount,
    BrowserAccountImportRequest,
    BrowserLoginRequest,
    delete_browser_account,
    existing_storage,
    import_browser_account,
    list_browser_accounts,
    save_login,
    start_login,
)
from app.publish_browser_engine import login_events
from app.settings import fernet_from_environment
from app.storage import StorageAdapter
from app.sub_account_permissions import enforce_sub_account_publish_accounts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studio/publish/browser")


def optional_media_storage(conn: BusinessReadConn) -> StorageAdapter | None:
    """Storage for avatar re-hosting only.

    Connecting an account must not depend on object storage being configured, so
    an unavailable backend degrades to an account without a picture instead of
    failing the import.
    """
    try:
        return get_media_storage(conn)
    except HTTPException as exc:
        logger.warning("publish avatar storage unavailable: detail=%s", exc.detail)
        return None


AvatarStorage = Annotated[StorageAdapter | None, Depends(optional_media_storage)]


def release_login(owner: str, login_id: str) -> None:
    with pg_transaction() as conn:
        conn.execute(
            "DELETE FROM publish_browser_logins WHERE id=%s AND user_id=%s", (login_id, owner)
        )


@router.get("/accounts", response_model=list[BrowserAccount])
def accounts(conn: Database, actor: AuthenticatedUser, response: Response) -> list[BrowserAccount]:
    response.headers["Cache-Control"] = "no-store"
    return list_browser_accounts(conn, actor.id)


@router.post("/accounts/import", response_model=BrowserAccount)
def import_account(
    request: BrowserAccountImportRequest,
    db: BusinessDbDep,
    storage: AvatarStorage,
) -> BrowserAccount:
    """Persist a desktop WebView2 login exported once at connect time.

    The desktop client keeps its own WebView2 profile for manual publishing;
    this copy lets the server-side worker deliver on the account's behalf. The
    account's avatar is copied into our own storage here so the studio never
    hotlinks a platform CDN.
    """
    fernet = fernet_from_environment()
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="publish.browser.import",
            entity_type="publish_account",
            entity_id="import",
        )
        # Account-binding admission (Phase 3b): restricted sub-accounts
        # without the publish switch cannot bind imported accounts.
        enforce_sub_account_publish_accounts(conn, actor_id=actor.id)
        return import_browser_account(conn, actor.id, request, fernet, storage)


@router.delete("/accounts/{account_id}")
def remove_account(account_id: str, db: BusinessDbDep) -> dict[str, bool]:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="publish.browser.delete",
            entity_type="publish_account",
            entity_id=account_id,
        )
        delete_browser_account(conn, actor.id, account_id)
    return {"deleted": True}


@router.delete("/logins/{login_id}")
def cancel_login(login_id: str, db: BusinessDbDep) -> dict[str, bool]:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="publish.browser.cancel",
            entity_type="publish_login",
            entity_id=login_id,
        )
        conn.execute(
            "DELETE FROM publish_browser_logins WHERE id=%s AND user_id=%s", (login_id, actor.id)
        )
    return {"cancelled": True}


@router.post("/logins", response_class=StreamingResponse)
def login(request: BrowserLoginRequest, db: BusinessDbDep) -> StreamingResponse:
    fernet = fernet_from_environment()
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="publish.browser.login",
            entity_type="publish_login",
            entity_id="new",
        )
        # Account-binding admission (Phase 3b): same gate as import; covers
        # first-time binds and re-logins of existing accounts alike.
        enforce_sub_account_publish_accounts(conn, actor_id=actor.id)
        storage = existing_storage(conn, actor.id, request.account_id, fernet)
        login_id = start_login(conn, actor.id, request)
        owner = actor.id

    def encode(phase: str, **fields: Any) -> str:
        return (
            json.dumps(
                {"phase": phase, "image": None, "account": None, **fields}, ensure_ascii=False
            )
            + "\n"
        )

    def persist(identity: dict[str, str], state: dict[str, Any]) -> BrowserAccount:
        # Reverify customer session in the SAME transaction as credential persistence.
        with db.write() as (conn, current_actor):
            if current_actor.id != owner:
                raise HTTPException(401, "用户会话已变更。")
            return save_login(conn, owner, login_id, identity, state, fernet)

    def still_active() -> bool:
        with pg_transaction() as conn:
            return (
                conn.execute(
                    "SELECT id FROM publish_browser_logins WHERE id=%s AND user_id=%s "
                    "AND expires_at>clock_timestamp()",
                    (login_id, owner),
                ).fetchone()
                is not None
            )

    async def stream() -> AsyncIterator[str]:
        try:
            yield encode("loading", login_id=login_id)
            async with asyncio.timeout(300):
                async with aclosing(login_events(request.platform, storage)) as events:
                    async for event in events:
                        if not await asyncio.to_thread(still_active):
                            yield encode("closed")
                            return
                        if (
                            event.phase == "connected"
                            and event.identity is not None
                            and event.storage is not None
                        ):
                            account = await asyncio.to_thread(
                                persist, event.identity, event.storage
                            )
                            yield encode("connected", account=account.model_dump())
                            return
                        yield encode(event.phase, image=event.image)
        except TimeoutError:
            yield encode("expired")
        except HTTPException as exc:
            message = (
                exc.detail
                if isinstance(exc.detail, str)
                else "登录确认失败，请重新登录工作台后重试。"
            )
            yield encode("closed", message=message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Browser exceptions may contain URLs/session material; never stream/log them.
            yield encode(
                "closed",
                message="扫码浏览器启动或平台连接失败，请稍后重试；若持续失败，请联系管理员检查浏览器运行环境。",
            )
        finally:
            await asyncio.shield(asyncio.to_thread(release_login, owner, login_id))

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(release_login, owner, login_id),
    )
