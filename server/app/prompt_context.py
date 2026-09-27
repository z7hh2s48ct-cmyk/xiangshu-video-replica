"""Resolve authorized context without trusting client labels or signed URLs."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.h3_prompts import GenerationContext, digest
from app.permissions import require_asset_access, require_project_access
from app.storage import StorageAdapter, require_storage_match, storage_object_ref_from_uri

IMAGE_KINDS = {
    "first_frame",
    "reference_image",
    "character_image",
    "source_frame",
    "character_sheet",
    "uploaded_image",
}
VIDEO_KINDS = {"reference_video", "generated_video", "video"}
AUDIO_KINDS = {"reference_audio", "audio", "voice_audio", "audio_input"}

# 用途留空不再让整单失败（旧行为要求用户先逐项填完才允许生成）：音频按默认
# 「音色参考」生成（音轨不传模型，留空只能猜），图片与视频留给模型按画面判断；
# 两种情况都写进 needs_confirmation，由核对区提示用户确认。
_UNDECLARED_PURPOSES = ("unspecified", "reference")
DEFAULT_AUDIO_PURPOSE = "音色参考"


def resolve_context(
    conn: BusinessConnection, *, actor: CurrentUser, request: GenerationContext
) -> dict[str, Any]:
    data = request.model_dump(mode="json")
    if request.project_id:
        require_project_access(
            conn, actor=actor, project_id=request.project_id, action="prompt.context"
        )
    for key, kind in (
        ("analysis_version_id", "analysis"),
        ("shot_card_version_id", "shot_card"),
        ("script_version_id", "script"),
    ):
        if data[key]:
            row = conn.execute("SELECT * FROM versions WHERE id = %s", (data[key],)).fetchone()
            if row is None or row["kind"] != kind or row["project_id"] != request.project_id:
                raise HTTPException(404, detail={"code": "PROMPT_SOURCE_NOT_FOUND"})
            latest = conn.execute(
                "SELECT id FROM versions WHERE project_id=%s AND kind=%s "
                "ORDER BY version_number DESC LIMIT 1",
                (request.project_id, kind),
            ).fetchone()
            if latest is None or str(latest["id"]) != data[key]:
                raise HTTPException(
                    409,
                    detail={
                        "code": "PROMPT_SOURCE_STALE",
                        "message": "分析、分镜或文案已更新，请重新合成最终提示词。",
                    },
                )
    if request.source_asset_id:
        require_asset_access(
            conn, actor=actor, asset_id=request.source_asset_id, action="prompt.context"
        )
    issues: list[dict[str, str]] = []
    if request.route == "reference" and (
        request.first_frame_asset_id or request.last_frame_asset_id
    ):
        raise HTTPException(422, detail={"code": "REFERENCE_FRAME_ROLE_CONFLICT"})
    if request.route != "reference" and request.references:
        raise HTTPException(422, detail={"code": "REFERENCE_ROUTE_REQUIRED"})
    mode = request.mode()
    if (mode == "I2VA" and not request.first_frame_asset_id) or (
        mode == "Ref2VA" and not request.references
    ):
        issues.append(
            {"code": "GENERATION_ASSET_REQUIRED", "message": "请先选择当前模式所需的生成素材。"}
        )
    slots = [
        (request.first_frame_asset_id, "first_frame", "first_frame"),
        (request.last_frame_asset_id, "last_frame", "last_frame"),
    ]
    slots += [(ref.asset_id, "reference", ref.purpose) for ref in request.references]
    assets: list[dict[str, str]] = []
    counts = {"Picture": 0, "Video": 0, "Audio": 0}
    for asset_id, role, purpose in slots:
        if not asset_id:
            continue
        asset = require_asset_access(conn, actor=actor, asset_id=asset_id, action="prompt.context")
        kind = str(asset["kind"])
        mime = str(asset["content_type"])
        media = (
            "Picture"
            if kind in IMAGE_KINDS or mime.startswith("image/")
            else (
                "Video"
                if kind in VIDEO_KINDS or mime.startswith("video/")
                else "Audio"
                if kind in AUDIO_KINDS or mime.startswith("audio/")
                else ""
            )
        )
        if not media or (role != "reference" and media != "Picture"):
            raise HTTPException(422, detail={"code": "PROMPT_ASSET_KIND_INVALID"})
        counts[media] += 1
        assets.append(
            {
                "asset_id": asset_id,
                "label": f"<{media} {counts[media]}>",
                "alias": f"@{len(assets) + 1}",
                "role": role,
                "kind": media.lower(),
                "purpose": purpose,
            }
        )
    if len({a["asset_id"] for a in assets}) != len(assets):
        raise HTTPException(422, detail={"code": "DUPLICATE_REFERENCE"})
    if counts["Picture"] > 8 or counts["Video"] > 3 or counts["Audio"] > 3:
        raise HTTPException(422, detail={"code": "REFERENCE_LIMIT_EXCEEDED"})
    needs_confirmation: list[dict[str, str]] = []
    for item in assets:
        if item["purpose"] not in _UNDECLARED_PURPOSES:
            continue
        if item["kind"] == "audio":
            # 音轨不随上下文传给模型，用途只能来自用户文字：留空必须给一个安全的
            # 默认值，否则模型会凭空猜声音。默认值本身也是推断，要用户确认。
            item["purpose"] = DEFAULT_AUDIO_PURPOSE
            needs_confirmation.append(
                {
                    "code": "AUDIO_PURPOSE_INFERRED",
                    "message": (
                        f"音频{item['alias']}未填写用途，已按「{DEFAULT_AUDIO_PURPOSE}」生成；"
                        "系统不分析音轨内容，请核对。"
                    ),
                    "alias": item["alias"],
                    "label": item["label"],
                    "purpose": item["purpose"],
                }
            )
            continue
        # 图片与视频会作为素材图交给模型：用途留空就让它按画面内容判断，不按顺序
        # 臆造用途——把场景图当成人物参考会直接写错 subject_definitions。
        needs_confirmation.append(
            {
                "code": "REFERENCE_PURPOSE_UNDECLARED",
                "message": f"{item['label']}未填写用途，系统按画面内容判断，请核对。",
                "alias": item["alias"],
                "label": item["label"],
            }
        )
    context = {
        **data,
        "mode": mode,
        "generation_assets": assets,
        "issues": issues,
    }
    # 只在真有推断项时才加这个键：分析流（无参考素材）的上下文要与历史行逐字节
    # 一致，否则升级后重试同一视频会撞上 ANALYSIS_CONTEXT_CONFLICT。
    if needs_confirmation:
        context["needs_confirmation"] = needs_confirmation
    context["context_hash"] = digest(context)
    return context


def attach_context_media(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    context: dict[str, Any],
    storage: StorageAdapter,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for item in context["generation_assets"]:
        asset = require_asset_access(
            conn, actor=actor, asset_id=item["asset_id"], action="prompt.context"
        )
        ref = storage_object_ref_from_uri(str(asset["storage_uri"]))
        require_storage_match(storage, ref)
        url = storage.create_download_intent(
            ref.key, expires_in=timedelta(minutes=15), can_read=True
        ).url
        if not url.startswith("https://"):
            raise HTTPException(
                422,
                detail={
                    "code": "PROMPT_MEDIA_UNAVAILABLE",
                    "message": "云端模型无法读取参考素材。",
                },
            )
        content.append({"type": "text", "text": item["label"] + ": " + item["purpose"]})
        # Audio references remain bound, but this gateway has no verified audio
        # input. Use only the user's explicit description, never claim to hear it.
        if item["kind"] == "audio":
            content.append(
                {
                    "type": "text",
                    "text": (
                        "音轨未传给分析模型；仅按用户说明保留引用。不能推断音色、台词或节奏。"
                    ),
                }
            )
            continue
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def context_source_data(conn: BusinessConnection, context: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("analysis_version_id", "shot_card_version_id", "script_version_id"):
        if context.get(key):
            row = conn.execute(
                "SELECT payload_json FROM versions WHERE id = %s", (context[key],)
            ).fetchone()
            if row is not None:
                result[key] = json.loads(str(row["payload_json"]))
    return result
