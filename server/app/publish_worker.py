"""Standalone publish worker: account login-state probes and video deliveries.

Calling a platform means outbound I/O — douyin additionally needs a Node.js
signing subprocess — and delivering one video blocks for minutes (download
from storage, upload, platform transcode wait, create). That work runs in its
own process instead of the generation worker's serial loop so a slow or hung
platform call never stalls H3 polling or oral tasks.

Each round claims at most one legacy account probe (phase 1, ``app.publish``),
at most one QR-login account health probe (phase 1b, ``app.publish_browser``)
and at most one publish record (phase 2, ``app.publish_records``). Claim and
finalize each get their own short transaction; the platform I/O happens inside
the adapters with no database transaction open. The heavy vendor/adapter imports
are function-local so this module can also be imported for tests without
Node/curl_cffi present.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from threading import Event
from types import FrameType
from typing import Any, Literal

from app.db_pg import (
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    resolve_database_config,
    validate_customer_production,
)
from app.db_portable import BusinessConnection
from app.publish import (
    PublishLeaseLostError,
    claim_account_verify_work,
    finalize_account_verify,
)
from app.publish_browser import (
    BrowserProbeLease,
    BrowserProbeLeaseLostError,
    claim_browser_probe_work,
    finalize_browser_probe,
)
from app.publish_records import PublishWork, claim_publish_work, finalize_publish_work
from app.publishers.base import PublishResult
from app.settings import fernet_from_environment
from app.worker_identity import new_worker_instance_id

logger = logging.getLogger(__name__)


def _dispatch_probe(
    platform: str, cookie: str, security_sdk: str | dict[str, Any] | None
) -> tuple[bool, str | None]:
    # security_sdk 只抖音用得上：账号巡检传入解密后的 JSON 字符串，
    # 浏览器登录态探测传入 credentials_for_platform 解出的 dict；两者
    # douyin_adapter.probe_douyin(SecuritySdk = str | dict | None) 都接受。
    if platform == "douyin":
        from app.publishers import douyin_adapter

        return douyin_adapter.probe_douyin(cookie, security_sdk)
    from app.publishers import channels_adapter

    return channels_adapter.probe_channels(cookie)


def _perform_publish(
    open_txn: Callable[[], AbstractContextManager[BusinessConnection]],
    *,
    work: PublishWork,
    key: Any,
) -> tuple[PublishResult, Literal["api", "browser"] | None]:
    """Decrypt, materialize media and deliver; never raises, never logs secrets."""
    import json

    from app import publish_delivery
    from app.media_routes import storage_for_asset

    row = work.row
    platform = str(row["platform"])
    try:
        storage_state = json.loads(key.decrypt(str(row["storage_state_enc"]).encode("ascii")))
        if not isinstance(storage_state, dict):
            raise ValueError("storage state is not an object")
    except Exception as exc:  # noqa: BLE001 - credential boundary
        logger.warning("publish credential decode failed: %s", type(exc).__name__)
        return (
            PublishResult(
                platform=platform,
                status="failed",
                message="登录态材料无法读取，请重新扫码连接账号",
                account_invalid=True,
            ),
            None,
        )
    try:
        with open_txn() as conn:
            storage = storage_for_asset(conn, str(row["video_uri"]))
    except Exception as exc:  # noqa: BLE001 - storage configuration boundary
        logger.warning("publish storage resolution failed: %s", type(exc).__name__)
        return (
            PublishResult(
                platform=platform, status="failed", message="存储服务暂不可用，稍后自动重试"
            ),
            None,
        )
    tags_value = row.get("tags")
    tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
    options_value = row.get("options")
    options = dict(options_value) if isinstance(options_value, dict) else {}
    try:
        with publish_delivery.materialized_media(
            storage,
            video_uri=str(row["video_uri"]),
            video_content_type=row.get("video_content_type"),
            cover_uri=row.get("cover_uri"),
            cover_content_type=row.get("cover_content_type"),
        ) as (video_path, cover_path):
            outcome = publish_delivery.deliver(
                platform,
                storage_state,
                video_path=video_path,
                cover_path=cover_path,
                title=str(row["title"] or ""),
                description=str(row["description"] or ""),
                tags=tags,
                options=options,
            )
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        logger.warning("publish delivery preparation failed: %s", type(exc).__name__)
        return (
            PublishResult(platform=platform, status="failed", message="发布准备失败，稍后自动重试"),
            None,
        )
    return outcome.result, outcome.mode


def _perform_browser_probe(lease: BrowserProbeLease, key: Any) -> tuple[bool, str | None]:
    """Decrypt a QR-login account's state and probe its cookie; never raises."""
    import json

    from app.publish_credentials import credentials_for_platform

    try:
        storage = json.loads(key.decrypt(lease.storage_state_enc.encode("ascii")))
        if not isinstance(storage, dict):
            raise ValueError("storage state is not an object")
    except Exception as exc:  # noqa: BLE001 - credential boundary
        logger.warning("browser probe credential decode failed: %s", type(exc).__name__)
        return False, "登录态材料无法读取，请重新扫码连接账号"
    credentials = credentials_for_platform(lease.platform, storage)
    if not credentials.cookie:
        return False, "登录态材料无法读取，请重新扫码连接账号"
    return _dispatch_probe(lease.platform, credentials.cookie, credentials.security_sdk)


def run_publish_round(
    open_txn: Callable[[], AbstractContextManager[BusinessConnection]],
    *,
    worker_id: str,
    fernet: object = None,
) -> int:
    """Claim and process at most one account probe and one publish record.

    Claim and finalize each get their own short transaction via ``open_txn``
    (one ``pg_transaction`` block each) — outbound platform I/O must never
    run inside an open transaction, or the claimed lease would not commit and
    concurrent workers could take the same row instead of serializing on
    ``FOR UPDATE SKIP LOCKED``.
    """
    from cryptography.fernet import Fernet

    key = fernet if isinstance(fernet, Fernet) else fernet_from_environment()
    processed = 0

    with open_txn() as conn:
        lease = claim_account_verify_work(conn, worker_id=worker_id)
    if lease is not None:
        processed += 1
        row = lease.row
        try:
            cookie = key.decrypt(str(row["cookie_enc"]).encode("ascii")).decode("utf-8")
            sdk_enc = row.get("security_sdk_enc")
            security_sdk = (
                key.decrypt(str(sdk_enc).encode("ascii")).decode("utf-8") if sdk_enc else None
            )
            ok, message = _dispatch_probe(str(row["platform"]), cookie, security_sdk)
        except PublishLeaseLostError:
            return processed
        except Exception as exc:  # noqa: BLE001 - probe boundary
            logger.warning("account verify preparation failed: %s", type(exc).__name__)
            ok, message = False, "登录态材料无法读取，请重新连接账号"
        try:
            with open_txn() as conn:
                finalize_account_verify(conn, lease=lease, ok=ok, message=message)
        except PublishLeaseLostError:
            logger.warning("account verify finalize lost lease: %s", lease.record_id)

    with open_txn() as conn:
        probe_lease = claim_browser_probe_work(conn, worker_id=worker_id)
    if probe_lease is not None:
        processed += 1
        ok, message = _perform_browser_probe(probe_lease, key)
        try:
            with open_txn() as conn:
                finalize_browser_probe(conn, lease=probe_lease, ok=ok, message=message)
        except BrowserProbeLeaseLostError:
            logger.warning("browser probe finalize lost lease: %s", probe_lease.record_id)

    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id=worker_id)
    if work is not None:
        processed += 1
        result, mode = _perform_publish(open_txn, work=work, key=key)
        try:
            with open_txn() as conn:
                outcome = finalize_publish_work(conn, work=work, result=result, delivery_mode=mode)
            logger.info("publish record %s -> %s", work.record_id, outcome)
        except PublishLeaseLostError:
            logger.warning("publish finalize lost lease: %s", work.record_id)
    return processed


# CW-030 移除了 Worker 的 SQLite 业务入口，CW-060 的反向消费者注册表把历史
# app.db 导入面冻结成只减不增的清单（该守卫按源码子串扫描，注释里出现同样的
# 字面量也会被计入）。本模块是 CW-068 新增的，从第一天起就只有 PG lane ——
# 因此这里刻意不存在 SQLite 事务入口。


def _pg_round(*, worker_id: str) -> int:  # pragma: no cover - process loop
    from app.db_pg import pg_transaction

    @contextmanager
    def open_txn() -> Iterator[BusinessConnection]:
        with pg_transaction() as raw_conn:
            yield BusinessConnection.postgres(raw_conn)

    return run_publish_round(open_txn, worker_id=worker_id)


def run_pg_forever(
    *,
    worker_id: str,
    idle_seconds: float,
    stop_event: Event | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> None:  # pragma: no cover - process loop
    stop = stop_event if stop_event is not None else Event()

    def stopping() -> bool:
        return stop.is_set() or (stop_requested is not None and stop_requested())

    if check_pg_ready() is None:
        raise RuntimeError("PostgreSQL publish worker readiness check returned no result")
    while not stopping():
        try:
            processed = _pg_round(worker_id=worker_id)
        except Exception:
            logger.exception("publish worker iteration failed")
            processed = 0
        if processed == 0:
            deadline = time.monotonic() + max(0.0, idle_seconds)
            while not stopping():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Signals only set a lock-free flag. Bound the wait so even a
                # long idle interval observes that flag within 200 milliseconds.
                stop.wait(min(remaining, 0.2))


@contextmanager
def _shutdown_signals() -> Iterator[Callable[[], bool]]:
    """Finish an in-flight probe, then exit without beginning another round."""
    previous = {}
    requested = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        # Event.set() can deadlock if a signal interrupts Event.wait() while
        # its non-reentrant condition lock is held on this same main thread.
        nonlocal requested
        requested = True

    def is_requested() -> bool:
        return requested

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        yield is_requested
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> None:  # pragma: no cover - CLI entry
    parser = argparse.ArgumentParser(description="Run the platform publish worker")
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--worker-id", help="logical worker label; each startup adds a unique suffix"
    )
    args = parser.parse_args()

    worker_id = new_worker_instance_id("publish-worker", args.worker_id)
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s worker_id={worker_id} %(message)s",
    )
    logger.info("publish worker starting instance=%s", worker_id)

    config = resolve_database_config()
    validate_customer_production(config)
    if config.mode is DatabaseMode.POSTGRESQL:
        try:
            with _shutdown_signals() as stop_requested:
                run_pg_forever(
                    worker_id=worker_id,
                    idle_seconds=args.idle_seconds,
                    stop_requested=stop_requested,
                )
        finally:
            close_pg_pool()
            logger.info("publish worker stopped instance=%s", worker_id)
        return

    # CW-025: resolve_database_config() 全环境 fail-closed 后，SQLite 分支 unreachable。
    # 本 RuntimeError 是防御性断言：如果走到这里，说明 resolve_database_config() 有 bug。
    raise RuntimeError(
        "publish_worker: SQLite online path is unreachable after CW-025; "
        "this indicates a bug in resolve_database_config(). "
        "Worker SQLite business logic removal is tracked by CW-030."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
