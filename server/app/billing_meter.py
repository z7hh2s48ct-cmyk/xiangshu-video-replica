"""Cost context for auxiliary provider calls outside database transactions.

Extends with API metadata tracking for TikTok Hub calls:
- douyin_search
- wechat_search_page
- wechat_video_detail
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.usage_billing import (
    accept_platform_operation,
    begin_attempt,
    begin_source_attempt,
    complete_attempt,
    finish_operation,
)

_source: ContextVar[str | None] = ContextVar("billing_source", default=None)
_collection: ContextVar[str | None] = ContextVar("billing_collection", default=None)
_video: ContextVar[tuple[str, str] | None] = ContextVar("billing_video", default=None)
_api_type: ContextVar[str | None] = ContextVar("billing_api_type", default=None)


@dataclass
class MeteredCall:
    """分开记录供应商用量与调用方可见结果。"""

    units: float | int
    usage: float | int | None = None

    def record_usage(self) -> None:
        """供应商已返回响应，因此该次用量可确定。"""
        self.usage = self.units


@contextmanager
def video_billing_context(platform: str, video_id: str) -> Iterator[None]:
    """Attribute only requests made for this exact video; never split shared searches."""
    token = _video.set((platform, video_id))
    try:
        yield
    finally:
        _video.reset(token)


@contextmanager
def collection_billing_context(batch_id: str) -> Iterator[None]:
    """Each physical request owns its cost; a batch only groups shared customer charges."""
    token = _collection.set(batch_id)
    try:
        yield
    finally:
        _collection.reset(token)


@contextmanager
def set_api_type(api_type: str) -> Iterator[None]:
    """Temporarily set the API type for the current execution context.

    Used to tag billing operations with the specific TikTok Hub API being called.
    Example:
        with set_api_type("douyin_search"):
            videos = client.douyin_search(keyword="别墅")
    """
    token = _api_type.set(api_type)
    try:
        yield
    finally:
        _api_type.reset(token)


@contextmanager
def billing_context(source_id: str) -> Iterator[None]:
    token = _source.set(source_id)
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def meter_call(service: str, *, units: float | int = 1) -> Iterator[MeteredCall]:
    source = None if service == "viral_data" and _collection.get() else _source.get()
    attempt = None
    platform_operation = None
    api_type = _api_type.get()
    api_metadata = {"api_type": api_type} if api_type else {}
    if service == "viral_data" and _video.get():
        platform, video_id = _video.get() or ("", "")
        api_metadata.update(video_platform=platform, video_id=video_id)

    if source:
        with pg_transaction() as raw:
            attempt = begin_source_attempt(
                BusinessConnection.postgres(raw),
                source,
                service=service,
                api_metadata=api_metadata if api_metadata else None,
            )
    elif service == "viral_data" and os.environ.get("VIDEO_REPLICA_DATABASE_URL"):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            platform_operation = accept_platform_operation(
                conn,
                service=service,
                source_id=str(uuid4()),
                collection_batch_id=_collection.get(),
                api_metadata=api_metadata if api_metadata else None,
            )
            attempt = begin_attempt(conn, operation_id=platform_operation, attempt_key="request")
    call = MeteredCall(units=units)
    succeeded = False
    try:
        yield call
        succeeded = True
        if call.usage is None:
            call.record_usage()
    finally:
        if attempt:
            with pg_transaction() as raw:
                conn = BusinessConnection.postgres(raw)
                if platform_operation:
                    operation = conn.execute(
                        "SELECT state FROM billing_operations WHERE id=%s FOR UPDATE",
                        (platform_operation,),
                    ).fetchone()
                    if operation[0] != "PENDING":
                        raise RuntimeError("采集接口计量已超时，迟到结果保留待核对")
                complete_attempt(conn, attempt_id=attempt, usage=call.usage)
                if platform_operation:
                    finish_operation(
                        conn,
                        operation_id=platform_operation,
                        units=units if succeeded else 0,
                        succeeded=succeeded,
                    )
