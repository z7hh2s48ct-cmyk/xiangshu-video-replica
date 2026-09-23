"""Owner-scoped, encrypted accounts for web QR login; no credentials in DTOs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db_portable import BusinessConnection
from app.publish_avatars import rehost_avatar
from app.storage import StorageAdapter

Platform = Literal["douyin", "wechat_channels", "xiaohongshu"]
AccountSource = Literal["cloud", "desktop"]

MAX_STORAGE_STATE_BYTES = 2_000_000
_ACCOUNT_COLUMNS = (
    "id,platform,platform_user_id,username,verified_at,status,error_message,source,avatar_url"
)

# 扫码账号每 24 小时做一次登录态健康探测；探测租约 5 分钟，够一次平台探测往返。
PROBE_INTERVAL_SECONDS = 86_400
_PROBE_LEASE_SECONDS = 300
_PROBE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class BrowserLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform
    account_id: str | None = Field(default=None, max_length=64)


class BrowserIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_user_id: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=1, max_length=256)
    # The platform CDN link as seen by the official page; re-hosted before storage.
    avatar_url: str | None = Field(default=None, max_length=1024)


class BrowserAccountImportRequest(BaseModel):
    """Desktop WebView2 login exported once at connect time (cookies + localStorage)."""

    model_config = ConfigDict(extra="forbid")
    platform: Platform
    identity: BrowserIdentity
    storage_state: dict[str, Any]


class BrowserAccount(BaseModel):
    id: str
    platform: Platform
    platform_user_id: str
    username: str
    verified_at: int
    status: Literal["connected", "invalid"] = "connected"
    error_message: str | None = None
    source: AccountSource = "cloud"
    # Storage URI of the re-hosted avatar, never a platform CDN link.
    avatar_url: str | None = None


def account_response(row: Any) -> BrowserAccount:
    return BrowserAccount(
        id=row["id"],
        platform=row["platform"],
        platform_user_id=row["platform_user_id"],
        username=row["username"],
        verified_at=int(row["verified_at"].timestamp()),
        status=row["status"],
        error_message=row["error_message"],
        source=row["source"],
        avatar_url=row["avatar_url"],
    )


def list_browser_accounts(conn: BusinessConnection, owner: str) -> list[BrowserAccount]:
    return [
        account_response(row)
        for row in conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM publish_browser_accounts "  # noqa: S608
            "WHERE user_id=%s ORDER BY verified_at DESC,id",
            (owner,),
        ).fetchall()
    ]


def validate_storage_state(storage: Any) -> dict[str, Any]:
    """Accept only the Playwright storage_state shape; reject anything else."""
    if not isinstance(storage, dict) or set(storage) - {"cookies", "origins"}:
        raise HTTPException(422, "平台登录状态格式不正确。")
    cookies, origins = storage.get("cookies", []), storage.get("origins", [])
    if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
        raise HTTPException(422, "平台登录状态格式不正确。")
    if not isinstance(origins, list) or not all(isinstance(o, dict) for o in origins):
        raise HTTPException(422, "平台登录状态格式不正确。")
    if not cookies:
        raise HTTPException(422, "平台登录状态为空，请重新扫码。")
    return {"cookies": cookies, "origins": origins}


def upsert_browser_account(
    conn: BusinessConnection,
    owner: str,
    *,
    platform: str,
    identity: dict[str, str],
    storage: dict[str, Any],
    fernet: Fernet,
    source: AccountSource,
    account_id: str | None = None,
    avatar_url: str | None = None,
) -> BrowserAccount:
    """Insert or refresh one (owner, platform, platform_user_id) login state.

    A refresh resets ``status`` to connected: a fresh scan supersedes any
    earlier platform rejection. A refresh that could not re-host an avatar keeps
    the one already stored rather than blanking the account's picture.
    """
    uid, username = identity["platform_user_id"].strip(), identity["username"].strip()
    if not uid or len(uid) > 256 or not username or len(username) > 256:
        raise HTTPException(422, "平台账号信息不完整。")
    raw = json.dumps(storage, ensure_ascii=False).encode()
    if len(raw) > MAX_STORAGE_STATE_BYTES:
        raise HTTPException(422, "平台登录状态过大，请重新扫码。")
    row = conn.execute(
        "INSERT INTO publish_browser_accounts"
        "(id,user_id,platform,platform_user_id,username,storage_state_enc,source,avatar_url,"
        "next_probe_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs => %s)) "
        "ON CONFLICT(user_id,platform,platform_user_id) "
        "DO UPDATE SET username=EXCLUDED.username,"
        "storage_state_enc=EXCLUDED.storage_state_enc,source=EXCLUDED.source,"
        "avatar_url=COALESCE(EXCLUDED.avatar_url,publish_browser_accounts.avatar_url),"
        "status='connected',error_message=NULL,verified_at=clock_timestamp(),"
        "next_probe_at=clock_timestamp()+make_interval(secs => %s),"
        "probe_lease_owner=NULL,probe_lease_expires_at=NULL,probe_attempt_count=0 "
        f"RETURNING {_ACCOUNT_COLUMNS}",  # noqa: S608 - fixed column literal
        (
            account_id or str(uuid4()),
            owner,
            platform,
            uid,
            username,
            fernet.encrypt(raw).decode("ascii"),
            source,
            avatar_url,
            PROBE_INTERVAL_SECONDS,
            PROBE_INTERVAL_SECONDS,
        ),
    ).fetchone()
    return account_response(row)


def import_browser_account(
    conn: BusinessConnection,
    owner: str,
    request: BrowserAccountImportRequest,
    fernet: Fernet,
    media_storage: StorageAdapter | None = None,
) -> BrowserAccount:
    state = validate_storage_state(request.storage_state)
    # Copy the avatar before it is stored; a link to the platform CDN is never kept.
    avatar_url = (
        rehost_avatar(
            request.identity.avatar_url,
            storage=media_storage,
            owner=owner,
            platform=request.platform,
            platform_user_id=request.identity.platform_user_id.strip(),
        )
        if media_storage is not None
        else None
    )
    return upsert_browser_account(
        conn,
        owner,
        platform=request.platform,
        identity=request.identity.model_dump(),
        storage=state,
        fernet=fernet,
        source="desktop",
        avatar_url=avatar_url,
    )


def start_login(conn: BusinessConnection, owner: str, request: BrowserLoginRequest) -> str:
    # Serialize only the short slot-allocation transaction across API instances.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('publish-browser-login-capacity'))")
    conn.execute("DELETE FROM publish_browser_logins WHERE expires_at <= clock_timestamp()")
    if conn.execute("SELECT id FROM publish_browser_logins WHERE user_id=%s", (owner,)).fetchone():
        raise HTTPException(409, "请先完成或取消当前扫码。")
    if conn.execute("SELECT count(*) AS n FROM publish_browser_logins").fetchone()["n"] >= 4:
        raise HTTPException(429, "扫码服务繁忙，请稍后重试。")
    if (
        request.account_id
        and not conn.execute(
            "SELECT id FROM publish_browser_accounts WHERE id=%s AND user_id=%s AND platform=%s",
            (request.account_id, owner, request.platform),
        ).fetchone()
    ):
        raise HTTPException(404, "发布账号不存在。")
    login_id = str(uuid4())
    conn.execute(
        "INSERT INTO publish_browser_logins(id,user_id,platform,account_id,expires_at) "
        "VALUES (%s,%s,%s,%s,clock_timestamp()+interval '5 minutes')",
        (login_id, owner, request.platform, request.account_id),
    )
    return login_id


def existing_storage(
    conn: BusinessConnection,
    owner: str,
    account_id: str | None,
    fernet: Fernet,
) -> dict[str, Any] | None:
    if account_id is None:
        return None
    row = conn.execute(
        "SELECT storage_state_enc FROM publish_browser_accounts WHERE id=%s AND user_id=%s",
        (account_id, owner),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "发布账号不存在。")
    value: dict[str, Any] = json.loads(fernet.decrypt(row["storage_state_enc"].encode()))
    return value


def save_login(
    conn: BusinessConnection,
    owner: str,
    login_id: str,
    identity: dict[str, str],
    storage: dict[str, Any],
    fernet: Fernet,
) -> BrowserAccount:
    session = conn.execute(
        "SELECT * FROM publish_browser_logins WHERE id=%s AND user_id=%s "
        "AND expires_at>clock_timestamp() FOR UPDATE",
        (login_id, owner),
    ).fetchone()
    if session is None:
        raise HTTPException(409, "扫码已取消或过期，请重新获取二维码。")
    uid = identity["platform_user_id"].strip()
    if not uid or len(uid) > 256:
        raise HTTPException(422, "平台账号信息不完整。")
    previous = None
    if session["account_id"]:
        previous = conn.execute(
            "SELECT * FROM publish_browser_accounts WHERE id=%s AND user_id=%s FOR UPDATE",
            (session["account_id"], owner),
        ).fetchone()
        if previous is None or previous["platform_user_id"] != uid:
            raise HTTPException(409, "扫码账号与原账号不同，请取消后添加新账号。")
    account = upsert_browser_account(
        conn,
        owner,
        platform=session["platform"],
        identity=identity,
        storage=storage,
        fernet=fernet,
        source="cloud",
        account_id=previous["id"] if previous else None,
    )
    conn.execute("DELETE FROM publish_browser_logins WHERE id=%s AND user_id=%s", (login_id, owner))
    return account


def delete_browser_account(conn: BusinessConnection, owner: str, account_id: str) -> None:
    if (
        conn.execute(
            "DELETE FROM publish_browser_accounts WHERE id=%s AND user_id=%s",
            (account_id, owner),
        ).rowcount
        != 1
    ):
        raise HTTPException(404, "发布账号不存在。")


class BrowserProbeLeaseLostError(RuntimeError):
    """Raised when a fenced probe write-back no longer owns its lease."""


@dataclass
class BrowserProbeLease:
    record_id: str
    platform: str
    storage_state_enc: str
    lease_token: str
    attempt_count: int


def _probe_now() -> datetime:
    return datetime.now(UTC)


def claim_browser_probe_work(
    conn: BusinessConnection,
    *,
    worker_id: str,
    lease_seconds: int = _PROBE_LEASE_SECONDS,
    now: datetime | None = None,
) -> BrowserProbeLease | None:
    """Claim one connected browser account whose 24h health probe is due.

    Mirrors the legacy ``publish_accounts`` verify CAS: ``FOR UPDATE SKIP
    LOCKED`` picks a candidate, then a fenced ``UPDATE`` re-checks the same
    predicate so concurrent workers serialize instead of double-claiming. Only
    ``connected`` accounts are probed; an ``invalid`` account waits for a re-scan.
    """
    moment = now or _probe_now()
    now_text = moment.strftime(_PROBE_TIME_FORMAT)
    expires_text = (moment + timedelta(seconds=lease_seconds)).strftime(_PROBE_TIME_FORMAT)
    with conn:
        row = conn.execute(
            """
            SELECT id, platform, storage_state_enc
            FROM publish_browser_accounts
            WHERE status = 'connected'
              AND (next_probe_at IS NULL OR next_probe_at <= %s)
              AND (probe_lease_expires_at IS NULL OR probe_lease_expires_at <= %s)
            ORDER BY next_probe_at NULLS FIRST, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            return None
        lease_token = uuid4().hex
        claimed = conn.execute(
            """
            UPDATE publish_browser_accounts
            SET probe_lease_owner = %s, probe_lease_expires_at = %s,
                probe_attempt_count = probe_attempt_count + 1
            WHERE id = %s AND status = 'connected'
              AND (next_probe_at IS NULL OR next_probe_at <= %s)
              AND (probe_lease_expires_at IS NULL OR probe_lease_expires_at <= %s)
            RETURNING probe_attempt_count
            """,
            (lease_token, expires_text, str(row["id"]), now_text, now_text),
        ).fetchone()
        if claimed is None:
            return None
        return BrowserProbeLease(
            record_id=str(row["id"]),
            platform=str(row["platform"]),
            storage_state_enc=str(row["storage_state_enc"]),
            lease_token=lease_token,
            attempt_count=int(claimed["probe_attempt_count"]),
        )


def finalize_browser_probe(
    conn: BusinessConnection,
    *,
    lease: BrowserProbeLease,
    ok: bool,
    message: str | None = None,
    interval_seconds: int = PROBE_INTERVAL_SECONDS,
    now: datetime | None = None,
) -> None:
    """Fenced write-back: schedule the next probe, flip status on failure.

    On success the account stays ``connected`` and its next probe is pushed
    ``interval_seconds`` out; on failure it becomes ``invalid`` (surfacing a
    re-scan prompt in the UI) but is still rescheduled so a later recovery is
    possible only via a fresh scan, not another probe of a dead cookie.
    """
    moment = now or _probe_now()
    next_text = (moment + timedelta(seconds=interval_seconds)).strftime(_PROBE_TIME_FORMAT)
    with conn:
        cursor = conn.execute(
            """
            UPDATE publish_browser_accounts
            SET status = %s,
                error_message = %s,
                next_probe_at = %s,
                probe_lease_owner = NULL,
                probe_lease_expires_at = NULL
            WHERE id = %s AND probe_lease_owner = %s AND probe_attempt_count = %s
            """,
            (
                "connected" if ok else "invalid",
                None if ok else (message or "登录态已失效，请重新扫码"),
                next_text,
                lease.record_id,
                lease.lease_token,
                lease.attempt_count,
            ),
        )
        if cursor.rowcount != 1:
            raise BrowserProbeLeaseLostError("browser account probe lease was lost")
