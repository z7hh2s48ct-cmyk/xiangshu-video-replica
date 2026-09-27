"""Oral digital-human domain services (C1 / 未接通能力拆解).

Vendor-neutral by contract: no table, row, or customer-visible message may
name the upstream provider (see the red-line test in tests/test_hifly_client.py).
Polling is pull-based (no public webhook), but customer GET endpoints are pure
reads; explicit POST refresh endpoints perform vendor reconciliation inside a
fenced business-write transaction.

Wallet RESERVE/SETTLE intentionally waits for a dedicated slice: the internal
billing reconciler (BILL-03) is generation-task scoped, so oral reservations
need a task-type discriminator before they can survive it. Until then the
task carries a price snapshot only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, get_args
from uuid import uuid4

from fastapi import HTTPException

from app import content_store
from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.hifly import HiflyClient, HiflyError, HiflySubmissionUncertain, validate_oral_subtitle
from app.internal_billing import finalize_oral_billing, reserve_oral_billing
from app.media_routes import get_media_storage, storage_for_asset
from app.media_tools import inspect_media_bytes
from app.permissions import require_asset_access, require_not_auditor, write_audit
from app.sql_pagination import PAGE_CLAUSE, page_bounds
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

ORAL_UNIT_PRICE_FEN_DEFAULT = 1000
MAX_ORAL_SCRIPT_CHARS = 10_000
ORAL_SOURCE_MAX_BYTES = {
    "audio": 50 * 1024 * 1024,
    "image": 10 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
}
ORAL_CONSENT_TEXT_VERSION = "2026-09-06-v1"
ORAL_CONSENT_PURPOSES = {"AVATAR_CLONE", "VOICE_CLONE"}

# 声音克隆样本语言：只开放普通话与国内方言（上游另支持的外语不开放），键与上游
# 字典一致，并与迁移 20260927T0000 的 CHECK 约束同步维护。
VoiceLanguage = Literal[
    "zh",
    "zh_cantonese",
    "zh_sichuanese",
    "zh_shanghainese",
    "zh_tianjinese",
    "zh_zhengzhounese",
    "zh_wuhanese",
]
VOICE_LANGUAGES: frozenset[str] = frozenset(get_args(VoiceLanguage))
DEFAULT_VOICE_LANGUAGE = "zh"

AvatarStatus = str  # PENDING/RUNNING/READY/FAILED
TaskStatus = str  # QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED


class OralDomainError(Exception):
    """Customer-safe oral-domain failure (message is UI-renderable)."""


class OralConflictError(OralDomainError):
    """An idempotency key was reused for a different request."""


class OralTaskNotFoundError(OralDomainError):
    """The requested task is absent from the current actor's scope."""


class OralResourceNotFoundError(OralDomainError):
    """The addressed clone is absent from the current actor's scope (404)."""


class OralResourceInUseError(OralDomainError):
    """The clone cannot be deleted yet: still cloning or has live tasks (409)."""


def oral_unit_price_fen(conn: BusinessConnection) -> int:
    """Retail equivalent per second. Missing tariffs are free, query errors propagate."""
    from decimal import ROUND_CEILING, Decimal

    from app.billing_catalog import retail_snapshot

    quote = retail_snapshot(conn, "oral", 1)
    ratio = quote["points_per_yuan"]
    return (
        int(
            (Decimal(str(quote["credits"])) * 100 / Decimal(str(ratio))).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if ratio
        else 0
    )


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _identity(conn: BusinessConnection, identity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, owner_user_id, display_name FROM person_identities WHERE id = %s",
        (identity_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _asset(conn: BusinessConnection, asset_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes,
               content_type, metadata_json, created_by_user_id
        FROM assets
        WHERE id = %s
        """,
        (asset_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_own_identity(
    conn: BusinessConnection, actor: CurrentUser, identity_id: str
) -> dict[str, Any]:
    identity = _identity(conn, identity_id)
    if identity is None or identity["owner_user_id"] != actor.id:
        raise OralDomainError("人物不存在或无权使用")
    return identity


def _require_source_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
    media_type: str,
    label: str,
) -> dict[str, Any]:
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="oral.source_asset.use",
    )
    asset = dict(row)
    size_bytes = int(asset["size_bytes"])
    if size_bytes <= 0 or not str(asset["sha256"] or "").strip():
        raise OralDomainError(f"{label}未完成或已失效，请重新上传")
    if not str(asset["content_type"] or "").lower().startswith(f"{media_type}/"):
        raise OralDomainError(f"{label}类型不匹配")
    if size_bytes > ORAL_SOURCE_MAX_BYTES[media_type]:
        raise OralDomainError(f"{label}超过大小限制")
    return asset


def _require_biometric_source_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
    media_type: str,
    label: str,
) -> dict[str, Any]:
    asset = _require_source_asset(
        conn,
        actor=actor,
        asset_id=asset_id,
        media_type=media_type,
        label=label,
    )
    if str(asset["created_by_user_id"] or "") != actor.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "ASSET_NOT_FOUND", "message": "Asset does not exist."},
        )
    return asset


def _require_audio_purpose(asset: dict[str, Any], expected: str, label: str) -> None:
    try:
        metadata = json.loads(str(asset.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    purpose = metadata.get("audio_purpose") if isinstance(metadata, dict) else None
    duration = metadata.get("duration_seconds") if isinstance(metadata, dict) else None
    verified = metadata.get("audio_duration_verified") if isinstance(metadata, dict) else None
    duration_value = (
        float(duration)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool)
        else None
    )
    duration_valid = duration_value is not None and duration_value > 0
    if expected == "voice_clone" and duration_value is not None:
        duration_valid = 5 <= duration_value <= 180
    if purpose != expected or verified is not True or not duration_valid:
        raise OralDomainError(f"{label}用途不匹配，请重新选择")


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Clone consent
# ---------------------------------------------------------------------------


def normalize_consent_purpose(purpose: str) -> str:
    normalized = purpose.strip().upper()
    aliases = {"AVATAR": "AVATAR_CLONE", "VOICE": "VOICE_CLONE"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ORAL_CONSENT_PURPOSES:
        raise OralDomainError("授权用途不支持")
    return normalized


def create_oral_consent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    source_asset_id: str,
    purpose: str,
    consent_text_version: str,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.consent.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    _require_own_identity(conn, actor, identity_id)
    if consent_text_version != ORAL_CONSENT_TEXT_VERSION:
        raise OralDomainError("授权文本版本已更新，请重新确认")
    asset = dict(
        require_asset_access(
            conn,
            actor=actor,
            asset_id=source_asset_id,
            action="oral.consent.create",
        )
    )
    if str(asset["created_by_user_id"] or "") != actor.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "ASSET_NOT_FOUND", "message": "Asset does not exist."},
        )
    source_sha256 = str(asset["sha256"] or "").strip()
    if int(asset["size_bytes"]) <= 0 or not source_sha256:
        raise OralDomainError("授权素材未完成或已失效，请重新上传")

    consent_id = str(uuid4())
    consented_at = datetime.now(UTC).isoformat()
    normalized_purpose = normalize_consent_purpose(purpose)
    with conn:
        conn.execute(
            """
            INSERT INTO oral_consents (
                id, identity_id, owner_user_id, source_asset_id, purpose,
                consent_text_version, source_sha256, consented_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                consent_id,
                identity_id,
                actor.id,
                source_asset_id,
                normalized_purpose,
                consent_text_version,
                source_sha256,
                consented_at,
            ),
        )
        write_audit(
            conn,
            actor=actor,
            action="oral.consent.create",
            entity_type="oral_consent",
            entity_id=consent_id,
            metadata={
                "identity_id": identity_id,
                "source_asset_id": source_asset_id,
                "purpose": normalized_purpose,
                "consent_text_version": consent_text_version,
                "source_sha256": source_sha256,
            },
            commit=False,
        )
    return _oral_consent_row(conn, consent_id)


def list_oral_consents(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> list[dict[str, Any]]:
    _require_own_identity(conn, actor, identity_id)
    rows = conn.execute(
        """
        SELECT id, identity_id, owner_user_id, source_asset_id, purpose,
               consent_text_version, source_sha256, consented_at, created_at
        FROM oral_consents
        WHERE identity_id = %s AND owner_user_id = %s
        ORDER BY consented_at DESC, id DESC
        """,
        (identity_id, actor.id),
    ).fetchall()
    return [dict(row) for row in rows]


def _oral_consent_row(conn: BusinessConnection, consent_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, identity_id, owner_user_id, source_asset_id, purpose,
               consent_text_version, source_sha256, consented_at, created_at
        FROM oral_consents WHERE id = %s
        """,
        (consent_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - inserted immediately before reading
        raise OralDomainError("克隆授权不存在或无权使用")
    return dict(row)


def _require_valid_consent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    consent_id: str,
    identity_id: str,
    source_asset_id: str,
    source_sha256: str,
    purpose: str,
) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM oral_consents
        WHERE id = %s AND owner_user_id = %s AND identity_id = %s
          AND source_asset_id = %s AND source_sha256 = %s AND purpose = %s
          AND consent_text_version = %s
        """,
        (
            consent_id,
            actor.id,
            identity_id,
            source_asset_id,
            source_sha256,
            purpose,
            ORAL_CONSENT_TEXT_VERSION,
        ),
    ).fetchone()
    if row is None:
        raise OralDomainError("克隆授权不存在、已失效或与当前素材不匹配")


# ---------------------------------------------------------------------------
# Avatar / voice cloning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloneStartResult:
    task_id: str
    status: str
    submission_state: str
    replayed: bool


def start_avatar_clone(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    title: str,
    source_asset_id: str,
    source_kind: str,
    consent_id: str,
    idempotency_key: str,
    vendor: HiflyClient | None = None,
) -> CloneStartResult:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.avatar.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    if source_kind not in {"VIDEO", "IMAGE"}:
        raise OralDomainError("分身素材类型不支持")
    _require_own_identity(conn, actor, identity_id)
    asset = _require_biometric_source_asset(
        conn,
        actor=actor,
        asset_id=source_asset_id,
        media_type=source_kind.lower(),
        label="分身素材",
    )
    _require_valid_consent(
        conn,
        actor=actor,
        consent_id=consent_id,
        identity_id=identity_id,
        source_asset_id=source_asset_id,
        source_sha256=str(asset["sha256"]),
        purpose="AVATAR_CLONE",
    )
    clean_title = title.strip() or "口播分身"
    request_hash = _request_hash(
        {
            "identity_id": identity_id,
            "title": clean_title,
            "source_asset_id": source_asset_id,
            "source_kind": source_kind,
            "consent_id": consent_id,
        }
    )
    existing = conn.execute(
        """
        SELECT id, status, submission_state, request_hash
        FROM oral_avatars
        WHERE owner_user_id = %s AND idempotency_key = %s
        """,
        (actor.id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"] or "") != request_hash:
            raise OralConflictError("幂等键已用于其他分身请求")
        return CloneStartResult(
            task_id=str(existing["id"]),
            status=str(existing["status"]),
            submission_state=str(existing["submission_state"]),
            replayed=True,
        )

    avatar_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO oral_avatars (
            id, identity_id, owner_user_id, title, status, source_kind,
            source_asset_id, consent_id, idempotency_key, request_hash,
            submission_state
        ) VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, 'LOCAL_PENDING')
        """,
        (
            avatar_id,
            identity_id,
            actor.id,
            clean_title,
            source_kind,
            source_asset_id,
            consent_id,
            idempotency_key,
            request_hash,
        ),
    )
    from app.usage_billing import accept_operation

    accept_operation(conn, user_id=actor.id, service="avatar_clone", source_id=avatar_id, units=1)
    conn.commit()
    return CloneStartResult(
        task_id=avatar_id,
        status="PENDING",
        submission_state="LOCAL_PENDING",
        replayed=False,
    )


def start_voice_clone(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    title: str,
    source_asset_id: str,
    consent_id: str,
    idempotency_key: str,
    language: str = DEFAULT_VOICE_LANGUAGE,
    vendor: HiflyClient | None = None,
) -> CloneStartResult:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    _require_own_identity(conn, actor, identity_id)
    asset = _require_biometric_source_asset(
        conn,
        actor=actor,
        asset_id=source_asset_id,
        media_type="audio",
        label="音频素材",
    )
    _require_audio_purpose(asset, "voice_clone", "声音克隆样本")
    if int(asset["size_bytes"]) > 20 * 1024 * 1024:
        raise OralDomainError("声音克隆样本不能超过 20 MB")
    _require_valid_consent(
        conn,
        actor=actor,
        consent_id=consent_id,
        identity_id=identity_id,
        source_asset_id=source_asset_id,
        source_sha256=str(asset["sha256"]),
        purpose="VOICE_CLONE",
    )
    if language not in VOICE_LANGUAGES:
        raise OralDomainError("不支持的声音样本语言")
    clean_title = title.strip() or "克隆声音"
    hashed_request: dict[str, Any] = {
        "identity_id": identity_id,
        "title": clean_title,
        "source_asset_id": source_asset_id,
        "consent_id": consent_id,
    }
    # 普通话不进哈希：上线前提交的请求（没有语言字段）重放时哈希不变，不会被误判冲突。
    if language != DEFAULT_VOICE_LANGUAGE:
        hashed_request["language"] = language
    request_hash = _request_hash(hashed_request)
    existing = conn.execute(
        """
        SELECT id, status, submission_state, request_hash
        FROM oral_voices
        WHERE owner_user_id = %s AND idempotency_key = %s
        """,
        (actor.id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"] or "") != request_hash:
            raise OralConflictError("幂等键已用于其他声音请求")
        return CloneStartResult(
            task_id=str(existing["id"]),
            status=str(existing["status"]),
            submission_state=str(existing["submission_state"]),
            replayed=True,
        )

    voice_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO oral_voices (
            id, identity_id, owner_user_id, title, status, source_asset_id,
            consent_id, idempotency_key, request_hash, submission_state, language
        ) VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, 'LOCAL_PENDING', %s)
        """,
        (
            voice_id,
            identity_id,
            actor.id,
            clean_title,
            source_asset_id,
            consent_id,
            idempotency_key,
            request_hash,
            language,
        ),
    )
    from app.usage_billing import accept_operation

    accept_operation(conn, user_id=actor.id, service="voice_clone", source_id=voice_id, units=1)
    conn.commit()
    return CloneStartResult(
        task_id=voice_id,
        status="PENDING",
        submission_state="LOCAL_PENDING",
        replayed=False,
    )


def _read_asset_bytes(conn: BusinessConnection, asset: dict[str, Any]) -> bytes:
    storage = storage_for_asset(conn, str(asset["storage_uri"]))
    key = (
        str(asset["storage_uri"]).split("/", 3)[-1]
        if "://" in str(asset["storage_uri"])
        else str(asset["storage_uri"])
    )
    try:
        return storage.get_object(key)
    except Exception as exc:  # noqa: BLE001 - surfaced as a customer-safe message
        logger.warning("oral source asset read failed: %s", type(exc).__name__)
        raise OralDomainError("素材读取失败，请重新上传") from exc


def _extension_for(asset: dict[str, Any], source_kind: str) -> str:
    content_type = str(asset.get("content_type") or "")
    if source_kind == "IMAGE" or content_type.startswith("image/"):
        return "png"
    if content_type.endswith("webm"):
        return "webm"
    return "mp4"


# ---------------------------------------------------------------------------
# Oral task lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OralTaskCreated:
    task_id: str
    status: str
    submission_state: str
    estimated_cost_fen: int
    replayed: bool


def create_oral_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    avatar_id: str,
    voice_id: str | None,
    mode: str,
    title: str,
    script_text: str | None,
    audio_asset_id: str | None,
    subtitle: dict[str, Any] | None,
    idempotency_key: str,
    vendor: HiflyClient | None = None,
) -> OralTaskCreated:
    try:
        subtitle = validate_oral_subtitle(subtitle)
    except HiflyError as exc:
        raise OralDomainError(str(exc)) from exc
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.task.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    if mode not in {"TTS", "AUDIO"}:
        raise OralDomainError("口播模式不支持")
    if not title.strip():
        raise OralDomainError("请填写作品标题")
    _require_own_identity(conn, actor, identity_id)
    clean_title = title.strip()[:120]
    request_hash = _request_hash(
        {
            "identity_id": identity_id,
            "avatar_id": avatar_id,
            "voice_id": voice_id,
            "mode": mode,
            "title": clean_title,
            "script_text": script_text,
            "audio_asset_id": audio_asset_id,
            "subtitle": subtitle,
        }
    )
    existing = conn.execute(
        """
        SELECT id, status, submission_state, estimated_cost_fen, request_hash
        FROM oral_tasks
        WHERE owner_user_id = %s AND idempotency_key = %s
        """,
        (actor.id, idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"] or "") != request_hash:
            raise OralConflictError("幂等键已用于其他口播请求")
        return OralTaskCreated(
            task_id=str(existing["id"]),
            status=str(existing["status"]),
            submission_state=str(existing["submission_state"]),
            estimated_cost_fen=int(existing["estimated_cost_fen"]),
            replayed=True,
        )

    avatar = conn.execute(
        """
        SELECT id, status, identity_id, source_kind, source_asset_id, consent_id
        FROM oral_avatars WHERE id = %s AND owner_user_id = %s
        """,
        (avatar_id, actor.id),
    ).fetchone()
    if avatar is None or str(avatar["status"]) != "READY":
        raise OralDomainError("请选择已就绪的口播分身")
    if avatar["identity_id"] != identity_id:
        raise OralDomainError("口播分身与人物不匹配")
    avatar_asset = _require_biometric_source_asset(
        conn,
        actor=actor,
        asset_id=str(avatar["source_asset_id"]),
        media_type=str(avatar["source_kind"]).lower(),
        label="分身素材",
    )
    if not avatar["consent_id"]:
        raise OralDomainError("口播分身缺少有效授权，请重新制作")
    _require_valid_consent(
        conn,
        actor=actor,
        consent_id=str(avatar["consent_id"]),
        identity_id=identity_id,
        source_asset_id=str(avatar["source_asset_id"]),
        source_sha256=str(avatar_asset["sha256"]),
        purpose="AVATAR_CLONE",
    )

    effective_voice = voice_id
    if mode == "TTS":
        if not script_text or not script_text.strip():
            raise OralDomainError("请填写口播文案")
        if len(script_text) > MAX_ORAL_SCRIPT_CHARS:
            raise OralDomainError("口播文案过长（上限 1 万字）")
        if not effective_voice:
            raise OralDomainError("请选择已就绪的声音")
        voice = conn.execute(
            """
            SELECT id, status, identity_id, confirmed, demo_asset_id,
                   source_asset_id, consent_id
            FROM oral_voices WHERE id = %s AND owner_user_id = %s
            """,
            (effective_voice, actor.id),
        ).fetchone()
        if voice is None or str(voice["status"]) != "READY":
            raise OralDomainError("请选择已就绪的声音")
        if voice["identity_id"] != identity_id:
            raise OralDomainError("声音与人物不匹配")
        if int(voice["confirmed"]) != 1:
            raise OralDomainError("请先试听并确认声音")
        if not voice["demo_asset_id"]:
            raise OralDomainError("声音试听文件未归档，请刷新后重试")
        voice_asset = _require_biometric_source_asset(
            conn,
            actor=actor,
            asset_id=str(voice["source_asset_id"]),
            media_type="audio",
            label="声音素材",
        )
        if not voice["consent_id"]:
            raise OralDomainError("声音缺少有效授权，请重新克隆")
        _require_valid_consent(
            conn,
            actor=actor,
            consent_id=str(voice["consent_id"]),
            identity_id=identity_id,
            source_asset_id=str(voice["source_asset_id"]),
            source_sha256=str(voice_asset["sha256"]),
            purpose="VOICE_CLONE",
        )
    else:
        effective_voice = None
        if not audio_asset_id:
            raise OralDomainError("请上传完整的口播音频")
        audio_asset = _require_source_asset(
            conn,
            actor=actor,
            asset_id=audio_asset_id,
            media_type="audio",
            label="口播音频",
        )
        _require_audio_purpose(audio_asset, "oral_audio", "口播音频")

    price = oral_unit_price_fen(conn)
    task_id = str(uuid4())
    with conn:
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, voice_id, mode, title,
                script_text, audio_asset_id, subtitle_json, status,
                estimated_cost_fen, idempotency_key, request_hash, submission_state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED', %s, %s, %s,
                      'LOCAL_PENDING')
            """,
            (
                task_id,
                actor.id,
                identity_id,
                avatar_id,
                effective_voice,
                mode,
                clean_title,
                script_text,
                audio_asset_id,
                json.dumps(subtitle, ensure_ascii=False) if subtitle else None,
                price,
                idempotency_key,
                request_hash,
            ),
        )
        reserve_oral_billing(conn, user_id=actor.id, oral_task_id=task_id)
        accepted = conn.execute(
            "SELECT reserved_credits,pricing_snapshot_json FROM billing_operations WHERE "
            "source_id=%s AND service='oral'",
            (task_id,),
        ).fetchone()
        ratio = json.loads(str(accepted[1])).get("points_per_yuan")
        price = (int(accepted[0]) * 100 + int(ratio) - 1) // int(ratio) if ratio else 0
        conn.execute("UPDATE oral_tasks SET estimated_cost_fen=%s WHERE id=%s", (price, task_id))
    row = _oral_task_row(conn, task_id)
    return OralTaskCreated(
        task_id=task_id,
        status=str(row["status"]),
        submission_state=str(row["submission_state"]),
        estimated_cost_fen=price,
        replayed=False,
    )


def _submit_oral_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    vendor: HiflyClient,
) -> None:
    row = _oral_task_row(conn, task_id)
    try:
        audio_target = None
        if row["mode"] == "AUDIO":
            asset = _asset(conn, str(row["audio_asset_id"]))
            if asset is None:
                raise OralDomainError("口播音频已失效，请重新上传")
            content = _read_asset_bytes(conn, asset)
            audio_target = vendor.create_upload_url("mp3")
            vendor.upload_file(audio_target, content)
        subtitle = json.loads(str(row["subtitle_json"])) if row["subtitle_json"] else None
        if row["mode"] == "TTS":
            vendor_task_id = vendor.create_video_by_tts(
                voice=_vendor_voice_id(conn, str(row["voice_id"])),
                text=str(row["script_text"]),
                avatar=_vendor_avatar_id(conn, str(row["avatar_id"])),
                title=str(row["title"])[:20],
                aigc_flag=True,
                subtitle=subtitle,
            )
        else:
            vendor_task_id = vendor.create_video_by_audio(
                avatar=_vendor_avatar_id(conn, str(row["avatar_id"])),
                title=str(row["title"])[:20],
                file_id=audio_target.file_id if audio_target else None,
                aigc_flag=True,
            )
    except HiflySubmissionUncertain as exc:
        conn.execute(
            """
            UPDATE oral_tasks
            SET submission_state = 'SUBMISSION_UNKNOWN', error_message = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND submission_state = 'LOCAL_PENDING'
            """,
            (str(exc)[:500], task_id),
        )
        conn.commit()
        raise
    except (HiflyError, OralDomainError) as exc:
        conn.execute(
            """
            UPDATE oral_tasks
            SET status = 'FAILED', submission_state = 'FAILED', error_message = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND submission_state = 'LOCAL_PENDING'
            """,
            (str(exc)[:500], task_id),
        )
        conn.commit()
        raise
    updated = conn.execute(
        """
        UPDATE oral_tasks
        SET status = 'RUNNING', vendor_task_id = %s, submission_state = 'SUBMITTED',
            error_message = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND submission_state = 'LOCAL_PENDING' AND vendor_task_id IS NULL
        """,
        (vendor_task_id, task_id),
    )
    conn.commit()
    if updated.rowcount != 1:
        raise OralConflictError("口播任务状态已变化，请刷新后查看")


def _vendor_avatar_id(conn: BusinessConnection, avatar_id: str) -> str:
    row = conn.execute(
        "SELECT vendor_avatar_id FROM oral_avatars WHERE id = %s", (avatar_id,)
    ).fetchone()
    if row is None or not row["vendor_avatar_id"]:
        raise OralDomainError("口播分身尚未就绪")
    return str(row["vendor_avatar_id"])


def _vendor_voice_id(conn: BusinessConnection, voice_id: str) -> str:
    row = conn.execute(
        "SELECT vendor_voice_id FROM oral_voices WHERE id = %s", (voice_id,)
    ).fetchone()
    if row is None or not row["vendor_voice_id"]:
        raise OralDomainError("声音尚未就绪")
    return str(row["vendor_voice_id"])


def _oral_task_row(conn: BusinessConnection, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM oral_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise OralTaskNotFoundError("口播任务不存在")
    return dict(row)


def read_oral_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
) -> dict[str, Any]:
    # 隐藏（customer_oral_task_visibility）是账号级列表偏好：owner 视角的
    # 读取统一跳过已隐藏行，钱包等账务读取不受影响。
    row = conn.execute(
        """
        SELECT * FROM oral_tasks
        WHERE id = %s AND owner_user_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM customer_oral_task_visibility v
              WHERE v.user_id = oral_tasks.owner_user_id AND v.task_id = oral_tasks.id
          )
        """,
        (task_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralTaskNotFoundError("口播任务不存在")
    return dict(row)


def hide_oral_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
) -> None:
    """把口播任务从本账号任务列表隐藏（账号级偏好，不取消、不删数据）。

    与普通批次的 ``customer_batch_visibility`` 语义对齐：``oral_tasks`` 行
    承载计费追溯且被分身/声音外键 RESTRICT 引用，因此只写偏好表——重复隐藏
    幂等，钱包事务与审计记录一律保留。
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.task.hide",
        entity_type="oral_task",
        entity_id=task_id,
    )
    # 归属判断必须绕过可见性过滤：重复隐藏同一条任务要保持幂等（204），
    # 不能因为第一次隐藏后 owner 视角已不可见而误报 404。
    row = conn.execute(
        "SELECT id FROM oral_tasks WHERE id = %s AND owner_user_id = %s",
        (task_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralTaskNotFoundError("口播任务不存在")
    with conn:
        inserted = conn.execute(
            """
            INSERT INTO customer_oral_task_visibility (user_id, task_id)
            VALUES (%s, %s)
            ON CONFLICT (user_id, task_id) DO NOTHING
            """,
            (actor.id, task_id),
        )
        if inserted.rowcount == 1:
            write_audit(
                conn,
                actor=actor,
                action="oral.task.hide",
                entity_type="oral_task",
                entity_id=task_id,
                metadata={"owner_user_id": actor.id},
                commit=False,
            )


def cancel_oral_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.task.cancel",
        entity_type="oral_task",
        entity_id=task_id,
    )
    row = read_oral_task(conn, task_id=task_id, actor=actor)
    if str(row["status"]) == "CANCELLED":
        return row
    if str(row["status"]) != "QUEUED":
        raise OralConflictError("只能取消尚未提交的排队任务")
    with conn:
        updated = conn.execute(
            """
            UPDATE oral_tasks
            SET status = 'CANCELLED', error_message = NULL,
                lease_owner = NULL, lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'QUEUED'
            """,
            (task_id, actor.id),
        )
        if updated.rowcount != 1:
            raise OralConflictError("任务状态已变化，请刷新后重试")
        finalize_oral_billing(conn, oral_task_id=task_id)
    return read_oral_task(conn, task_id=task_id, actor=actor)


# ---------------------------------------------------------------------------
# Pull-based vendor refresh (no public webhook available)
# ---------------------------------------------------------------------------


def refresh_oral_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
    vendor: HiflyClient,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.task.refresh",
        entity_type="oral_task",
        entity_id=task_id,
    )
    row = _oral_task_row(conn, task_id)
    if row["owner_user_id"] != actor.id:
        raise OralDomainError("口播任务不存在")
    if row["status"] == "RUNNING" and row["vendor_task_id"]:
        try:
            snapshot = vendor.video_task(str(row["vendor_task_id"]))
        except HiflyError as exc:
            logger.warning("oral vendor poll failed: %s", type(exc).__name__)
            return row
        if snapshot.status == "UNKNOWN":
            logger.warning("oral video vendor returned unknown status")
            return row
        if snapshot.status == "DONE":
            _archive_oral_result(
                conn,
                row=row,
                video_url=snapshot.video_url,
                duration_sec=snapshot.duration,
                vendor=vendor,
            )
        elif snapshot.status == "FAILED":
            conn.execute(
                """
            UPDATE oral_tasks
            SET status = 'FAILED', error_message = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
              AND vendor_task_id = %s
        """,
                (
                    "数字人服务生成失败，请调整内容后重试",
                    task_id,
                    actor.id,
                    row["vendor_task_id"],
                ),
            )
            conn.commit()
    return _oral_task_row(conn, task_id)


def _archive_oral_result(
    conn: BusinessConnection,
    *,
    row: dict[str, Any],
    video_url: str | None,
    duration_sec: int | None,
    vendor: HiflyClient,
) -> None:
    if not video_url:
        conn.execute(
            """
            UPDATE oral_tasks
            SET status = 'FAILED', error_message = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
              AND vendor_task_id = %s
        """,
            (
                "数字人服务未返回成片地址",
                str(row["id"]),
                str(row["owner_user_id"]),
                row["vendor_task_id"],
            ),
        )
        conn.commit()
        return
    try:
        content = vendor.download(video_url)
        inspect_media_bytes(content, suffix=".mp4", expected_type="video")
        storage: StorageAdapter = get_media_storage(conn)
        stored = storage.put_object(
            f"oral/results/{row['id']}.mp4", content, content_type="video/mp4"
        )
    except Exception as exc:  # noqa: BLE001 - keep the task retryable
        logger.warning("oral result archive failed: %s", type(exc).__name__)
        return
    asset_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id
        ) VALUES (%s, NULL, 'oral_video', %s, %s, %s, 'video/mp4', %s)
        """,
        (asset_id, stored.uri, stored.sha256, stored.size, row["owner_user_id"]),
    )
    updated = conn.execute(
        """
        UPDATE oral_tasks
        SET status = 'SUCCEEDED', result_asset_id = %s, duration_sec = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
          AND vendor_task_id = %s
        """,
        (
            asset_id,
            duration_sec,
            str(row["id"]),
            str(row["owner_user_id"]),
            row["vendor_task_id"],
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        try:
            content_store.delete_object_outside_content_namespace(
                storage, stored.key, actor_id=str(row["owner_user_id"])
            )
        except Exception:  # noqa: BLE001 - orphan cleanup is best effort
            logger.warning("oral result rollback cleanup failed")
        return
    conn.commit()


# ---------------------------------------------------------------------------
# Clone-task refresh & listing (pull-based; no webhook)
# ---------------------------------------------------------------------------


def _clone_status_map(vendor_status: str) -> str:
    return {
        "WAITING": "RUNNING",
        "PROCESSING": "RUNNING",
        "DONE": "READY",
        "FAILED": "FAILED",
        "UNKNOWN": "RUNNING",
    }[vendor_status]


def refresh_avatar_clone(
    conn: BusinessConnection,
    *,
    avatar_id: str,
    actor: CurrentUser,
    vendor: HiflyClient,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.avatar.refresh",
        entity_type="oral_avatar",
        entity_id=avatar_id,
    )
    row = conn.execute(
        "SELECT * FROM oral_avatars WHERE id = %s AND owner_user_id = %s",
        (avatar_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("口播分身任务不存在")
    record = dict(row)
    if record["status"] == "RUNNING" and record["vendor_task_id"]:
        try:
            snapshot = vendor.avatar_task(str(record["vendor_task_id"]))
        except HiflyError:
            return record
        if snapshot.status == "UNKNOWN":
            logger.warning("oral avatar vendor returned unknown status")
            return record
        if snapshot.status == "DONE" and not snapshot.avatar_id:
            logger.warning("oral avatar done response missing avatar id")
            return record
        status = _clone_status_map(snapshot.status)
        error = None if status != "FAILED" else "分身制作未通过，请更换素材后重试"
        conn.execute(
            """
            UPDATE oral_avatars
            SET status = %s, vendor_avatar_id = COALESCE(%s, vendor_avatar_id),
                error_message = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
              AND vendor_task_id = %s
            """,
            (
                status,
                snapshot.avatar_id,
                error,
                avatar_id,
                actor.id,
                record["vendor_task_id"],
            ),
        )
        conn.commit()
        record = dict(
            conn.execute("SELECT * FROM oral_avatars WHERE id = %s", (avatar_id,)).fetchone()
        )
    return record


def refresh_voice_clone(
    conn: BusinessConnection,
    *,
    voice_id: str,
    actor: CurrentUser,
    vendor: HiflyClient,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.refresh",
        entity_type="oral_voice",
        entity_id=voice_id,
    )
    row = conn.execute(
        "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s",
        (voice_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("声音克隆任务不存在")
    record = dict(row)
    if record["status"] == "RUNNING" and record["vendor_task_id"]:
        try:
            snapshot = vendor.voice_task(str(record["vendor_task_id"]))
        except HiflyError:
            return record
        if snapshot.status == "UNKNOWN":
            logger.warning("oral voice vendor returned unknown status")
            return record
        if snapshot.status == "DONE":
            if not snapshot.voice or not snapshot.demo_url:
                logger.warning("oral voice done response missing voice or demo URL")
                return record
            try:
                demo_content = vendor.download(snapshot.demo_url)
                if not demo_content:
                    logger.warning("oral voice demo download returned empty content")
                    return record
                inspect_media_bytes(demo_content, suffix=".mp3", expected_type="audio")
                storage = get_media_storage(conn)
                stored = storage.put_object(
                    f"oral/voices/{voice_id}/demo.mp3",
                    demo_content,
                    content_type="audio/mpeg",
                )
            except Exception as exc:  # noqa: BLE001 - refresh remains retryable
                logger.warning("oral voice demo archive failed: %s", type(exc).__name__)
                return record
            demo_asset_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id
                ) VALUES (%s, NULL, 'oral_audio', %s, %s, %s, 'audio/mpeg', %s)
                """,
                (demo_asset_id, stored.uri, stored.sha256, stored.size, actor.id),
            )
            updated = conn.execute(
                """
                UPDATE oral_voices
                SET status = 'READY', vendor_voice_id = %s, demo_asset_id = %s,
                    error_message = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
                  AND vendor_task_id = %s
                """,
                (
                    snapshot.voice,
                    demo_asset_id,
                    voice_id,
                    actor.id,
                    record["vendor_task_id"],
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                try:
                    content_store.delete_object_outside_content_namespace(
                        storage, stored.key, actor_id=actor.id
                    )
                except Exception:  # noqa: BLE001 - orphan cleanup is best effort
                    logger.warning("oral voice demo rollback cleanup failed")
                return record
            conn.commit()
        elif snapshot.status == "FAILED":
            conn.execute(
                """
                UPDATE oral_voices
                SET status = 'FAILED', error_message = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND owner_user_id = %s AND status = 'RUNNING'
                  AND vendor_task_id = %s
                """,
                (
                    "声音克隆未通过，请更换音频后重试",
                    voice_id,
                    actor.id,
                    record["vendor_task_id"],
                ),
            )
            conn.commit()
        else:
            return record
        record = dict(
            conn.execute("SELECT * FROM oral_voices WHERE id = %s", (voice_id,)).fetchone()
        )
    return record


def confirm_voice_clone(
    conn: BusinessConnection,
    *,
    voice_id: str,
    actor: CurrentUser,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.confirm",
        entity_type="oral_voice",
        entity_id=voice_id,
    )
    row = conn.execute(
        "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s",
        (voice_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("声音克隆任务不存在")
    record = dict(row)
    if record["status"] != "READY":
        raise OralDomainError("声音尚未就绪，无法确认")
    if not record["demo_asset_id"]:
        raise OralDomainError("声音试听文件未归档，无法确认")
    if not record["consent_id"]:
        raise OralDomainError("声音缺少有效授权，请重新克隆")
    source_asset = _require_biometric_source_asset(
        conn,
        actor=actor,
        asset_id=str(record["source_asset_id"]),
        media_type="audio",
        label="声音素材",
    )
    _require_valid_consent(
        conn,
        actor=actor,
        consent_id=str(record["consent_id"]),
        identity_id=str(record["identity_id"]),
        source_asset_id=str(record["source_asset_id"]),
        source_sha256=str(source_asset["sha256"]),
        purpose="VOICE_CLONE",
    )
    if int(record["confirmed"]) == 1:
        return record

    confirmed_at = datetime.now(UTC).isoformat()
    with conn:
        updated = conn.execute(
            """
            UPDATE oral_voices
            SET confirmed = 1, confirmed_by_user_id = %s, confirmed_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'READY'
              AND confirmed = 0
            """,
            (actor.id, confirmed_at, voice_id, actor.id),
        )
        if updated.rowcount != 1:
            concurrent = conn.execute(
                "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s",
                (voice_id, actor.id),
            ).fetchone()
            if concurrent is not None and int(concurrent["confirmed"]) == 1:
                return dict(concurrent)
            raise OralConflictError("声音确认状态已变化，请刷新后重试")
        write_audit(
            conn,
            actor=actor,
            action="oral.voice.confirm",
            entity_type="oral_voice",
            entity_id=voice_id,
            metadata={"identity_id": str(record["identity_id"])},
            commit=False,
        )
    confirmed = conn.execute(
        "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s",
        (voice_id, actor.id),
    ).fetchone()
    return dict(confirmed)


MAX_CLONE_TITLE_CHARS = 60


def _title_search_clause(query: str | None) -> tuple[str, tuple[object, ...]]:
    """按名称模糊搜索；转义 LIKE 通配符，让用户输入的 % 与 _ 按字面匹配。"""
    clean = (query or "").strip()
    if not clean:
        return "", ()
    escaped = clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return " AND title ILIKE %s ESCAPE '\\'", (f"%{escaped}%",)


def _clean_clone_title(title: str) -> str:
    clean = title.strip()
    if not clean:
        raise OralDomainError("名称不能为空")
    if len(clean) > MAX_CLONE_TITLE_CHARS:
        raise OralDomainError(f"名称不能超过 {MAX_CLONE_TITLE_CHARS} 个字")
    return clean


def list_avatars(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    query: str | None = None,
) -> list[dict[str, Any]]:
    # 只查本系统记录的、当前账号创建的分身：上游「自己的数字人」列表是整个平台账号
    # 维度，含其他用户的分身，不能直接透出。
    search_sql, search_params = _title_search_clause(query)
    rows = conn.execute(
        f"""
        SELECT * FROM oral_avatars
        WHERE identity_id = %s AND owner_user_id = %s AND deleted_at IS NULL{search_sql}
        ORDER BY created_at DESC
        """,
        (identity_id, actor.id, *search_params),
    ).fetchall()
    return [dict(row) for row in rows]


def _rename_clone(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    table: Literal["oral_avatars", "oral_voices"],
    entity_type: Literal["oral_avatar", "oral_voice"],
    record_id: str,
    title: str,
    missing_message: str,
) -> dict[str, Any]:
    """只改本地名称：上游没有改名接口，界面、搜索与列表都以本地名称为准。"""
    action = f"{entity_type.replace('_', '.')}.rename"
    require_not_auditor(
        conn, actor=actor, action=action, entity_type=entity_type, entity_id=record_id
    )
    clean_title = _clean_clone_title(title)
    before = conn.execute(
        f"SELECT title FROM {table} "
        "WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL FOR UPDATE",
        (record_id, actor.id),
    ).fetchone()
    if before is None:
        raise OralDomainError(missing_message)
    if str(before["title"]) != clean_title:
        conn.execute(
            f"UPDATE {table} SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (clean_title, record_id),
        )
        write_audit(
            conn,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=record_id,
            metadata={"before": str(before["title"]), "after": clean_title},
            commit=False,
        )
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id = %s", (record_id,)).fetchone())


def rename_avatar_clone(
    conn: BusinessConnection, *, actor: CurrentUser, avatar_id: str, title: str
) -> dict[str, Any]:
    return _rename_clone(
        conn,
        actor=actor,
        table="oral_avatars",
        entity_type="oral_avatar",
        record_id=avatar_id,
        title=title,
        missing_message="口播分身任务不存在",
    )


def rename_voice_clone(
    conn: BusinessConnection, *, actor: CurrentUser, voice_id: str, title: str
) -> dict[str, Any]:
    return _rename_clone(
        conn,
        actor=actor,
        table="oral_voices",
        entity_type="oral_voice",
        record_id=voice_id,
        title=title,
        missing_message="声音克隆任务不存在",
    )


def read_avatar_clone(
    conn: BusinessConnection, *, actor: CurrentUser, avatar_id: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM oral_avatars WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
        (avatar_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("口播分身任务不存在")
    return dict(row)


def list_voices(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    query: str | None = None,
) -> list[dict[str, Any]]:
    # 与分身同理：上游声音列表是平台账号维度，只能以本系统的归属记录为准。
    search_sql, search_params = _title_search_clause(query)
    rows = conn.execute(
        f"""
        SELECT * FROM oral_voices
        WHERE identity_id = %s AND owner_user_id = %s AND deleted_at IS NULL{search_sql}
        ORDER BY created_at DESC
        """,
        (identity_id, actor.id, *search_params),
    ).fetchall()
    return [dict(row) for row in rows]


def read_voice_clone(
    conn: BusinessConnection, *, actor: CurrentUser, voice_id: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
        (voice_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("声音克隆任务不存在")
    return dict(row)


# --------------------------------------------------------------------------- #
# Voice settings (语速 / 音量 / 音调).
#
# 上游的生成接口不接受这三个参数，只能通过编辑接口改写声音本身，之后用该声音
# 生成的口播都按新参数合成。上游调用可能耗时数十秒，不能占着围栏事务里的会话行
# 锁，所以拆成三段：事务内校验取标识 → 事务外调用上游 → 新事务复核后落库与审计。
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VoiceSettings:
    speech_rate: Decimal
    volume: Decimal
    pitch: Decimal


VOICE_SETTING_RANGES: dict[str, tuple[Decimal, Decimal, str]] = {
    "speech_rate": (Decimal("0.5"), Decimal("2.0"), "语速"),
    "volume": (Decimal("0.1"), Decimal("2.0"), "音量"),
    "pitch": (Decimal("0.1"), Decimal("2.0"), "音调"),
}


def _require_valid_voice_settings(settings: VoiceSettings) -> None:
    for field, (low, high, label) in VOICE_SETTING_RANGES.items():
        value: Decimal = getattr(settings, field)
        # 库列是 NUMERIC(2,1)，多余的小数位会被静默四舍五入，与上游实际值不一致。
        exponent = value.as_tuple().exponent
        if (
            not value.is_finite()
            or not low <= value <= high
            or (isinstance(exponent, int) and exponent < -1)
        ):
            raise OralDomainError(f"{label}需在 {low}–{high} 之间，且最多一位小数")


def begin_voice_settings_update(
    conn: BusinessConnection, *, actor: CurrentUser, voice_id: str
) -> str:
    """校验声音可调参数并返回上游声音标识，供调用方在事务外调用上游。"""
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.settings",
        entity_type="oral_voice",
        entity_id=voice_id,
    )
    row = conn.execute(
        "SELECT status, vendor_voice_id FROM oral_voices "
        "WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
        (voice_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralDomainError("声音克隆任务不存在")
    if row["status"] != "READY" or not row["vendor_voice_id"]:
        raise OralDomainError("声音尚未克隆完成，暂不能调整参数")
    return str(row["vendor_voice_id"])


def apply_voice_settings_to_vendor(
    vendor: HiflyClient, *, vendor_voice_id: str, settings: VoiceSettings
) -> None:
    _require_valid_voice_settings(settings)
    vendor.edit_voice(
        voice=vendor_voice_id,
        rate=format(settings.speech_rate, ".1f"),
        volume=format(settings.volume, ".1f"),
        pitch=format(settings.pitch, ".1f"),
    )


def finish_voice_settings_update(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    voice_id: str,
    vendor_voice_id: str,
    settings: VoiceSettings,
) -> dict[str, Any]:
    """上游已接受新参数后落库并审计；声音在此期间被删除或替换则报冲突。"""
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.settings",
        entity_type="oral_voice",
        entity_id=voice_id,
    )
    _require_valid_voice_settings(settings)
    before = conn.execute(
        "SELECT speech_rate, volume, pitch FROM oral_voices "
        "WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL "
        "AND vendor_voice_id = %s FOR UPDATE",
        (voice_id, actor.id, vendor_voice_id),
    ).fetchone()
    if before is None:
        raise OralConflictError("声音状态已变化，请刷新后重试")
    conn.execute(
        """
        UPDATE oral_voices
        SET speech_rate = %s, volume = %s, pitch = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (settings.speech_rate, settings.volume, settings.pitch, voice_id),
    )
    write_audit(
        conn,
        actor=actor,
        action="oral.voice.settings",
        entity_type="oral_voice",
        entity_id=voice_id,
        metadata={
            field: {"before": str(before[field]), "after": str(getattr(settings, field))}
            for field in VOICE_SETTING_RANGES
        },
        commit=False,
    )
    return dict(conn.execute("SELECT * FROM oral_voices WHERE id = %s", (voice_id,)).fetchone())


# --------------------------------------------------------------------------- #
# Soft delete for clone records.
#
# ``oral_tasks.avatar_id`` / ``voice_id`` are ON DELETE RESTRICT, so a hard
# delete would fail whenever any task ever referenced the clone. The upstream
# vendor exposes no delete endpoint either, so deletion is a *local* soft hide:
# we stamp ``deleted_at`` / ``deleted_by_user_id`` and every owner-facing read
# filters ``deleted_at IS NULL``. The row (and its source/demo assets) survive
# for audit and billing history; admins can still see it.
#
# A clone is only deletable once it is no longer in flight (its own status is
# terminal and its vendor submission has settled) and no live oral task still
# references it — otherwise the worker could resurrect or bill against a clone
# the user believes is gone.
# --------------------------------------------------------------------------- #

_ACTIVE_CLONE_STATUSES = frozenset({"PENDING", "RUNNING"})
_ACTIVE_SUBMISSION_STATES = frozenset({"LOCAL_PENDING", "SUBMITTING", "SUBMISSION_UNKNOWN"})
_TERMINAL_TASK_STATUSES_SQL = "('SUCCEEDED', 'FAILED', 'CANCELLED')"


def _guard_clone_deletable(record: dict[str, Any], *, label: str) -> None:
    if str(record["status"]) in _ACTIVE_CLONE_STATUSES:
        raise OralResourceInUseError(f"{label}正在制作中，请等待完成或失败后再删除")
    if str(record.get("submission_state") or "") in _ACTIVE_SUBMISSION_STATES:
        raise OralResourceInUseError(f"{label}提交尚未确认，请稍后刷新状态再删除")


def _guard_no_active_oral_tasks(
    conn: BusinessConnection,
    *,
    fk_column: str,
    resource_id: str,
    owner_id: str,
    label: str,
) -> None:
    if fk_column not in ("avatar_id", "voice_id"):
        raise AssertionError("unreachable: fk_column must be an internal literal")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM oral_tasks "
        f"WHERE {fk_column} = %s AND owner_user_id = %s "
        f"AND status NOT IN {_TERMINAL_TASK_STATUSES_SQL}",
        (resource_id, owner_id),
    ).fetchone()
    if row is not None and int(row["n"]) > 0:
        raise OralResourceInUseError(f"{label}仍被进行中的口播任务使用，请等待任务完成后再删除")


def delete_avatar_clone(
    conn: BusinessConnection,
    *,
    avatar_id: str,
    actor: CurrentUser,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.avatar.delete",
        entity_type="oral_avatar",
        entity_id=avatar_id,
    )
    row = conn.execute(
        "SELECT * FROM oral_avatars WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
        (avatar_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralResourceNotFoundError("口播分身不存在或已删除")
    record = dict(row)
    _guard_clone_deletable(record, label="口播分身")
    _guard_no_active_oral_tasks(
        conn,
        fk_column="avatar_id",
        resource_id=avatar_id,
        owner_id=actor.id,
        label="口播分身",
    )
    deleted_at = datetime.now(UTC).isoformat()
    with conn:
        updated = conn.execute(
            """
            UPDATE oral_avatars
            SET deleted_at = %s, deleted_by_user_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL
            """,
            (deleted_at, actor.id, avatar_id, actor.id),
        )
        if updated.rowcount != 1:
            concurrent = conn.execute(
                "SELECT deleted_at FROM oral_avatars WHERE id = %s AND owner_user_id = %s",
                (avatar_id, actor.id),
            ).fetchone()
            if concurrent is not None and concurrent["deleted_at"]:
                return {"id": avatar_id, "deleted_at": str(concurrent["deleted_at"])}
            raise OralConflictError("口播分身删除状态已变化，请刷新后重试")
        write_audit(
            conn,
            actor=actor,
            action="oral.avatar.delete",
            entity_type="oral_avatar",
            entity_id=avatar_id,
            metadata={"identity_id": str(record["identity_id"])},
            commit=False,
        )
    return {"id": avatar_id, "deleted_at": deleted_at}


def delete_voice_clone(
    conn: BusinessConnection,
    *,
    voice_id: str,
    actor: CurrentUser,
) -> dict[str, Any]:
    require_not_auditor(
        conn,
        actor=actor,
        action="oral.voice.delete",
        entity_type="oral_voice",
        entity_id=voice_id,
    )
    row = conn.execute(
        "SELECT * FROM oral_voices WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
        (voice_id, actor.id),
    ).fetchone()
    if row is None:
        raise OralResourceNotFoundError("声音克隆不存在或已删除")
    record = dict(row)
    _guard_clone_deletable(record, label="声音")
    _guard_no_active_oral_tasks(
        conn,
        fk_column="voice_id",
        resource_id=voice_id,
        owner_id=actor.id,
        label="声音",
    )
    deleted_at = datetime.now(UTC).isoformat()
    with conn:
        updated = conn.execute(
            """
            UPDATE oral_voices
            SET deleted_at = %s, deleted_by_user_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL
            """,
            (deleted_at, actor.id, voice_id, actor.id),
        )
        if updated.rowcount != 1:
            concurrent = conn.execute(
                "SELECT deleted_at FROM oral_voices WHERE id = %s AND owner_user_id = %s",
                (voice_id, actor.id),
            ).fetchone()
            if concurrent is not None and concurrent["deleted_at"]:
                return {"id": voice_id, "deleted_at": str(concurrent["deleted_at"])}
            raise OralConflictError("声音删除状态已变化，请刷新后重试")
        write_audit(
            conn,
            actor=actor,
            action="oral.voice.delete",
            entity_type="oral_voice",
            entity_id=voice_id,
            metadata={"identity_id": str(record["identity_id"])},
            commit=False,
        )
    return {"id": voice_id, "deleted_at": deleted_at}


def list_oral_tasks(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    visible_sql = (
        "AND NOT EXISTS ("
        " SELECT 1 FROM customer_oral_task_visibility v"
        " WHERE v.user_id = oral_tasks.owner_user_id AND v.task_id = oral_tasks.id)"
    )
    total_row = conn.execute(
        f"SELECT COUNT(*) AS total FROM oral_tasks WHERE owner_user_id = %s {visible_sql}",
        (actor.id,),
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT * FROM oral_tasks
        WHERE owner_user_id = %s {visible_sql}
        ORDER BY created_at DESC
        {PAGE_CLAUSE}
        """,
        (actor.id, *page_bounds(limit, offset, max_limit=100, min_limit=1)),
    ).fetchall()
    return [dict(row) for row in rows], int(total_row["total"] if total_row else 0)


def oral_price_quote(conn: BusinessConnection) -> dict[str, int | float]:
    from decimal import Decimal

    from app.billing_catalog import retail_snapshot

    snapshot = retail_snapshot(conn, "oral", 1)
    unit = (
        Decimal(str(snapshot["unit_credits"]))
        * Decimal(str(snapshot["discount_basis_points"]))
        / 10000
        if snapshot["enabled"]
        else Decimal(0)
    )
    ratio = snapshot["points_per_yuan"]
    return {
        "unit_price_fen": int(unit * 100 / Decimal(str(ratio))) if ratio else 0,
        "unit_credits": float(unit),
        "credit_price_version": int(str(snapshot["version"])),
    }


def oral_task_available_actions(row: dict[str, Any]) -> list[str]:
    """Safe retry hints for the customer task center.

    ``archive_retry`` maps to POST /tasks/{id}/archive-retry, which further
    requires an archived provider result URL. Submission-uncertain work needs
    provider evidence and manual reconciliation; it must never be re-posted.
    """
    status = str(row["status"])
    if status == "ARCHIVE_FAILED" and str(row.get("provider_result_url") or "").strip():
        return ["archive_retry"]
    return []


def oral_terminal_billing_states(conn: BusinessConnection, *, owner_user_id: str) -> dict[str, str]:
    """Map task id -> SETTLE/RELEASE for each task's current billing round.

    Wallet rows are the billing truth: a task whose current round has no
    terminal transaction still holds its reservation (open or frozen).
    """
    rows = conn.execute(
        """
        SELECT t.id AS task_id, wt.type AS terminal_type
        FROM oral_tasks AS t
        JOIN wallet_transactions AS wt
          ON wt.oral_task_id = t.id AND wt.billing_round = t.billing_round
        WHERE t.owner_user_id = %s AND wt.type IN ('SETTLE', 'RELEASE')
        """,
        (owner_user_id,),
    ).fetchall()
    result = {str(row["task_id"]): str(row["terminal_type"]) for row in rows}
    for operation in conn.execute(
        "SELECT o.source_id,o.state,o.reserved_credits FROM billing_operations o "
        "JOIN oral_tasks t ON t.id=o.source_id AND t.billing_round=o.billing_round "
        "WHERE o.user_id=%s AND o.service='oral'",
        (owner_user_id,),
    ).fetchall():
        result[str(operation[0])] = (
            "FREE"
            if not operation[2]
            else "SETTLE"
            if operation[1] == "SUCCEEDED"
            else "RELEASE"
            if operation[1] in {"FAILED", "CANCELLED"}
            else "RESERVED"
        )
    return result
