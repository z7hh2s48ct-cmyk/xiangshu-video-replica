from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import content_store
from app.analysis import APILIO_GEMINI_MODEL, insert_version
from app.auth import CurrentUser
from app.character_reference_matching import (
    current_character_reference_selection_for_generation,
)
from app.characters import character_is_available, get_project_main_character, read_character
from app.db_portable import BusinessConnection
from app.external_calls import CallTimer, endpoint_from_url, record_external_call, recorded_urlopen
from app.net_safety import FAKE_IP_NETWORK
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    ExtractedSourceFrame,
    SourceFrameCandidateAssessment,
    SourceFrameSemanticInspection,
    latest_version,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    create_local_storage_from_environment,
    require_storage_match,
    storage_object_ref_from_uri,
)
from app.viral_media import ViralMediaError
from app.viral_media import pinned_connection as _pinned_connection

logger = logging.getLogger(__name__)

FIRST_FRAME_CANDIDATES_KIND = "first_frame_candidates"
FIRST_FRAME_SELECTION_KIND = "first_frame_selection"
FIRST_FRAME_SCHEMA_VERSION = "b5.first-frame.v1"
PROJECT_CHARACTER_APPEARANCE_KIND = "project_character_appearance"
PROJECT_CHARACTER_APPEARANCE_SCHEMA_VERSION = "wp1.project-character-appearance.v2"
FIRST_FRAME_REPLACEMENT_CONTRACT_VERSION = 3
FIRST_FRAME_RECONSTRUCTION_MODE = "full_person_replace.v1"
FIRST_FRAME_MODELS = ("gpt-image-2", "nano-banana-pro-2k")
FIRST_FRAME_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FIRST_FRAME_CANDIDATES = 3
APILIO_DEFAULT_BASE_URL = "https://api.apilio.ai"
APILIO_IMAGE_EDIT_PATH = "/v1/images/edits"
# gpt-image-2 的尺寸档位。2K 必须通过 `size` 兑现：2026-09-22 对 apilio 的实测
# 证明 `image_size=2K` 这类档位别名会被网关静默忽略——同图同参下，带该字段与
# 不带该字段的出图逐像素同尺寸（1792x1008），既不生效也不报错。只有把尺寸写成
# WIDTHxHEIGHT 才真正改变出图（2048x1152 / 1152x2048 均实测命中）。
# 取值须满足网关硬约束：宽高各能被 16 整除、宽高比在 1:3~3:1、总像素在
# 655,360~8,294,400；超过 2560x1440 属实验档位，故上限压在 2048 长边。
FIRST_FRAME_IMAGE_SIZES: Mapping[str, str] = {
    "9:16": "1152x2048",
    "16:9": "2048x1152",
    "1:1": "2048x2048",
    "3:4": "1536x2048",
    "4:3": "2048x1536",
    "2:3": "1360x2048",
    "3:2": "2048x1360",
    "4:5": "1632x2048",
    "5:4": "2048x1632",
    "21:9": "2048x880",
}
APILIO_OUTPUT_HOSTS = frozenset({"files.closeai.fans"})
MAX_PROVIDER_IMAGE_BYTES = 20 * 1024 * 1024
MAX_QUALITY_IMAGE_BYTES = 12 * 1024 * 1024
MAX_QUALITY_REQUEST_IMAGE_BYTES = 32 * 1024 * 1024
SOURCE_FRAME_QUALITY_TIMEOUT_SECONDS = 8.0
# 质检是标注不是闸门：先出图后质检，最多自动补做一轮；未通过的候选照样
# 发布给用户，由人工确认环节决定是否使用。
MAX_FIRST_FRAME_QUALITY_ATTEMPTS = 2
# 单张产品流：每次付费任务交付 1 张，再次生成把新候选追加进最新候选版本；
# 池子封顶防止无限重生成的 payload 无界增长（超出的旧图仍在历史版本里）。
MAX_FIRST_FRAME_CANDIDATE_POOL = 6
MAX_SCENE_CONTACT_SHEET_QUALITY_ATTEMPTS = 2
MIN_FIRST_FRAME_IDENTITY_SCORE = 0.78
MIN_FIRST_FRAME_RECONSTRUCTION_SCORE = 0.75
MIN_FIRST_FRAME_OUTFIT_SCORE = 0.7
MIN_SCENE_CONTACT_SHEET_IDENTITY_SCORE = 0.78
MIN_SCENE_CONTACT_SHEET_OUTFIT_SCORE = 0.7
MIN_SCENE_CONTACT_SHEET_SCENE_SCORE = 0.7
MIN_GENERATED_VIDEO_IDENTITY_SCORE = 0.75
MIN_GENERATED_VIDEO_OUTFIT_SCORE = 0.7
MIN_GENERATED_VIDEO_MOTION_SCORE = 0.65
APILIO_OUTPUT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
FIRST_FRAME_NO_TEXT_CONSTRAINT = (
    "硬性输出约束（优先级最高）：去除原图中的叠加字幕、叠加标题和后期文字标签，"
    "不得新增这些覆盖文字。只清除后期叠加层，并用周围场景的自然纹理补全；"
    "最终采用的人物衣物、随身物品和最终场景本身的文字与 Logo 保持原样，"
    "不得抹除或改写。"
)

FirstFrameModel = Literal["gpt-image-2", "nano-banana-pro-2k"]


class FirstFrameSourceInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_count: int = Field(ge=0)
    notes: list[str]
    provider: str
    model: str


class FirstFrameCandidateInspection(BaseModel):
    """Semantic comparison of one generated first frame against its three roles."""

    model_config = ConfigDict(extra="forbid")

    person_count: int = Field(ge=0)
    identity_match_score: float = Field(ge=0, le=1)
    full_person_reconstruction_score: float = Field(ge=0, le=1)
    outfit_match_score: float = Field(ge=0, le=1)
    pose_preserved: bool
    framing_preserved: bool
    scene_preserved: bool
    anatomy_valid: bool
    head_only_replacement_detected: bool
    original_body_retained: bool
    text_detected: bool
    notes: list[str]
    provider: str
    model: str


class FirstFrameQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    attempt: int = Field(ge=1)
    issue_codes: list[str]
    inspection: FirstFrameCandidateInspection


class SceneContactSheetInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_count: int = Field(ge=0)
    identity_consistency_score: float = Field(ge=0, le=1)
    outfit_match_score: float = Field(ge=0, le=1)
    scene_match_score: float = Field(ge=0, le=1)
    anatomy_valid: bool
    text_detected: bool
    extra_people_detected: bool
    notes: list[str]
    provider: str
    model: str


class SceneContactSheetQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    attempt: int = Field(ge=1)
    issue_codes: list[str]
    inspection: SceneContactSheetInspection


class GeneratedVideoInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_count: int = Field(ge=0)
    identity_consistency_score: float = Field(ge=0, le=1)
    outfit_consistency_score: float = Field(ge=0, le=1)
    motion_continuity_score: float = Field(ge=0, le=1)
    anatomy_valid: bool
    extra_people_detected: bool
    severe_flicker_detected: bool
    notes: list[str]
    provider: str
    model: str


class GeneratedVideoQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    issue_codes: list[str]
    inspection: GeneratedVideoInspection


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    content_type: str
    quality: FirstFrameQualityResult | None = None
    stored_candidate: dict[str, object] | None = None
    quality_attempt: int | None = None


@dataclass(frozen=True)
class ImageInput:
    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class FirstFrameCharacterInputs:
    main_character_version_id: str
    character_snapshot: dict[str, object]
    reference_asset_ids: list[str]
    character_name: str
    authorized_project_ids: list[str]
    character_reference_selection_id: str | None = None
    character_version_id: str | None = None
    # Mirrors reference_asset_ids for audit trails: "contact_sheet" /
    # "source_photo" for simple-upload characters, "legacy_view" otherwise.
    reference_asset_roles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectAppearanceSpec:
    source_analysis_version_id: str | None
    source_timestamp_seconds: float | None
    category: str
    scene: str
    subject: str
    outfit_description: str
    selection_reason: str
    fingerprint: str
    appearance_source: Literal["VIDEO_ANALYSIS", "SCENE_LOOK"] = "VIDEO_ANALYSIS"
    scene_look_name: str | None = None
    scene_look_description: str | None = None
    scene_look_version_id: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROJECT_CHARACTER_APPEARANCE_SCHEMA_VERSION,
            "source_analysis_version_id": self.source_analysis_version_id,
            "source_timestamp_seconds": self.source_timestamp_seconds,
            "category": self.category,
            "scene": self.scene,
            "subject": self.subject,
            "outfit_description": self.outfit_description,
            "selection_reason": self.selection_reason,
            "fingerprint": self.fingerprint,
            "appearance_source": self.appearance_source,
            "scene_look_name": self.scene_look_name,
            "scene_look_description": self.scene_look_description,
            "scene_look_version_id": self.scene_look_version_id,
        }


@dataclass(frozen=True)
class FirstFrameGenerationWork:
    project_id: str
    actor: CurrentUser
    model: FirstFrameModel
    quantity: int
    source_frame_asset_id: str
    source_frame_selection_version_id: str
    character_inputs: FirstFrameCharacterInputs
    source_image: ImageInput
    reference_images: list[ImageInput]
    project_appearance: ProjectAppearanceSpec
    effective_prompt: str
    aspect_ratio: str | None = None
    replace_scene: bool = False


@dataclass(frozen=True)
class FirstFrameGenerationPlan:
    """Authorized DB snapshot that can be hydrated after releasing the fence."""

    project_id: str
    actor: CurrentUser
    model: FirstFrameModel
    quantity: int
    source_frame_asset_id: str
    source_frame_selection_version_id: str
    character_inputs: FirstFrameCharacterInputs
    source_asset: dict[str, object]
    reference_assets: list[dict[str, object]]
    project_appearance: ProjectAppearanceSpec
    effective_prompt: str
    aspect_ratio: str | None = None
    replace_scene: bool = False


@dataclass(frozen=True)
class StoredFirstFrameCandidates:
    candidates: list[dict[str, object]]
    created_assets: list[tuple[str, str]]


class ImageProvider(Protocol):
    provider_name: str

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
        aspect_ratio: str | None = None,
        size_override: str | None = None,
    ) -> list[GeneratedImage]: ...


class FirstFrameQualityInspector(Protocol):
    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection: ...

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection: ...

    def inspect_scene_contact_sheet(
        self,
        *,
        source_image: ImageInput,
        contact_sheet: GeneratedImage,
        scene_description: str,
        costume_description: str,
    ) -> SceneContactSheetInspection: ...

    def inspect_source_frame_candidates(
        self,
        frames: list[ExtractedSourceFrame],
    ) -> SourceFrameSemanticInspection: ...

    def inspect_generated_video(
        self,
        *,
        first_frame: ImageInput,
        sampled_frames: list[ImageInput],
    ) -> GeneratedVideoInspection: ...


class ImageProviderFailed(RuntimeError):
    pass


class RetryableImageProviderFailed(ImageProviderFailed):
    """可重试的供应商侧异常。

    ``rate_limited=True`` 表示上游以 429 明确拒绝且未受理本次请求——重试
    不会产生第二笔计费；其余（超时/连接中断/5xx）结果未知，是否重试必须
    由任务层依据回执状态裁决，不得据此标记直接重试。
    """

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


class FirstFrameQualityInspectorFailed(RuntimeError):
    pass


class FakeFirstFrameQualityInspector:
    """Local deterministic substitute; production never selects it implicitly."""

    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection:
        if not source_image.content:
            raise FirstFrameQualityInspectorFailed("source image is empty")
        return FirstFrameSourceInspection(
            person_count=1,
            notes=["本地测试质检"],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection:
        if (
            not source_image.content
            or not character_reference_images
            or not candidate.content
            or not expected_outfit
        ):
            raise FirstFrameQualityInspectorFailed("first-frame quality input is incomplete")
        return FirstFrameCandidateInspection(
            person_count=1,
            identity_match_score=0.95,
            full_person_reconstruction_score=0.95,
            outfit_match_score=0.95,
            pose_preserved=True,
            framing_preserved=True,
            scene_preserved=True,
            anatomy_valid=True,
            head_only_replacement_detected=False,
            original_body_retained=False,
            text_detected=False,
            notes=["本地测试质检"],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_scene_contact_sheet(
        self,
        *,
        source_image: ImageInput,
        contact_sheet: GeneratedImage,
        scene_description: str,
        costume_description: str,
    ) -> SceneContactSheetInspection:
        if (
            not source_image.content
            or not contact_sheet.content
            or not scene_description
            or not costume_description
        ):
            raise FirstFrameQualityInspectorFailed("scene contact-sheet input is incomplete")
        return SceneContactSheetInspection(
            view_count=5,
            identity_consistency_score=0.95,
            outfit_match_score=0.95,
            scene_match_score=0.95,
            anatomy_valid=True,
            text_detected=False,
            extra_people_detected=False,
            notes=["本地测试质检"],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_source_frame_candidates(
        self,
        frames: list[ExtractedSourceFrame],
    ) -> SourceFrameSemanticInspection:
        if not frames or any(not frame.image for frame in frames):
            raise FirstFrameQualityInspectorFailed("source-frame candidates are incomplete")
        assessments: list[SourceFrameCandidateAssessment] = []
        for index, frame in enumerate(frames):
            score = float(frame.technical_score) if frame.technical_score is not None else 0.5
            assessments.append(
                SourceFrameCandidateAssessment(
                    candidate_index=index,
                    person_count=1,
                    person_visibility_score=score,
                    face_clarity_score=score,
                    unobstructed_score=score,
                    pose_suitability_score=score,
                    motion_blur_detected=False,
                    notes=["本地测试质检"],
                )
            )
        return SourceFrameSemanticInspection(
            candidates=assessments,
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_generated_video(
        self,
        *,
        first_frame: ImageInput,
        sampled_frames: list[ImageInput],
    ) -> GeneratedVideoInspection:
        if not first_frame.content or len(sampled_frames) != 5:
            raise FirstFrameQualityInspectorFailed("generated-video quality input is invalid")
        return GeneratedVideoInspection(
            frame_count=5,
            identity_consistency_score=0.95,
            outfit_consistency_score=0.95,
            motion_continuity_score=0.9,
            anatomy_valid=True,
            extra_people_detected=False,
            severe_flicker_detected=False,
            notes=["本地测试质检"],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )


class FakeImageProvider:
    provider_name = "fake"

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
        aspect_ratio: str | None = None,
        size_override: str | None = None,
    ) -> list[GeneratedImage]:
        del model, prompt, character_reference_images, aspect_ratio, size_override
        return [
            GeneratedImage(content=source_image.content, content_type=source_image.content_type)
            for _ in range(output_count)
        ]


class ApilioTransport(Protocol):
    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]: ...

    def get(self, url: str) -> tuple[bytes, Mapping[str, str]]: ...

    def get_json(
        self, url: str, *, headers: Mapping[str, str]
    ) -> tuple[bytes, Mapping[str, str]]: ...


class UrllibApilioTransport:
    """Small stdlib transport so provider secrets never enter the client process."""

    # gpt-image edit calls are synchronous and regularly need 1-3 minutes
    # (five-view contact sheets sit at the high end). Keep the same 240s
    # per-call budget as the analysis provider so slow generations succeed
    # instead of falling back to the local placeholder.
    def __init__(self, *, timeout_seconds: float = 240.0) -> None:
        self.timeout_seconds = timeout_seconds

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        return self._open(Request(url, data=body, headers=dict(headers), method="POST"))

    def get_json(self, url: str, *, headers: Mapping[str, str]) -> tuple[bytes, Mapping[str, str]]:
        # Only called with a provider-origin URL, never an output/CDN URL.
        return self._open(Request(url, headers=dict(headers), method="GET"), timeout_seconds=60)

    def get(self, url: str) -> tuple[bytes, Mapping[str, str]]:
        timer = CallTimer()
        observation: dict[str, Any] = {}
        common = {
            "provider": "apilio",
            "endpoint": "images/output/download",
            "method": "GET",
            "url": url,
        }
        try:
            body, headers = self._get_output(url, observation)
        except (ImageProviderFailed, RetryableImageProviderFailed) as exc:
            cause = exc.__cause__ or exc
            record_external_call(
                **common,
                outcome="TIMEOUT"
                if isinstance(cause, TimeoutError)
                else "PROVIDER_ERROR"
                if observation.get("http_status") is not None
                else "NETWORK_ERROR",
                latency_ms=timer.elapsed_ms(),
                exception_type=type(cause).__name__,
                error_message=str(exc),
                **observation,
            )
            raise
        record_external_call(
            **common,
            outcome="SUCCEEDED",
            latency_ms=timer.elapsed_ms(),
            response_body=body,
            binary_response=True,
            **observation,
        )
        return body, headers

    def _get_output(self, url: str, observation: dict[str, Any]) -> tuple[bytes, Mapping[str, str]]:
        hostname, connect_ips = require_safe_provider_download_url(url)
        parsed = urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        connection = None
        last_error: OSError | None = None
        deadline = time.monotonic() + self.timeout_seconds
        for index, connect_ip in enumerate(connect_ips):
            remaining = self.timeout_seconds if index == 0 else deadline - time.monotonic()
            if remaining <= 0:
                break
            candidate = _pinned_connection("https", hostname, 443, connect_ip, remaining)
            try:
                candidate.connect()
            except ViralMediaError as exc:
                candidate.close()
                raise ImageProviderFailed(
                    "Apilio image connection address was not verified"
                ) from exc
            except OSError as exc:
                candidate.close()
                last_error = exc
                continue
            connection = candidate
            break
        if connection is None:
            raise RetryableImageProviderFailed("Apilio image request failed") from last_error
        response = None
        try:
            # Connect to the validated IP while retaining hostname SNI and Host.
            # Direct http.client connections also ignore environment HTTP proxies.
            connection.request(
                "GET",
                target,
                headers={
                    "Host": f"[{hostname}]" if ":" in hostname else hostname,
                    "User-Agent": APILIO_OUTPUT_USER_AGENT,
                },
            )
            response = connection.getresponse()
            observation.update(
                http_status=response.status, response_headers=dict(response.headers.items())
            )
            if not 200 <= response.status < 300:
                if response.status >= 400:
                    observation["response_body"] = response.read(MAX_PROVIDER_IMAGE_BYTES + 1)
                if response.status == 429 or response.status >= 500:
                    # 下载段是只读取回已生成的产出图——付费生成此时已完成并
                    # 计费，重发提交会产生第二笔费用，因此绝不打 rate_limited
                    # 标记（保持"结果未知"，由任务层保守处理）。
                    raise RetryableImageProviderFailed(f"Apilio returned HTTP {response.status}")
                raise ImageProviderFailed(f"Apilio returned HTTP {response.status}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ImageProviderFailed("Apilio returned an invalid image size") from exc
                if declared_size < 0 or declared_size > MAX_PROVIDER_IMAGE_BYTES:
                    raise ImageProviderFailed("Apilio response exceeds the image size limit")
            body = response.read(MAX_PROVIDER_IMAGE_BYTES + 1)
            if len(body) > MAX_PROVIDER_IMAGE_BYTES:
                raise ImageProviderFailed("Apilio response exceeds the image size limit")
            return body, dict(response.headers.items())
        except ViralMediaError as exc:
            raise ImageProviderFailed("Apilio image connection address was not verified") from exc
        except (TimeoutError, OSError) as exc:
            raise RetryableImageProviderFailed("Apilio image request failed") from exc
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _open(
        self, request: Request, *, timeout_seconds: float | None = None
    ) -> tuple[bytes, Mapping[str, str]]:
        try:
            opener = build_opener(NoRedirectHandler())
            # 记录每次调用的原始响应，图片失败原因可在管理端查到（方案 P0-9）。
            body, headers, _status = recorded_urlopen(
                request,
                expected_json=request.get_method() == "POST",
                timeout=timeout_seconds or self.timeout_seconds,
                provider="apilio",
                endpoint=endpoint_from_url(request.full_url),
                opener=opener.open,
                read_limit=MAX_PROVIDER_IMAGE_BYTES + 1,
            )
            if len(body) > MAX_PROVIDER_IMAGE_BYTES:
                raise ImageProviderFailed("Apilio response exceeds the image size limit")
            return body, headers
        except HTTPError as exc:
            logger.warning("Apilio image request failed with HTTP status %s", exc.code)
            if exc.code == 429 or exc.code >= 500:
                raise RetryableImageProviderFailed(
                    f"Apilio returned HTTP {exc.code}",
                    rate_limited=exc.code == 429,
                )
            raise ImageProviderFailed(f"Apilio returned HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Apilio image request failed: %s", type(exc).__name__)
            raise RetryableImageProviderFailed("Apilio image request failed") from exc


class ApilioImageProvider:
    provider_name = "apilio"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        transport: ApilioTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibApilioTransport()

    @property
    def account_fingerprint(self) -> str:
        return hashlib.sha256(f"{self.base_url}\n{self.api_key}".encode()).hexdigest()

    def submit_edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
        aspect_ratio: str | None = None,
        size_override: str | None = None,
    ) -> str:
        body, content_type = build_apilio_edit_multipart(
            model=model,
            prompt=prompt,
            source_image=source_image,
            character_reference_images=character_reference_images,
            output_count=output_count,
            aspect_ratio=aspect_ratio,
            size_override=size_override,
        )
        raw, _ = self.transport.post(
            f"{self.base_url}{APILIO_IMAGE_EDIT_PATH}?async=true",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            body=body,
        )
        payload = self._json_object(raw)
        return self._task_id(payload.get("task_id"))

    @staticmethod
    def _json_object(raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageProviderFailed("Apilio returned invalid task JSON") from exc
        if not isinstance(payload, dict):
            raise ImageProviderFailed("Apilio returned invalid task JSON")
        return payload

    @staticmethod
    def _task_id(value: object) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) is None:
            raise ImageProviderFailed("Apilio returned an invalid task ID")
        return value

    def poll_edit(self, task_id: str, *, output_count: int) -> list[GeneratedImage] | None:
        task_id = self._task_id(task_id)
        raw, _ = self.transport.get_json(
            f"{self.base_url}/v1/images/tasks/{task_id}",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        payload = self._json_object(raw)
        data = payload.get("data")
        if payload.get("code") != "success" or not isinstance(data, dict):
            raise ImageProviderFailed("Apilio returned invalid task status")
        if data.get("task_id") != task_id:
            raise ImageProviderFailed("Apilio returned a different task")
        status = data.get("status")
        if status == "FAILURE":
            # Do not echo vendor text: it can contain signed URLs or credentials.
            raise first_frame_error(
                422, "FIRST_FRAME_PROVIDER_REJECTED", "图像服务生成失败，请调整素材后重试。"
            )
        if status == "SUCCESS":
            return self._parse_response(
                json.dumps(data.get("data")).encode(), output_count=output_count
            )
        if status not in {"NOT_START", "SUBMITTED", "IN_PROGRESS"}:
            raise ImageProviderFailed("Apilio returned an unknown task status")
        return None

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
        aspect_ratio: str | None = None,
        size_override: str | None = None,
    ) -> list[GeneratedImage]:
        body, content_type = build_apilio_edit_multipart(
            model=model,
            prompt=prompt,
            source_image=source_image,
            character_reference_images=character_reference_images,
            output_count=output_count,
            aspect_ratio=aspect_ratio,
            size_override=size_override,
        )
        raw_body, _ = self.transport.post(
            f"{self.base_url}{APILIO_IMAGE_EDIT_PATH}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            body=body,
        )
        return self._parse_response(raw_body, output_count=output_count)

    def _parse_response(self, raw_body: bytes, *, output_count: int) -> list[GeneratedImage]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageProviderFailed("Apilio returned invalid JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ImageProviderFailed("Apilio response is missing image output")
        if len(data) != output_count:
            raise ImageProviderFailed("Apilio returned an unexpected number of image outputs")
        return [self._parse_image(item) for item in data]

    def _parse_image(self, item: object) -> GeneratedImage:
        if not isinstance(item, dict):
            raise ImageProviderFailed("Apilio response is missing image output")
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            content_type = normalized_image_content_type(item.get("mime_type"))
            if encoded.startswith("data:"):
                header, separator, data = encoded.partition(",")
                allowed_headers = {
                    f"data:{mime};base64": mime for mime in FIRST_FRAME_IMAGE_CONTENT_TYPES
                }
                if not separator or header not in allowed_headers:
                    raise ImageProviderFailed("Apilio returned an unsupported image data URI")
                content_type = allowed_headers[header]
                encoded = data
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ImageProviderFailed("Apilio returned invalid base64 image data") from exc
            validate_provider_image_bytes(content, content_type)
            return GeneratedImage(content=content, content_type=content_type)
        url = item.get("url")
        if not isinstance(url, str) or not valid_provider_output_url(url):
            raise ImageProviderFailed("Apilio response is missing image output")
        content, headers = self.transport.get(url)
        if not content:
            raise ImageProviderFailed("Apilio returned an empty image output")
        content_type = normalized_image_content_type(header_value(headers, "content-type"))
        validate_provider_image_bytes(content, content_type)
        return GeneratedImage(
            content=content,
            content_type=content_type,
        )


class ApilioFirstFrameQualityInspector:
    """Fail-closed Gemini comparison for the single-person first-frame lane."""

    provider_name = "apilio_gemini"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        model: str = APILIO_GEMINI_MODEL,
        transport: ApilioTransport | None = None,
        max_attempts: int = 2,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport or UrllibApilioTransport()
        self.max_attempts = max(1, max_attempts)

    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection:
        _validate_quality_images((source_image.content,))
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "这是准备进行人物重构的原视频源画面。只返回 JSON 对象，字段必须严格为："
                    "person_count（整数）、notes（中文短句数组）。统计画面中不同的真实人物，"
                    "包括局部露出的人；同一个人的镜面反射不重复计数，海报、屏幕和照片中的人物不计数。"
                ),
            },
            _chat_image_item(source_image.content, source_image.content_type),
        ]
        payload = self._chat_json(content)
        payload["provider"] = "apilio_gemini"
        payload["model"] = self.model
        try:
            return FirstFrameSourceInspection.model_validate(payload)
        except ValidationError as exc:
            raise FirstFrameQualityInspectorFailed(
                "first-frame source inspection response was invalid"
            ) from exc

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection:
        _validate_quality_images(
            (
                source_image.content,
                *(reference.content for reference in character_reference_images),
                candidate.content,
            )
        )
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "你是人物重构首帧的严格视觉质检器。接下来依次给出原视频源画面、"
                    "角色身份参考图和生成候选图。只返回 JSON 对象，不要 markdown。"
                    "必须严格包含：person_count（整数）、identity_match_score、"
                    "full_person_reconstruction_score、outfit_match_score（三项均为0到1）、"
                    "pose_preserved、framing_preserved、scene_preserved、anatomy_valid、"
                    "head_only_replacement_detected、original_body_retained、text_detected（布尔）、"
                    "notes（中文短句数组）。full_person_reconstruction_score 要评价候选图中"
                    "所有可见的"
                    "头脸、发型、颈部、肤色、身形、上下装、鞋履、手部和连接部位是否属于同一目标人物；"
                    "只换脸或只覆盖头部必须低分。identity_match_score 以角色参考图为准；"
                    "姿态、构图、"
                    "场景以源画面为准。候选服装要求为："
                    f"{expected_outfit}"
                ),
            },
            {"type": "text", "text": "原视频源画面："},
            _chat_image_item(source_image.content, source_image.content_type),
        ]
        for index, reference in enumerate(character_reference_images, start=1):
            content.extend(
                [
                    {"type": "text", "text": f"角色身份参考图 {index}："},
                    _chat_image_item(reference.content, reference.content_type),
                ]
            )
        content.extend(
            [
                {"type": "text", "text": "待质检的生成候选图："},
                _chat_image_item(candidate.content, candidate.content_type),
            ]
        )
        payload = self._chat_json(content)
        payload["provider"] = "apilio_gemini"
        payload["model"] = self.model
        try:
            return FirstFrameCandidateInspection.model_validate(payload)
        except ValidationError as exc:
            raise FirstFrameQualityInspectorFailed(
                "first-frame candidate inspection response was invalid"
            ) from exc

    def inspect_scene_contact_sheet(
        self,
        *,
        source_image: ImageInput,
        contact_sheet: GeneratedImage,
        scene_description: str,
        costume_description: str,
    ) -> SceneContactSheetInspection:
        _validate_quality_images((source_image.content, contact_sheet.content))
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "你是人物场景五视图的严格视觉质检器。依次给出人物原始授权照片和生成的五视图参考板。"
                    "只返回 JSON 对象，必须严格包含：view_count（参考板中人物视图数量）、"
                    "identity_consistency_score、outfit_match_score、scene_match_score（三项均为0到1）、"
                    "anatomy_valid、text_detected、extra_people_detected（布尔）、notes（中文短句数组）。"
                    "五个视角必须是同一个人，身份以原始照片为准；服装要求为："
                    f"{costume_description}；场景要求为：{scene_description}。"
                    "检查脸型、年龄、性别、发型、服装、配饰、手脚和肢体连接是否一致自然，"
                    "不得包含额外人物、文字、水印或重复肢体。"
                ),
            },
            {"type": "text", "text": "人物原始授权照片："},
            _chat_image_item(source_image.content, source_image.content_type),
            {"type": "text", "text": "待质检的场景五视图参考板："},
            _chat_image_item(contact_sheet.content, contact_sheet.content_type),
        ]
        payload = self._chat_json(content)
        payload["provider"] = "apilio_gemini"
        payload["model"] = self.model
        try:
            return SceneContactSheetInspection.model_validate(payload)
        except ValidationError as exc:
            raise FirstFrameQualityInspectorFailed(
                "scene contact-sheet inspection response was invalid"
            ) from exc

    def inspect_source_frame_candidates(
        self,
        frames: list[ExtractedSourceFrame],
    ) -> SourceFrameSemanticInspection:
        if not frames:
            raise FirstFrameQualityInspectorFailed("source-frame candidates are empty")
        _validate_quality_images(tuple(frame.image for frame in frames))
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "你是替换视频首帧的候选画面质检器。后续图片按 candidate_index 从 0 开始编号。"
                    "只返回 JSON 对象，根字段必须严格为 candidates。每个候选必须包含："
                    "candidate_index、person_count、person_visibility_score、face_clarity_score、"
                    "unobstructed_score、pose_suitability_score、motion_blur_detected、notes。"
                    "四项分数均为0到1。优先选择人物完整、脸部清晰、无遮挡、姿态自然、"
                    "没有运动模糊并且适合进行整个人物重构的画面。"
                    "海报、屏幕和照片中的人物不计入 person_count。"
                ),
            }
        ]
        for index, frame in enumerate(frames):
            content.extend(
                [
                    {"type": "text", "text": f"candidate_index={index}"},
                    _chat_image_item(frame.image, "image/jpeg"),
                ]
            )
        payload = self._chat_json(content)
        payload["provider"] = "apilio_gemini"
        payload["model"] = self.model
        try:
            return SourceFrameSemanticInspection.model_validate(payload)
        except ValidationError as exc:
            raise FirstFrameQualityInspectorFailed(
                "source-frame semantic inspection response was invalid"
            ) from exc

    def inspect_generated_video(
        self,
        *,
        first_frame: ImageInput,
        sampled_frames: list[ImageInput],
    ) -> GeneratedVideoInspection:
        if len(sampled_frames) != 5:
            raise FirstFrameQualityInspectorFailed("generated-video sample count is invalid")
        _validate_quality_images(
            (first_frame.content, *(frame.content for frame in sampled_frames))
        )
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "你是图生视频成片的严格视觉质检器。第一张图是已确认的首帧，后续五张图按时间顺序"
                    "来自生成视频。只返回 JSON 对象，必须严格包含：frame_count、"
                    "identity_consistency_score、outfit_consistency_score、motion_continuity_score"
                    "（三项均为0到1）、anatomy_valid、extra_people_detected、"
                    "severe_flicker_detected（布尔）、notes（中文短句数组）。"
                    "身份、脸部、发型、身形、服装、鞋履和配饰均以首帧为准；检查五个时间点是否"
                    "出现身份或服装漂移、额外人物、肢体异常、严重闪烁或不连续运动。"
                ),
            },
            {"type": "text", "text": "已确认首帧："},
            _chat_image_item(first_frame.content, first_frame.content_type),
        ]
        for index, frame in enumerate(sampled_frames, start=1):
            content.extend(
                [
                    {"type": "text", "text": f"生成视频采样帧 {index}："},
                    _chat_image_item(frame.content, frame.content_type),
                ]
            )
        payload = self._chat_json(content)
        payload["provider"] = "apilio_gemini"
        payload["model"] = self.model
        try:
            return GeneratedVideoInspection.model_validate(payload)
        except ValidationError as exc:
            raise FirstFrameQualityInspectorFailed(
                "generated-video inspection response was invalid"
            ) from exc

    def _chat_json(self, content: list[dict[str, object]]) -> dict[str, object]:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": content}],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                from app.billing_meter import meter_call

                with meter_call("quality_inspection"):
                    raw_body, _ = self.transport.post(
                        f"{self.base_url}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        body=body,
                    )
                response = json.loads(raw_body.decode("utf-8"))
                raw_content = response["choices"][0]["message"]["content"]
                payload = json.loads(raw_content)
                if not isinstance(payload, dict):
                    raise TypeError("quality response must be an object")
                return cast(dict[str, object], payload)
            except RetryableImageProviderFailed as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    continue
            except (
                ImageProviderFailed,
                IndexError,
                KeyError,
                TypeError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
            break
        raise FirstFrameQualityInspectorFailed("first-frame quality request failed") from last_error


def bounded_first_frame_quality_inspector(
    inspector: FirstFrameQualityInspector,
) -> FirstFrameQualityInspector:
    """Use a separate budget for first-frame checks, without multiplying retries."""
    if not isinstance(inspector, ApilioFirstFrameQualityInspector):
        return inspector
    return ApilioFirstFrameQualityInspector(
        api_key=inspector.api_key,
        base_url=inspector.base_url,
        model=inspector.model,
        transport=UrllibApilioTransport(timeout_seconds=60.0),
        max_attempts=1,
    )


def bounded_source_frame_quality_inspector(
    inspector: FirstFrameQualityInspector,
) -> FirstFrameQualityInspector:
    """Keep optional source-frame scoring from blocking the required local extraction."""

    if not isinstance(inspector, ApilioFirstFrameQualityInspector):
        return inspector
    return ApilioFirstFrameQualityInspector(
        api_key=inspector.api_key,
        base_url=inspector.base_url,
        model=inspector.model,
        transport=UrllibApilioTransport(
            timeout_seconds=SOURCE_FRAME_QUALITY_TIMEOUT_SECONDS,
        ),
        max_attempts=1,
    )


def _chat_image_item(content: bytes, content_type: str) -> dict[str, object]:
    data_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": data_url}}


def _validate_quality_images(images: tuple[bytes, ...]) -> None:
    if any(len(image) > MAX_QUALITY_IMAGE_BYTES for image in images):
        raise FirstFrameQualityInspectorFailed("first-frame quality image exceeds size limit")
    if sum(len(image) for image in images) > MAX_QUALITY_REQUEST_IMAGE_BYTES:
        raise FirstFrameQualityInspectorFailed("first-frame quality request exceeds size limit")


def build_apilio_edit_multipart(
    *,
    model: FirstFrameModel,
    prompt: str,
    source_image: ImageInput,
    character_reference_images: list[ImageInput],
    output_count: int,
    aspect_ratio: str | None = None,
    size_override: str | None = None,
) -> tuple[bytes, str]:
    boundary = f"----video-replica-{uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    def add_image(image: ImageInput) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="image"; '
                f'filename="{safe_filename(image.filename)}"\r\n'
            ).encode()
        )
        body.extend(f"Content-Type: {image.content_type}\r\n\r\n".encode())
        body.extend(image.content)
        body.extend(b"\r\n")

    add_field("model", model)
    add_field("prompt", prompt)
    add_image(source_image)
    for image in character_reference_images:
        add_image(image)
    # Prefer inline output, while still accepting Apilio's URL fallback below.
    # Some compatible gateways ignore this field and return a hosted image.
    add_field("response_format", "b64_json")
    add_field("n", str(output_count))
    if model == "gpt-image-2":
        if aspect_ratio is not None and aspect_ratio not in FIRST_FRAME_IMAGE_SIZES:
            raise ImageProviderFailed("Unsupported image aspect ratio")

        # size 是 OpenAI Images Edit 协议的尺寸字段，也是 gpt-image-2 唯一的尺寸
        # 入口；2K 由 FIRST_FRAME_IMAGE_SIZES 的档位值本身兑现（见该常量注释）。
        # size_override 供"同一宽高比但需要另一种整图尺寸"的调用方覆盖档位：五视图
        # 复合排版就属此类，见 simple_character.CONTACT_SHEET_SIZE。
        add_field(
            "size",
            size_override or (FIRST_FRAME_IMAGE_SIZES[aspect_ratio] if aspect_ratio else "auto"),
        )
    else:
        add_field("aspect_ratio", aspect_ratio or image_aspect_ratio(source_image))
        add_field("image_size", "2K")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def image_aspect_ratio(image: ImageInput) -> str:
    dimensions = png_dimensions(image.content) or jpeg_dimensions(image.content)
    if dimensions is None:
        return "9:16"
    width, height = dimensions
    target_ratio = width / height
    supported_ratios = {
        "1:1": 1.0,
        "2:3": 2 / 3,
        "3:2": 3 / 2,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "4:5": 4 / 5,
        "5:4": 5 / 4,
        "9:16": 9 / 16,
        "16:9": 16 / 9,
        "21:9": 21 / 9,
    }
    return min(supported_ratios, key=lambda ratio: abs(supported_ratios[ratio] - target_ratio))


def png_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        return None
    return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")


def jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 <= len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(content):
            return None
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 7 or offset + segment_length > len(content):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def safe_filename(filename: str) -> str:
    return "".join(char if char.isalnum() or char in {".", "-", "_"} else "_" for char in filename)


def valid_provider_output_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def require_safe_provider_download_url(value: str) -> tuple[str, tuple[str, ...]]:
    if not valid_provider_output_url(value):
        raise ImageProviderFailed("Apilio output URL must use HTTPS")
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ImageProviderFailed("Apilio output URL is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ImageProviderFailed("Apilio output URL port is invalid") from exc
    if parsed.username is not None or parsed.password is not None or port not in {None, 443}:
        raise ImageProviderFailed("Apilio output URL credentials or port are not allowed")
    hostname = hostname.encode("idna").decode("ascii")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImageProviderFailed("Apilio output URL hostname could not be resolved") from exc
    if not addresses:
        raise ImageProviderFailed("Apilio output URL hostname could not be resolved")
    trusted_output_host = hostname.lower() in APILIO_OUTPUT_HOSTS
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        proxy_fake_ip = trusted_output_host and ip in FAKE_IP_NETWORK
        if not ip.is_global and not proxy_fake_ip:
            raise ImageProviderFailed("Apilio output URL must resolve to a public address")
    return hostname, tuple(dict.fromkeys(str(address[4][0]) for address in addresses))


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    name_lower = name.lower()
    return next((value for key, value in headers.items() if key.lower() == name_lower), None)


def normalized_image_content_type(value: object) -> str:
    content_type = str(value or "image/png").split(";", 1)[0].lower().strip()
    if content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise ImageProviderFailed("Apilio returned an unsupported image type")
    return content_type


def validate_provider_image_bytes(content: bytes, content_type: str) -> None:
    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    if not signatures[content_type]:
        raise ImageProviderFailed("Apilio returned image bytes that do not match its content type")


PROJECT_APPEARANCE_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "CONSTRUCTION",
        ("工地", "施工", "工程", "建筑", "项目现场"),
        "符合施工与工程现场的整洁专业工装：纯色工装外套或耐磨长袖上衣、工装长裤、封闭式低帮鞋；无品牌、无文字、不过度宽松。安全帽等防护用品只在源画面本来存在时保留，不凭空新增。",
    ),
    (
        "BUSINESS",
        ("商务", "会议", "办公室", "企业", "客户", "销售", "合作"),
        "符合商务沟通场景的简洁商务休闲装：低饱和纯色上装、利落长裤与简洁鞋履；无品牌、无文字，版型自然且便于动作。",
    ),
    (
        "DINING",
        ("餐厅", "探店", "美食", "厨房", "咖啡"),
        "符合餐饮与探店场景的干净生活化穿搭：简洁纯色上装、日常长裤与低调鞋履；无品牌、无文字，不喧宾夺主。",
    ),
    (
        "SPORT",
        ("运动", "健身", "跑步", "球场", "训练"),
        "符合运动场景的功能性休闲服：合身运动上装、运动长裤与轻便运动鞋；无品牌、无文字，保证肢体活动自然。",
    ),
    (
        "OUTDOOR",
        ("户外", "街道", "公园", "旅行", "山", "海边"),
        "符合户外环境的轻便层次穿搭：纯色外搭或上装、耐用长裤与舒适鞋履；无品牌、无文字，并与天气和光线协调。",
    ),
    (
        "LIFESTYLE",
        ("居家", "客厅", "卧室", "生活", "日常"),
        "符合日常生活场景的自然休闲穿搭：柔和纯色上装、简洁长裤与低调鞋履；无品牌、无文字，避免影楼感。",
    ),
)
DEFAULT_PROJECT_OUTFIT = (
    "依据源画面场景生成自然、完整、无品牌且无文字的中性日常服装；"
    "上装、下装与鞋履必须成套并符合人物动作，颜色与场景光线协调。"
)


def derive_project_appearance_spec(
    *,
    analysis_payload: Mapping[str, Any],
    source_analysis_version_id: str | None,
    source_timestamp_seconds: float | None,
) -> ProjectAppearanceSpec:
    """Derive one deterministic project/scene appearance without another paid call.

    The selected source-frame timestamp chooses the matching analysis segment.
    This keeps the base character responsible for identity only while the
    project appearance controls clothing and scene fit.
    """

    shots = analysis_payload.get("shots")
    selected_shot: Mapping[str, Any] | None = None
    if isinstance(shots, list):
        valid_shots = [shot for shot in shots if isinstance(shot, Mapping)]
        if source_timestamp_seconds is not None:
            for index, shot in enumerate(valid_shots):
                start = _appearance_number(shot.get("start_time"))
                end = _appearance_number(shot.get("end_time"))
                if (
                    start is not None
                    and end is not None
                    and start <= source_timestamp_seconds
                    and (
                        source_timestamp_seconds < end
                        or (index == len(valid_shots) - 1 and source_timestamp_seconds <= end)
                    )
                ):
                    selected_shot = shot
                    break
        if selected_shot is None and valid_shots:
            selected_shot = valid_shots[0]

    scene = _appearance_text(selected_shot, "scene") or "当前源画面场景"
    subject = _appearance_text(selected_shot, "subject") or "主讲人物"
    action = _appearance_text(selected_shot, "action")
    theme = str(analysis_payload.get("theme") or "").strip()
    visual_style = str(analysis_payload.get("visual_style") or "").strip()
    category = "GENERAL"
    outfit_description = DEFAULT_PROJECT_OUTFIT
    selected_segment_corpus = " ".join(value for value in (scene, subject, action) if value)
    project_corpus = " ".join(value for value in (theme, visual_style) if value)
    for corpus in (selected_segment_corpus, project_corpus):
        matched = next(
            (
                (rule_category, rule_outfit)
                for rule_category, keywords, rule_outfit in PROJECT_APPEARANCE_RULES
                if any(keyword in corpus for keyword in keywords)
            ),
            None,
        )
        if matched is not None:
            category, outfit_description = matched
            break

    reason_parts = [f"源画面场景“{scene}”"]
    if subject:
        reason_parts.append(f"人物身份“{subject}”")
    if action:
        reason_parts.append(f"动作“{action}”")
    selection_reason = "；".join(reason_parts) + "；由后台自动匹配项目人物造型。"
    fingerprint_source = {
        "schema_version": PROJECT_CHARACTER_APPEARANCE_SCHEMA_VERSION,
        "replacement_contract_version": FIRST_FRAME_REPLACEMENT_CONTRACT_VERSION,
        "source_analysis_version_id": source_analysis_version_id,
        "source_timestamp_seconds": source_timestamp_seconds,
        "category": category,
        "scene": scene,
        "subject": subject,
        "outfit_description": outfit_description,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProjectAppearanceSpec(
        source_analysis_version_id=source_analysis_version_id,
        source_timestamp_seconds=source_timestamp_seconds,
        category=category,
        scene=scene,
        subject=subject,
        outfit_description=outfit_description,
        selection_reason=selection_reason,
        fingerprint=fingerprint,
    )


def apply_selected_scene_look(
    appearance: ProjectAppearanceSpec,
    *,
    character_inputs: FirstFrameCharacterInputs,
) -> ProjectAppearanceSpec:
    return _apply_scene_look_snapshot(
        appearance,
        character_snapshot=character_inputs.character_snapshot,
        character_version_id=character_inputs.character_version_id,
    )


def _apply_scene_look_snapshot(
    appearance: ProjectAppearanceSpec,
    *,
    character_snapshot: Mapping[str, object],
    character_version_id: str | None,
) -> ProjectAppearanceSpec:
    persona = character_snapshot.get("persona_snapshot_json")
    if not isinstance(persona, Mapping):
        return appearance
    constraints = persona.get("appearance_constraints_json")
    if not isinstance(constraints, Mapping) or constraints.get("appearance_type") != "scene":
        return appearance
    name = str(persona.get("name") or "").strip()
    scene_description = str(persona.get("scene_description") or "").strip()
    costume_description = str(persona.get("costume_description") or "").strip()
    if not name or not character_version_id:
        raise stale_first_frame_inputs()
    costume_description = costume_description or "完整沿用所选场景形象图片中的外观、服饰与配饰"
    fingerprint_source = {
        "schema_version": PROJECT_CHARACTER_APPEARANCE_SCHEMA_VERSION,
        "source_analysis_version_id": appearance.source_analysis_version_id,
        "source_timestamp_seconds": appearance.source_timestamp_seconds,
        "source_scene": appearance.scene,
        "subject": appearance.subject,
        "appearance_source": "SCENE_LOOK",
        "review_mode": "HUMAN_CONFIRMATION",
        "replacement_contract_version": FIRST_FRAME_REPLACEMENT_CONTRACT_VERSION,
        "scene_look_name": name,
        "scene_look_description": scene_description,
        "scene_look_version_id": character_version_id,
        "outfit_description": costume_description,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return replace(
        appearance,
        category="SCENE_LOOK",
        outfit_description=costume_description,
        selection_reason=(
            f"用户已选择场景造型“{name}”；服装与配饰以该版本为准，"
            f"原视频场景“{appearance.scene}”仍作为构图和背景模板。"
        ),
        fingerprint=fingerprint,
        appearance_source="SCENE_LOOK",
        scene_look_name=name,
        scene_look_description=scene_description,
        scene_look_version_id=character_version_id,
    )


def resolve_project_appearance_spec(
    conn: BusinessConnection,
    *,
    project_id: str,
    source_timestamp_seconds: float | None,
) -> ProjectAppearanceSpec:
    analysis_version = latest_version(conn, project_id, "analysis")
    analysis_payload: Mapping[str, Any] = {}
    analysis_version_id: str | None = None
    if analysis_version is not None:
        try:
            version_payload = json.loads(str(analysis_version["payload_json"]))
        except json.JSONDecodeError:
            version_payload = {}
        nested_analysis = (
            version_payload.get("analysis") if isinstance(version_payload, dict) else None
        )
        if isinstance(nested_analysis, Mapping):
            analysis_payload = nested_analysis
            analysis_version_id = str(analysis_version["id"])
    return derive_project_appearance_spec(
        analysis_payload=analysis_payload,
        source_analysis_version_id=analysis_version_id,
        source_timestamp_seconds=source_timestamp_seconds,
    )


def _appearance_text(shot: Mapping[str, Any] | None, key: str) -> str:
    if shot is None:
        return ""
    value = shot.get(key)
    return value.strip() if isinstance(value, str) else ""


def _appearance_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def require_readable_video_analysis(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> None:
    """Refuse damaged analysis while keeping readable legacy rows usable."""

    analysis_version = latest_version(conn, project_id, "analysis")
    if analysis_version is None:
        return
    try:
        payload = json.loads(str(analysis_version["payload_json"]))
    except json.JSONDecodeError:
        raise first_frame_error(
            409,
            "VIDEO_ANALYSIS_UPGRADE_REQUIRED",
            "当前拆解结果版本过旧或已损坏，请先重新拆解视频。",
        )
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    shots = analysis.get("shots") if isinstance(analysis, dict) else None
    if not isinstance(shots, list) or not shots:
        raise first_frame_error(
            409,
            "VIDEO_ANALYSIS_UPGRADE_REQUIRED",
            "当前拆解结果结构不完整，请先重新拆解视频。",
        )
    for shot in shots:
        if not isinstance(shot, dict):
            raise first_frame_error(
                409,
                "VIDEO_ANALYSIS_UPGRADE_REQUIRED",
                "当前拆解结果结构不完整，请先重新拆解视频。",
            )


def evaluate_first_frame_candidate_quality(
    inspection: FirstFrameCandidateInspection,
    *,
    attempt: int,
) -> FirstFrameQualityResult:
    issues: list[str] = []
    if inspection.person_count != 1:
        issues.append("PERSON_COUNT_INVALID")
    if inspection.identity_match_score < MIN_FIRST_FRAME_IDENTITY_SCORE:
        issues.append("IDENTITY_MISMATCH")
    if inspection.full_person_reconstruction_score < MIN_FIRST_FRAME_RECONSTRUCTION_SCORE:
        issues.append("FULL_PERSON_RECONSTRUCTION_INCOMPLETE")
    if inspection.outfit_match_score < MIN_FIRST_FRAME_OUTFIT_SCORE:
        issues.append("OUTFIT_MISMATCH")
    if not inspection.pose_preserved:
        issues.append("POSE_CHANGED")
    if not inspection.framing_preserved:
        issues.append("FRAMING_CHANGED")
    if not inspection.scene_preserved:
        issues.append("SCENE_CHANGED")
    if not inspection.anatomy_valid:
        issues.append("ANATOMY_INVALID")
    if inspection.head_only_replacement_detected:
        issues.append("HEAD_ONLY_REPLACEMENT")
    if inspection.original_body_retained:
        issues.append("ORIGINAL_BODY_RETAINED")
    if inspection.text_detected:
        issues.append("TEXT_DETECTED")
    return FirstFrameQualityResult(
        passed=not issues,
        attempt=attempt,
        issue_codes=issues,
        inspection=inspection,
    )


def quality_retry_prompt(base_prompt: str, issue_codes: list[str], attempt: int) -> str:
    if attempt == 1 or not issue_codes:
        return base_prompt
    guidance = {
        "PERSON_COUNT_INVALID": "只保留源画面中的一名人物，不得新增、复制或删除主体。",
        "IDENTITY_MISMATCH": "人物身份必须严格遵循角色参考图，修正脸型、五官和发型漂移。",
        "FULL_PERSON_RECONSTRUCTION_INCOMPLETE": (
            "重新生成所有可见人物区域，确保头、颈、身体、服装、手脚属于同一人物。"
        ),
        "OUTFIT_MISMATCH": "服装必须完整符合项目场景造型，不得保留原人物服装。",
        "POSE_CHANGED": "严格恢复源画面人物姿态和动作。",
        "FRAMING_CHANGED": "严格恢复源画面构图、人物位置和画面比例。",
        "SCENE_CHANGED": "严格恢复源画面场景、道具、光线和色调。",
        "ANATOMY_INVALID": "修正手指、四肢、颈部和身体连接，保证真实人体结构。",
        "HEAD_ONLY_REPLACEMENT": "禁止局部换脸或只覆盖头部，必须完整重构所有可见人物区域。",
        "ORIGINAL_BODY_RETAINED": "不得保留原视频人物的身体、肤色或服装。",
        "TEXT_DETECTED": "移除所有文字、水印、Logo、数字和符号。",
    }
    unique_codes = list(dict.fromkeys(issue_codes))
    corrections = "\n".join(f"- {guidance[code]}" for code in unique_codes if code in guidance)
    return f"{base_prompt}\n\n自动质检未通过，第 {attempt} 次生成必须修正以下问题：\n{corrections}"


def evaluate_scene_contact_sheet_quality(
    inspection: SceneContactSheetInspection,
    *,
    attempt: int,
) -> SceneContactSheetQualityResult:
    issues: list[str] = []
    if inspection.view_count != 5:
        issues.append("VIEW_COUNT_INVALID")
    if inspection.identity_consistency_score < MIN_SCENE_CONTACT_SHEET_IDENTITY_SCORE:
        issues.append("IDENTITY_INCONSISTENT")
    if inspection.outfit_match_score < MIN_SCENE_CONTACT_SHEET_OUTFIT_SCORE:
        issues.append("OUTFIT_MISMATCH")
    if inspection.scene_match_score < MIN_SCENE_CONTACT_SHEET_SCENE_SCORE:
        issues.append("SCENE_MISMATCH")
    if not inspection.anatomy_valid:
        issues.append("ANATOMY_INVALID")
    if inspection.text_detected:
        issues.append("TEXT_DETECTED")
    if inspection.extra_people_detected:
        issues.append("EXTRA_PEOPLE_DETECTED")
    return SceneContactSheetQualityResult(
        passed=not issues,
        attempt=attempt,
        issue_codes=issues,
        inspection=inspection,
    )


def evaluate_generated_video_quality(
    inspection: GeneratedVideoInspection,
) -> GeneratedVideoQualityResult:
    issues: list[str] = []
    if inspection.frame_count != 5:
        issues.append("VIDEO_SAMPLE_COUNT_INVALID")
    if inspection.identity_consistency_score < MIN_GENERATED_VIDEO_IDENTITY_SCORE:
        issues.append("VIDEO_IDENTITY_DRIFT")
    if inspection.outfit_consistency_score < MIN_GENERATED_VIDEO_OUTFIT_SCORE:
        issues.append("VIDEO_OUTFIT_DRIFT")
    if inspection.motion_continuity_score < MIN_GENERATED_VIDEO_MOTION_SCORE:
        issues.append("VIDEO_MOTION_DISCONTINUITY")
    if not inspection.anatomy_valid:
        issues.append("VIDEO_ANATOMY_INVALID")
    if inspection.extra_people_detected:
        issues.append("VIDEO_EXTRA_PEOPLE_DETECTED")
    if inspection.severe_flicker_detected:
        issues.append("VIDEO_SEVERE_FLICKER")
    return GeneratedVideoQualityResult(
        passed=not issues,
        issue_codes=issues,
        inspection=inspection,
    )


def scene_contact_sheet_retry_prompt(
    base_prompt: str,
    issue_codes: list[str],
    attempt: int,
) -> str:
    if attempt == 1 or not issue_codes:
        return base_prompt
    guidance = {
        "VIEW_COUNT_INVALID": "必须生成且只生成五个人物视角，并严格遵循规定布局。",
        "IDENTITY_INCONSISTENT": "五个视角必须保持与授权照片完全相同的人物身份。",
        "OUTFIT_MISMATCH": "五个视角的服装、鞋履与配饰必须完整符合用户描述并保持一致。",
        "SCENE_MISMATCH": "背景和光线必须符合用户给定的场景描述并在五个视角中一致。",
        "ANATOMY_INVALID": "修正手指、四肢、颈部和身体连接，保持真实人体结构。",
        "TEXT_DETECTED": "移除所有文字、水印、Logo、数字和符号。",
        "EXTRA_PEOPLE_DETECTED": "每个视角只允许出现目标人物，不得增加其他人物。",
    }
    corrections = "\n".join(
        f"- {guidance[code]}" for code in dict.fromkeys(issue_codes) if code in guidance
    )
    return f"{base_prompt}\n\n自动质检未通过，第 {attempt} 次生成必须修正以下问题：\n{corrections}"


def prepare_first_frame_generation(
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
    model: FirstFrameModel,
    prompt: str | None,
    quantity: int,
    character_version_id: str | None = None,
    character_reference_selection_id: str | None = None,
    aspect_ratio: str | None = None,
    replace_scene: bool = False,
) -> FirstFrameGenerationPlan:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.generate",
        entity_type="project",
        entity_id=project_id,
    )
    require_readable_video_analysis(conn, project_id=project_id)
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.generate")
    if model not in FIRST_FRAME_MODELS:
        raise first_frame_error(
            422, "FIRST_FRAME_MODEL_UNSUPPORTED", "The requested image model is unavailable."
        )
    if aspect_ratio not in {None, "9:16", "16:9", "1:1", "3:4", "4:3"}:
        raise first_frame_error(422, "FIRST_FRAME_ASPECT_RATIO_UNSUPPORTED", "图片画幅不受支持。")
    if quantity < 1 or quantity > MAX_FIRST_FRAME_CANDIDATES:
        raise first_frame_error(
            422,
            "FIRST_FRAME_QUANTITY_INVALID",
            f"Generate between 1 and {MAX_FIRST_FRAME_CANDIDATES} candidates.",
        )

    source_selection = current_source_frame_selection(conn, project_id=project_id)
    source_frame_asset_id = str(source_selection["source_frame_asset_id"])
    source_frame = require_asset_access(
        conn,
        actor=actor,
        asset_id=source_frame_asset_id,
        action="first_frame.generate",
    )
    if str(source_frame["project_id"]) != project_id or str(source_frame["kind"]) != "source_frame":
        raise first_frame_error(
            422, "SOURCE_FRAME_INVALID", "The confirmed source frame is invalid."
        )

    character_inputs = resolve_first_frame_character_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=str(source_selection["id"]),
        expected_character_version_id=character_version_id,
        expected_reference_selection_id=character_reference_selection_id,
    )
    raw_source_timestamp = source_selection.get("timestamp_seconds")
    source_timestamp_seconds = (
        float(raw_source_timestamp)
        if isinstance(raw_source_timestamp, int | float)
        and not isinstance(raw_source_timestamp, bool)
        else None
    )
    project_appearance = apply_selected_scene_look(
        resolve_project_appearance_spec(
            conn,
            project_id=project_id,
            source_timestamp_seconds=source_timestamp_seconds,
        ),
        character_inputs=character_inputs,
    )
    reference_assets = [
        read_character_reference_asset(
            conn,
            actor=actor,
            asset_id=asset_id,
            authorized_project_ids=character_inputs.authorized_project_ids,
        )
        for asset_id in character_inputs.reference_asset_ids
    ]
    effective_prompt = normalize_prompt(
        prompt,
        character_name=character_inputs.character_name,
        reference_roles=character_inputs.reference_asset_roles,
        project_appearance=project_appearance,
        replace_scene=replace_scene,
    )

    return FirstFrameGenerationPlan(
        project_id=project_id,
        actor=actor,
        model=model,
        quantity=quantity,
        source_frame_asset_id=source_frame_asset_id,
        source_frame_selection_version_id=str(source_selection["id"]),
        character_inputs=character_inputs,
        source_asset=asset_snapshot(source_frame),
        reference_assets=[asset_snapshot(asset) for asset in reference_assets],
        project_appearance=project_appearance,
        effective_prompt=effective_prompt,
        aspect_ratio=aspect_ratio,
        replace_scene=replace_scene,
    )


def load_first_frame_generation_work(
    plan: FirstFrameGenerationPlan,
    *,
    storage: StorageAdapter,
) -> FirstFrameGenerationWork:
    """Read COS inputs after the customer session transaction has committed."""

    source_image = read_asset_image(storage, plan.source_asset)
    effective_prompt = plan.effective_prompt
    if plan.aspect_ratio and plan.aspect_ratio != image_aspect_ratio(source_image):
        effective_prompt = effective_prompt.replace(
            "输出必须保持其画幅比例、取景范围和人物占画面比例。",
            f"目标画幅为 {plan.aspect_ratio}，保留原有主体与人物姿态，保持人物占画面比例。",
        ).replace("禁止裁切或扩图。", "禁止裁切原有主体，仅允许扩展原背景以适配目标画幅。")
    return FirstFrameGenerationWork(
        project_id=plan.project_id,
        actor=plan.actor,
        model=plan.model,
        quantity=plan.quantity,
        source_frame_asset_id=plan.source_frame_asset_id,
        source_frame_selection_version_id=plan.source_frame_selection_version_id,
        character_inputs=plan.character_inputs,
        source_image=source_image,
        reference_images=[read_asset_image(storage, asset) for asset in plan.reference_assets],
        project_appearance=plan.project_appearance,
        effective_prompt=effective_prompt,
        aspect_ratio=plan.aspect_ratio,
        replace_scene=plan.replace_scene,
    )


def perform_first_frame_generation(
    work: FirstFrameGenerationWork,
    *,
    provider: ImageProvider,
    quality_inspector: FirstFrameQualityInspector | None = None,
    before_provider_call: Callable[[], None] | None = None,
    after_provider_call: Callable[[], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    quality_heartbeat: Callable[[], None] | None = None,
    resumed_candidates: list[GeneratedImage] | None = None,
    archive_generated: Callable[[list[GeneratedImage], int], list[GeneratedImage]] | None = None,
    checkpoint_candidates: Callable[[list[GeneratedImage]], None] | None = None,
    on_generated_images: Callable[[int], None] | None = None,
    provider_submission: dict[str, object] | None = None,
    save_provider_submission: Callable[[dict[str, object]], None] | None = None,
) -> list[GeneratedImage]:
    """Deliver one paid batch directly for human review, without AI inspection.

    Durable candidates/receipts are reused after interruption. A new provider
    request is only made for a new task; quality never triggers regeneration.
    """
    candidates = list(resumed_candidates or [])
    if candidates:
        return candidates

    def before_paid_call() -> None:
        if heartbeat is not None:
            heartbeat()
        if before_provider_call is not None:
            before_provider_call()

    if isinstance(provider, ApilioImageProvider) and save_provider_submission:
        generated = generate_scene_async(
            work,
            provider=provider,
            submission=provider_submission,
            save_submission=save_provider_submission,
            before_paid_call=before_paid_call,
            heartbeat=heartbeat,
        )
    else:
        generated = edit_once_with_retry(
            provider,
            model=work.model,
            prompt=work.effective_prompt,
            source_image=work.source_image,
            character_reference_images=work.reference_images,
            quantity=work.quantity,
            max_attempts=1,
            aspect_ratio=getattr(work, "aspect_ratio", None),
            before_provider_call=before_paid_call,
            after_provider_call=after_provider_call,
        )
    if on_generated_images is not None:
        on_generated_images(len(generated))
    if len(generated) != work.quantity or any(
        not item.content or item.content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES
        for item in generated
    ):
        raise first_frame_error(
            502,
            "FIRST_FRAME_PROVIDER_RESPONSE_INVALID",
            "The image provider did not return the requested candidates.",
        )
    candidates = [replace(candidate, quality_attempt=1, quality=None) for candidate in generated]
    if archive_generated is not None:
        candidates = archive_generated(candidates, 1)
    if checkpoint_candidates is not None:
        checkpoint_candidates(candidates)
    return candidates


def store_first_frame_generation(
    work: FirstFrameGenerationWork,
    *,
    storage: StorageAdapter,
    generated: list[GeneratedImage],
) -> StoredFirstFrameCandidates:
    """Archive provider output without holding a database transaction."""

    created_assets: list[tuple[str, str]] = []
    try:
        candidates: list[dict[str, object]] = []
        for image in generated:
            candidate = dict(image.stored_candidate or {})
            if not candidate:
                extension = image_extension(image.content_type)
                asset_id = str(uuid4())
                storage_key = f"projects/{work.project_id}/first-frames/{asset_id}.{extension}"
                created_assets.append((asset_id, storage_key))
                stored = storage.put_object(
                    storage_key,
                    image.content,
                    content_type=image.content_type,
                )
                candidate = {
                    "asset_id": asset_id,
                    "storage_key": storage_key,
                    "storage_uri": stored.uri,
                    "sha256": stored.sha256 or hashlib.sha256(image.content).hexdigest(),
                    "size_bytes": stored.size,
                    "content_type": image.content_type,
                }
            candidate["quality"] = (
                image.quality.model_dump(mode="json") if image.quality is not None else None
            )
            candidates.append(candidate)
        return StoredFirstFrameCandidates(
            candidates=candidates,
            created_assets=created_assets,
        )
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        delete_created_first_frames(storage, created_assets, actor_id=work.actor.id)
        raise first_frame_error(
            503,
            "FIRST_FRAME_STORAGE_UNAVAILABLE",
            "First-frame storage is temporarily unavailable.",
        ) from exc


def first_frame_character_contract(
    character_inputs: FirstFrameCharacterInputs,
) -> dict[str, object]:
    roles = character_inputs.reference_asset_roles
    identity_source = (
        "selected_scene_image"
        if character_uses_scene_look(character_inputs)
        else "contact_sheet+source_photo"
        if "contact_sheet" in roles and "source_photo" in roles
        else "legacy_views_only"
    )
    return {
        "identity_source": identity_source,
        "body_reconstruction": True,
        "preserve_scene": True,
        "preserve_pose": True,
        "preserve_framing": True,
        "clothing_policy": (
            "selected_scene_look"
            if character_uses_scene_look(character_inputs)
            else "project_appearance_first"
        ),
    }


def character_uses_scene_look(character_inputs: FirstFrameCharacterInputs) -> bool:
    persona = character_inputs.character_snapshot.get("persona_snapshot_json")
    if not isinstance(persona, Mapping):
        return False
    constraints = persona.get("appearance_constraints_json")
    return isinstance(constraints, Mapping) and constraints.get("appearance_type") == "scene"


def persist_project_character_appearance(
    conn: BusinessConnection,
    *,
    work: FirstFrameGenerationWork,
) -> sqlite3.Row:
    latest = latest_version(conn, work.project_id, PROJECT_CHARACTER_APPEARANCE_KIND)
    if latest is not None:
        try:
            payload = json.loads(str(latest["payload_json"]))
        except json.JSONDecodeError:
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("fingerprint") == work.project_appearance.fingerprint
            and payload.get("main_character_version_id")
            == work.character_inputs.main_character_version_id
        ):
            return latest
    return insert_version(
        conn,
        project_id=work.project_id,
        asset_id=work.source_frame_asset_id,
        kind=PROJECT_CHARACTER_APPEARANCE_KIND,
        created_by_user_id=work.actor.id,
        payload={
            **work.project_appearance.as_payload(),
            "main_character_version_id": work.character_inputs.main_character_version_id,
            "character_version_id": work.character_inputs.character_version_id,
            "character_name": work.character_inputs.character_name,
            "generation_mode": (
                "selected_scene_look"
                if work.project_appearance.appearance_source == "SCENE_LOOK"
                else "deterministic_scene_match"
            ),
        },
        commit=False,
    )


def _first_frame_pool_binding(payload: object) -> tuple[object, ...] | None:
    """Input identity of a candidates payload; ``None`` for legacy shapes.

    Candidates may only be carried forward across generations made from the
    same bound inputs: confirm and the H3 fence read only the latest
    candidates version, so a merged pool must never mix different input
    bindings.
    """
    if not isinstance(payload, dict):
        return None
    parts: list[object] = []
    for key in (
        "source_frame_selection_version_id",
        "main_character_version_id",
        "character_reference_asset_ids",
        "character_reference_asset_roles",
        "model",
        "aspect_ratio",
        "replace_scene",
        "prompt",
    ):
        value: object = payload.get(key)
        if isinstance(value, (list, dict)):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
        parts.append(value)
    appearance = payload.get("project_appearance")
    parts.append(appearance.get("fingerprint") if isinstance(appearance, dict) else None)
    if parts[0] is None or parts[1] is None:
        return None
    return tuple(parts)


def complete_first_frame_generation(
    conn: BusinessConnection,
    *,
    work: FirstFrameGenerationWork,
    provider: ImageProvider,
    stored: StoredFirstFrameCandidates,
    before_commit: Callable[[sqlite3.Row], None] | None = None,
) -> sqlite3.Row:
    """Revalidate inputs and atomically publish the already-archived images."""

    require_current_first_frame_inputs(
        conn,
        project_id=work.project_id,
        source_frame_selection_version_id=work.source_frame_selection_version_id,
        main_character_version_id=work.character_inputs.main_character_version_id,
        character_reference_selection_id=(work.character_inputs.character_reference_selection_id),
        character_version_id=work.character_inputs.character_version_id,
        require_usable_character=True,
    )
    current_appearance = apply_selected_scene_look(
        resolve_project_appearance_spec(
            conn,
            project_id=work.project_id,
            source_timestamp_seconds=work.project_appearance.source_timestamp_seconds,
        ),
        character_inputs=work.character_inputs,
    )
    if current_appearance.fingerprint != work.project_appearance.fingerprint:
        raise first_frame_error(
            409,
            "FIRST_FRAME_PROJECT_APPEARANCE_STALE",
            "Video analysis changed. Generate first-frame candidates again.",
        )
    if not conn.is_postgres:
        conn.execute("BEGIN IMMEDIATE")
    try:
        appearance_version = persist_project_character_appearance(conn, work=work)
        for candidate in stored.candidates:
            conn.execute(
                """
                    INSERT INTO assets (
                        id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                        created_by_user_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    candidate["asset_id"],
                    work.project_id,
                    "first_frame",
                    candidate["storage_uri"],
                    candidate["sha256"],
                    candidate["size_bytes"],
                    candidate["content_type"],
                    work.actor.id,
                ),
            )
        version_payload: dict[str, object] = {
            "schema_version": FIRST_FRAME_SCHEMA_VERSION,
            "source_frame_selection_version_id": work.source_frame_selection_version_id,
            "source_frame_asset_id": work.source_frame_asset_id,
            "main_character_version_id": work.character_inputs.main_character_version_id,
            "character_snapshot": work.character_inputs.character_snapshot,
            "character_reference_asset_ids": work.character_inputs.reference_asset_ids,
            "character_reference_asset_roles": work.character_inputs.reference_asset_roles,
            "provider": provider.provider_name,
            "model": work.model,
            "aspect_ratio": work.aspect_ratio,
            "replace_scene": work.replace_scene,
            "prompt": work.effective_prompt,
            "review_mode": "HUMAN_CONFIRMATION",
            "reconstruction_mode": FIRST_FRAME_RECONSTRUCTION_MODE,
            "character_contract": {
                **first_frame_character_contract(work.character_inputs),
                "preserve_scene": not work.replace_scene,
            },
            "project_appearance": work.project_appearance.as_payload(),
            "project_character_appearance_version_id": str(appearance_version["id"]),
            "candidates": stored.candidates,
        }
        if work.character_inputs.character_reference_selection_id is not None:
            version_payload["character_reference_selection_id"] = (
                work.character_inputs.character_reference_selection_id
            )
            version_payload["character_version_id"] = work.character_inputs.character_version_id
        # 单张重生成：同输入绑定的上一池候选仍必须可确认（确认与 H3 围栏
        # 只读最新候选版本），随新版本一并携带；绑定变化则从空池开始。
        previous_candidates_version = latest_version(
            conn, work.project_id, FIRST_FRAME_CANDIDATES_KIND
        )
        if previous_candidates_version is not None:
            try:
                previous_payload = json.loads(str(previous_candidates_version["payload_json"]))
            except json.JSONDecodeError:
                previous_payload = None
            if (
                isinstance(previous_payload, dict)
                and _first_frame_pool_binding(previous_payload)
                == _first_frame_pool_binding(version_payload)
                and isinstance(previous_payload.get("candidates"), list)
            ):
                carried = [
                    candidate
                    for candidate in previous_payload["candidates"]
                    if isinstance(candidate, dict)
                ]
                version_payload["candidates"] = [
                    *carried,
                    *stored.candidates,
                ][-MAX_FIRST_FRAME_CANDIDATE_POOL:]
        row = insert_version(
            conn,
            project_id=work.project_id,
            asset_id=work.source_frame_asset_id,
            kind=FIRST_FRAME_CANDIDATES_KIND,
            created_by_user_id=work.actor.id,
            payload=version_payload,
            commit=False,
        )
        write_audit(
            conn,
            actor=work.actor,
            action="first_frame.generate",
            entity_type="version",
            entity_id=str(row["id"]),
            metadata={
                "project_id": work.project_id,
                "model": work.model,
                "quantity": work.quantity,
            },
            commit=False,
        )
        if before_commit is not None:
            before_commit(row)
        if not conn.is_postgres:
            conn.commit()
    except BaseException:
        if not conn.is_postgres:
            conn.rollback()
        raise
    return row


def confirm_first_frame(
    conn: BusinessConnection,
    *,
    project_id: str,
    first_frame_asset_id: str,
    actor: CurrentUser,
    allow_unverified: bool = False,
    first_frame_candidates_version_id: str | None = None,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.confirm")
    if first_frame_candidates_version_id is None:
        candidate_version = current_first_frame_candidates(conn, project_id=project_id)
    else:
        latest = latest_version(conn, project_id=project_id, kind=FIRST_FRAME_CANDIDATES_KIND)
        if latest is not None and str(latest["id"]) == first_frame_candidates_version_id:
            # 显式指向当前（最新）候选版本：保持严格契约，输入新鲜度校验照跑。
            candidate_version = current_first_frame_candidates(conn, project_id=project_id)
        else:
            # 历史候选版本放开：用户显式选择了旧版本里已付费生成的图，只校验
            # 版本存在且属于本项目；不再要求“仍是最新”或输入未变化——历史图
            # 天然基于旧输入，「基于旧输入生成」的警示由前端承担。
            candidate_version = first_frame_candidates_version_by_id(
                conn, project_id=project_id, version_id=first_frame_candidates_version_id
            )
    payload = json.loads(str(candidate_version["payload_json"]))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    candidate = next(
        (
            value
            for value in candidates
            if isinstance(value, dict) and value.get("asset_id") == first_frame_asset_id
        ),
        None,
    )
    if candidate is None:
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "Select a candidate from the latest set."
        )
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=first_frame_asset_id,
        action="first_frame.confirm",
    )
    if str(asset["project_id"]) != project_id or str(asset["kind"]) != "first_frame":
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "The selected first frame is invalid."
        )

    selection_payload: dict[str, object] = {
        "schema_version": FIRST_FRAME_SCHEMA_VERSION,
        "first_frame_candidates_version_id": str(candidate_version["id"]),
        "first_frame_asset_id": first_frame_asset_id,
    }
    selection_payload["review_mode"] = "HUMAN_CONFIRMATION"
    selection_payload["reviewed_by_user_id"] = actor.id
    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=first_frame_asset_id,
        kind=FIRST_FRAME_SELECTION_KIND,
        created_by_user_id=actor.id,
        payload=selection_payload,
    )
    write_audit(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "first_frame_asset_id": first_frame_asset_id},
    )
    return row


def current_source_frame_selection(
    conn: BusinessConnection, *, project_id: str
) -> dict[str, object]:
    selection = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if selection is None or candidates is None:
        raise first_frame_error(
            409, "SOURCE_FRAME_SELECTION_REQUIRED", "Confirm a source frame first."
        )
    payload = json.loads(str(selection["payload_json"]))
    if payload.get("source_frame_candidates_version_id") != str(candidates["id"]):
        raise first_frame_error(
            409,
            "SOURCE_FRAME_SELECTION_STALE",
            "Select a source frame from the latest candidate set.",
        )
    return cast(dict[str, object], payload | {"id": str(selection["id"])})


def effective_reference_asset_ids(
    conn: BusinessConnection,
    *,
    character_version_id: str,
    legacy_selected: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve the reference images actually sent to the image provider.

    Simple-upload characters publish a five-view contact sheet; when one is
    present it replaces the per-view placeholder assets as the identity
    input: the contact sheet supplies multi-angle identity while the
    identity's original uploaded photo is the authoritative face. Legacy
    characters without a contact sheet keep their selected per-view images.
    Scene looks instead use the published FRONT_FULL crop shown in the UI;
    sending the contact sheet would give the provider a different multi-panel
    image than the user selected.
    Returns ``(asset_ids, roles)`` with roles mirroring asset_ids.
    """
    row = conn.execute(
        """
        SELECT version.publication_snapshot_json AS snapshot_json,
               version.persona_snapshot_json AS persona_snapshot_json,
               identity.source_asset_id AS source_asset_id
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE version.id = %s
        """,
        (character_version_id,),
    ).fetchone()
    if row is None:
        return legacy_selected, ["legacy_view"] * len(legacy_selected)
    try:
        snapshot = json.loads(str(row["snapshot_json"] or ""))
    except json.JSONDecodeError:
        snapshot = None
    contact_sheet_asset_id = (
        snapshot.get("contact_sheet_asset_id") if isinstance(snapshot, dict) else None
    )
    try:
        persona = json.loads(str(row["persona_snapshot_json"] or ""))
    except json.JSONDecodeError:
        persona = None
    constraints = persona.get("appearance_constraints_json") if isinstance(persona, dict) else None
    if isinstance(constraints, dict) and constraints.get("appearance_type") == "scene":
        snapshot_assets = snapshot.get("assets_by_view") if isinstance(snapshot, dict) else None
        front_full = (
            snapshot_assets.get("FRONT_FULL") if isinstance(snapshot_assets, dict) else None
        )
        front_full_asset_id = (
            front_full.get("approved_asset_id") if isinstance(front_full, dict) else None
        )
        if isinstance(front_full_asset_id, str) and front_full_asset_id:
            return [front_full_asset_id], ["scene_image"]
        # A scene look is already a complete authored appearance. One selected
        # scene image is sufficient; additional views only add conflicting cues.
        selected_scene = legacy_selected[:1]
        return selected_scene, ["scene_image"] * len(selected_scene)
    source_asset_id = row["source_asset_id"]
    if (
        isinstance(contact_sheet_asset_id, str)
        and contact_sheet_asset_id
        and isinstance(source_asset_id, str)
        and source_asset_id
    ):
        return [contact_sheet_asset_id, source_asset_id], ["contact_sheet", "source_photo"]
    return legacy_selected, ["legacy_view"] * len(legacy_selected)


def resolve_first_frame_character_inputs(
    conn: BusinessConnection,
    *,
    project_id: str,
    source_frame_selection_version_id: str,
    expected_character_version_id: str | None = None,
    expected_reference_selection_id: str | None = None,
) -> FirstFrameCharacterInputs:
    try:
        reference_selection = current_character_reference_selection_for_generation(
            conn,
            project_id=project_id,
            source_frame_version_id=source_frame_selection_version_id,
        )
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise first_frame_binding_stale() from exc
        raise
    if reference_selection is not None:
        if expected_character_version_id is None or expected_reference_selection_id is None:
            raise first_frame_binding_required()
        if (
            reference_selection.character_version_id != expected_character_version_id
            or reference_selection.id != expected_reference_selection_id
        ):
            raise first_frame_binding_stale()
        snapshot = reference_selection.character_version_snapshot_json
        persona_snapshot = snapshot.get("persona_snapshot_json")
        if not isinstance(persona_snapshot, dict):
            raise stale_first_frame_inputs()
        character_name = persona_snapshot.get("name")
        if not isinstance(character_name, str) or not character_name:
            raise stale_first_frame_inputs()
        main_character_version_id = snapshot.get("main_character_version_id")
        if not isinstance(main_character_version_id, str):
            raise stale_first_frame_inputs()
        reference_asset_ids, reference_asset_roles = effective_reference_asset_ids(
            conn,
            character_version_id=reference_selection.character_version_id,
            legacy_selected=reference_selection.selected_asset_ids_json,
        )
        return FirstFrameCharacterInputs(
            main_character_version_id=main_character_version_id,
            character_snapshot=snapshot,
            reference_asset_ids=reference_asset_ids,
            character_name=character_name,
            authorized_project_ids=[],
            character_reference_selection_id=reference_selection.id,
            character_version_id=reference_selection.character_version_id,
            reference_asset_roles=reference_asset_roles,
        )

    if expected_character_version_id is not None or expected_reference_selection_id is not None:
        raise first_frame_binding_stale()

    main_character = get_project_main_character(conn, project_id=project_id)
    character = read_character(conn, str(main_character["character_id"]))
    if not character_is_available(character, project_id=project_id):
        raise first_frame_error(
            422,
            "CHARACTER_NOT_AVAILABLE",
            "The selected character is inactive, expired, or not authorized for this project.",
        )
    character_snapshot = main_character["character_snapshot"]
    if not isinstance(character_snapshot, dict):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    snapshot_reference_ids = character_snapshot.get("reference_asset_ids")
    character_name = character_snapshot.get("name")
    if not isinstance(snapshot_reference_ids, list) or not all(
        isinstance(asset_id, str) for asset_id in snapshot_reference_ids
    ):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not isinstance(character_name, str) or not character_name:
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not snapshot_reference_ids:
        raise first_frame_error(
            422,
            "CHARACTER_REFERENCE_REQUIRED",
            "The selected character needs at least one reference image.",
        )
    authorized_project_ids = character_snapshot.get("authorization_project_ids") or []
    if not isinstance(authorized_project_ids, list) or not all(
        isinstance(project, str) for project in authorized_project_ids
    ):
        authorized_project_ids = []
    return FirstFrameCharacterInputs(
        main_character_version_id=str(main_character["version_id"]),
        character_snapshot=character_snapshot,
        reference_asset_ids=cast(list[str], snapshot_reference_ids),
        character_name=character_name,
        authorized_project_ids=cast(list[str], authorized_project_ids),
    )


def current_first_frame_candidates(conn: BusinessConnection, *, project_id: str) -> sqlite3.Row:
    candidates = latest_version(conn, project_id, FIRST_FRAME_CANDIDATES_KIND)
    if candidates is None:
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_NOT_FOUND", "Generate first-frame candidates first."
        )
    payload = json.loads(str(candidates["payload_json"]))
    if not isinstance(payload, dict):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    source_version_id = payload.get("source_frame_selection_version_id")
    main_character_version_id = payload.get("main_character_version_id")
    reference_selection_id = payload.get("character_reference_selection_id")
    character_version_id = payload.get("character_version_id")
    if not isinstance(source_version_id, str) or not isinstance(main_character_version_id, str):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    if (reference_selection_id is None) != (character_version_id is None) or (
        reference_selection_id is not None
        and (
            not isinstance(reference_selection_id, str) or not isinstance(character_version_id, str)
        )
    ):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    require_current_first_frame_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=source_version_id,
        main_character_version_id=main_character_version_id,
        character_reference_selection_id=reference_selection_id,
        character_version_id=character_version_id,
    )
    project_appearance = payload.get("project_appearance")
    if isinstance(project_appearance, dict):
        stored_fingerprint = project_appearance.get("fingerprint")
        raw_timestamp = project_appearance.get("source_timestamp_seconds")
        source_timestamp_seconds = (
            float(raw_timestamp)
            if isinstance(raw_timestamp, int | float) and not isinstance(raw_timestamp, bool)
            else None
        )
        current_appearance = resolve_project_appearance_spec(
            conn,
            project_id=project_id,
            source_timestamp_seconds=source_timestamp_seconds,
        )
        character_snapshot = payload.get("character_snapshot")
        if isinstance(character_snapshot, Mapping):
            current_appearance = _apply_scene_look_snapshot(
                current_appearance,
                character_snapshot=character_snapshot,
                character_version_id=(
                    character_version_id if isinstance(character_version_id, str) else None
                ),
            )
        if (
            not isinstance(stored_fingerprint, str)
            or stored_fingerprint != current_appearance.fingerprint
        ):
            raise stale_first_frame_inputs()
    return candidates


def first_frame_candidates_version_by_id(
    conn: BusinessConnection, *, project_id: str, version_id: str
) -> sqlite3.Row:
    """按显式版本 id 读取首帧候选（历史版本确认/选择的放开路径）。

    与 ``current_first_frame_candidates`` 的差别：不要求该版本仍是最新、也不
    重跑输入新鲜度校验——历史版本天然基于旧输入。版本存在性、项目归属与
    候选结构仍校验，损坏/越权记录不会被读成有效确认。
    """
    row = conn.execute(
        """
        SELECT id, project_id, payload_json
        FROM versions
        WHERE id = %s AND project_id = %s AND kind = %s
        """,
        (version_id, project_id, FIRST_FRAME_CANDIDATES_KIND),
    ).fetchone()
    if row is None:
        raise first_frame_error(
            404,
            "FIRST_FRAME_CANDIDATES_VERSION_NOT_FOUND",
            "The requested first-frame candidate version does not exist.",
        )
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    return cast(sqlite3.Row, row)


def require_current_first_frame_inputs(
    conn: BusinessConnection,
    *,
    project_id: str,
    source_frame_selection_version_id: str,
    main_character_version_id: str,
    character_reference_selection_id: str | None = None,
    character_version_id: str | None = None,
    require_usable_character: bool = False,
) -> None:
    if character_reference_selection_id is not None and character_version_id is not None:
        try:
            source_selection = current_source_frame_selection(conn, project_id=project_id)
            reference_selection = current_character_reference_selection_for_generation(
                conn,
                project_id=project_id,
                source_frame_version_id=source_frame_selection_version_id,
                expected_selection_id=character_reference_selection_id,
                require_usable_character=require_usable_character,
            )
        except HTTPException as exc:
            if exc.status_code in {404, 409}:
                raise stale_first_frame_inputs() from exc
            raise
        if (
            str(source_selection["id"]) != source_frame_selection_version_id
            or reference_selection is None
            or reference_selection.character_version_id != character_version_id
            or reference_selection.character_version_snapshot_json.get("main_character_version_id")
            != main_character_version_id
        ):
            raise stale_first_frame_inputs()
        return
    if character_reference_selection_id is not None or character_version_id is not None:
        raise stale_first_frame_inputs()

    try:
        source_selection = current_source_frame_selection(conn, project_id=project_id)
        main_character = get_project_main_character(conn, project_id=project_id)
        character = read_character(conn, str(main_character["character_id"]))
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise first_frame_error(
                409,
                "FIRST_FRAME_CANDIDATES_STALE",
                "Generate first-frame candidates again using the current source frame "
                "and character.",
            ) from exc
        raise
    if (
        str(source_selection["id"]) != source_frame_selection_version_id
        or str(main_character["version_id"]) != main_character_version_id
        or not character_is_available(character, project_id=project_id)
    ):
        raise first_frame_error(
            409,
            "FIRST_FRAME_CANDIDATES_STALE",
            "Generate first-frame candidates again using the current source frame and character.",
        )


def stale_first_frame_inputs() -> HTTPException:
    return first_frame_error(
        409,
        "FIRST_FRAME_CANDIDATES_STALE",
        "Generate first-frame candidates again using the current source frame and character.",
    )


def first_frame_binding_required() -> HTTPException:
    return first_frame_error(
        422,
        "CHARACTER_REFERENCE_BINDING_REQUIRED",
        "Confirm the current character references before generating first-frame candidates.",
    )


def first_frame_binding_stale() -> HTTPException:
    return first_frame_error(
        409,
        "FIRST_FRAME_INPUT_BINDING_STALE",
        "The character reference binding changed. Confirm the current references again.",
    )


def read_character_reference_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
    authorized_project_ids: list[str],
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes, content_type
        FROM assets WHERE id = %s
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise first_frame_error(
            422, "CHARACTER_REFERENCE_NOT_FOUND", "A character reference image is missing."
        )
    # A character library is a workspace-global entity, but its reference images
    # may belong to another project. Gate the read so an employee cannot pull
    # bytes from a project they have no access to, unless that project is within
    # the character's declared authorization scope (cross-project library use).
    try:
        require_asset_access(
            conn, actor=actor, asset_id=asset_id, action="character_reference.read"
        )
    except HTTPException:
        if str(row["project_id"]) not in authorized_project_ids:
            raise
    if str(row["content_type"]) not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise first_frame_error(
            422,
            "CHARACTER_REFERENCE_INVALID",
            "Character references must be JPEG, PNG, or WebP images.",
        )
    return cast(sqlite3.Row, row)


def asset_snapshot(asset: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    return {
        "id": asset["id"],
        "storage_uri": asset["storage_uri"],
        "content_type": asset["content_type"],
    }


def read_asset_image(storage: StorageAdapter, asset: Mapping[str, object]) -> ImageInput:
    content_type = str(asset["content_type"])
    if content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise first_frame_error(
            422,
            "FIRST_FRAME_IMAGE_TYPE_UNSUPPORTED",
            "Source and character reference images must be JPEG, PNG, or WebP.",
        )
    try:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        if reference.provider == "local":
            from app.bootstrap import is_customer_production

            if is_customer_production():
                raise StorageBackendUnavailable("Legacy local assets require migration to COS")
        # The authorized snapshot may reference a historical local asset after
        # new writes switched to COS. Resolve only that explicit local URI;
        # output archiving still uses the active storage and cloud buckets must match.
        source_storage = (
            create_local_storage_from_environment()
            if reference.provider == "local" and storage.provider != "local"
            else storage
        )
        require_storage_match(source_storage, reference)
        content = source_storage.get_object(reference.key)
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise first_frame_error(
            503,
            "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE",
            "无法读取源画面或人物参考图，请检查素材是否仍存在及其存储访问权限。",
        ) from exc
    return ImageInput(
        content=content,
        content_type=content_type,
        filename=f"{asset['id']}.{image_extension(content_type)}",
    )


def generate_scene_async(
    work: FirstFrameGenerationWork,
    *,
    provider: ApilioImageProvider,
    submission: dict[str, object] | None,
    save_submission: Callable[[dict[str, object]], None],
    before_paid_call: Callable[[], None],
    heartbeat: Callable[[], None] | None,
) -> list[GeneratedImage]:
    if submission is None:
        before_paid_call()
        task_id = provider.submit_edit(
            model=work.model,
            prompt=work.effective_prompt,
            source_image=work.source_image,
            character_reference_images=work.reference_images,
            output_count=work.quantity,
            aspect_ratio=getattr(work, "aspect_ratio", None)
            or image_aspect_ratio(work.source_image),
        )
        submission = {
            "schema_version": 1,
            "task_id": task_id,
            "account_fingerprint": provider.account_fingerprint,
            "model": work.model,
            "output_count": work.quantity,
        }
        # A failed receipt write must stop here: no poll and no second POST.
        save_submission(submission)
    if (
        submission.get("account_fingerprint") != provider.account_fingerprint
        or submission.get("model") != work.model
        or submission.get("output_count") != work.quantity
    ):
        raise first_frame_error(
            409, "FIRST_FRAME_PROVIDER_CHANGED", "图像服务配置已变化，请联系管理员核对原任务。"
        )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if heartbeat is not None:
            heartbeat()
        try:
            generated = provider.poll_edit(str(submission["task_id"]), output_count=work.quantity)
            if generated is not None:
                return generated
        except RetryableImageProviderFailed:
            # Retrying a read cannot create another paid generation.
            pass
        time.sleep(5)
    raise RetryableImageProviderFailed("Apilio task polling timed out; receipt retained")


def edit_once_with_retry(
    provider: ImageProvider,
    *,
    model: FirstFrameModel,
    prompt: str,
    source_image: ImageInput,
    character_reference_images: list[ImageInput],
    quantity: int,
    max_attempts: int = 2,
    aspect_ratio: str | None = None,
    before_provider_call: Callable[[], None] | None = None,
    after_provider_call: Callable[[], None] | None = None,
) -> list[GeneratedImage]:
    for attempt in range(max_attempts):
        try:
            if before_provider_call is not None:
                before_provider_call()
            if isinstance(provider, ApilioImageProvider):
                generated = provider.edit(
                    model=model,
                    prompt=prompt,
                    source_image=source_image,
                    character_reference_images=character_reference_images,
                    output_count=quantity,
                    aspect_ratio=aspect_ratio or image_aspect_ratio(source_image),
                )
            else:
                generated = provider.edit(
                    model=model,
                    prompt=prompt,
                    source_image=source_image,
                    character_reference_images=character_reference_images,
                    output_count=quantity,
                )
            if after_provider_call is not None:
                after_provider_call()
            return generated
        except RetryableImageProviderFailed as exc:
            if attempt + 1 < max_attempts and after_provider_call is not None:
                # A retryable provider response is known and the helper owns
                # the safe retry. Only the final unresolved call stays marked
                # as uncertain for the durable task state machine.
                after_provider_call()
            if attempt + 1 == max_attempts:
                raise first_frame_error(
                    502,
                    "FIRST_FRAME_PROVIDER_FAILED",
                    "The image provider could not generate a first frame.",
                ) from exc
        except ImageProviderFailed as exc:
            raise first_frame_error(
                502,
                "FIRST_FRAME_PROVIDER_FAILED",
                "The image provider could not generate a first frame.",
            ) from exc
    raise AssertionError("image provider retry loop must return or raise")


def normalize_prompt(
    prompt: str | None,
    *,
    character_name: str,
    reference_roles: list[str] | None = None,
    project_appearance: ProjectAppearanceSpec | None = None,
    replace_scene: bool = False,
) -> str:
    clean = (prompt or "").strip()
    clean = clean.replace(FIRST_FRAME_NO_TEXT_CONSTRAINT, "").strip()
    appearance = project_appearance or derive_project_appearance_spec(
        analysis_payload={},
        source_analysis_version_id=None,
        source_timestamp_seconds=None,
    )
    primary_subject_contract = (
        f"目标替换对象仅为源画面中承担“{appearance.subject}”角色的主要人物。"
        "如果画面中有多人，只重构这一名主要人物；"
        "其他人物的身份、服装、数量、位置、动作和遮挡关系均保持不变，"
        "不得把目标人物外观扩散到旁人。"
    )
    if replace_scene:
        if appearance.appearance_source != "SCENE_LOOK":
            raise first_frame_error(
                422, "FIRST_FRAME_SCENE_LOOK_REQUIRED", "请先选择含目标背景的场景形象，再替换场景。"
            )
        return (
            f"将源画面的主要人物完整替换为所选场景形象“{character_name}”。\n"
            "第 1 张源画面提供人物姿态、动作、机位、画幅和主体占比；保留这些空间关系。\n"
            "第 2 张场景参考图是人物身份、服装造型与目标环境的唯一外观依据。"
            f"{primary_subject_contract}\n"
            "使用场景参考图的背景替换原背景，结合源画面的透视重建自然完整场景；"
            "光照、人物阴影与目标环境一致，不保留与目标场景冲突的原建筑或道具。\n"
            "目标场景参考图中实际存在的招牌文字与 Logo 保持原样；"
            "不得恢复第 1 张源背景中的招牌、门联或其他场景文字。\n"
            "完整重构人物的头脸、头发、身体、服装与肢体连接，不能只换脸。"
            "不得增加其他人物，不复制参考板分格线、边框或多面板布局；"
            "禁止模糊补边、缩图留白和拼贴。输出一张完整画面。\n"
            f"{FIRST_FRAME_NO_TEXT_CONSTRAINT}"
        )
    if appearance.appearance_source == "SCENE_LOOK":
        server_template = (
            f"将第 1 张原视频源画面中的主要人物，替换为用户选中的场景形象“{character_name}”。\n"
            "第 2 张及后续输入图是同一个已完成造型的场景人物，是唯一外观依据："
            "完整沿用其面容、发型、肤色、体型、服装、鞋履和配饰。"
            "不要重新设计服饰，不要保留原视频人物的外貌或衣服。\n"
            f"{primary_subject_contract}\n"
            "原视频源画面只提供人物姿态、动作、位置、朝向、遮挡关系、构图、机位、背景、道具和光照；"
            "这些内容保持不变。将目标形象自然适配原姿态和透视。\n"
            "以第 1 张源画面作为完整编辑画布，输出必须保持其画幅比例、取景范围和人物占画面比例。"
            "只修改人物本身；人物以外的建筑、地面、植物、道具及场景内原生文字和标识保持原样。"
            "禁止模糊补边，禁止增加上下或左右留白，禁止把原画面缩进新背景，禁止裁切或扩图。"
            "如果字幕覆盖在人物身上，清除字幕后自然补全人物与背景。\n"
            "场景参考图的背景、姿势、分格线、边框和多面板布局不属于替换内容，不能复制到结果。"
            "只输出一张自然完整画面，不增加或删除其他主体。\n"
            f"{FIRST_FRAME_NO_TEXT_CONSTRAINT}"
        )
        # Scene appearance is already authored; free text must not redesign it.
        return server_template
    else:
        appearance_contract = (
            f"项目人物造型（后台自动匹配）：场景为“{appearance.scene}”，"
            f"人物身份为“{appearance.subject}”；服装要求：{appearance.outfit_description}\n"
            "项目人物造型优先于参考图服装；人物身份特征必须稳定，但不得机械复制参考图的服装。\n"
            f"{primary_subject_contract}"
        )
        contact_sheet_role = (
            "第 2 张输入图是该角色的五视图参考板，仅用于确定人物身份、长相、发型与身材比例；"
        )
        clothing_rule = "参考图中的服装只用于理解人物体型，不得直接照搬。"
    if reference_roles and "contact_sheet" in reference_roles:
        server_template = (
            f"把原视频中的人物完整重构为角色库人物“{character_name}”，严格保留原画面一切要素。\n"
            "第 1 张输入图是原视频源帧，是构图、机位、人物姿态、动作、场景、道具、"
            "光线与色调的唯一模板，不得改动。\n"
            f"{contact_sheet_role}"
            "参考板中的白色分格线、边框与多面板布局只属于参考板本身，严禁以任何形式出现在结果图中。\n"
            "第 3 张输入图是该角色的原始照片，是面部特征最权威的依据，以它为准还原面部细节。\n"
            f"{appearance_contract}\n"
            "必须完整重构原人物的头脸、发型、颈部、肤色、身形比例、上装、下装、鞋子、手部与肢体连接；"
            f"{clothing_rule}遮挡边缘、镜面或反射中的人物也要保持一致。\n"
            "严禁只替换脸部、只覆盖头部或保留原视频人物的身体与服装；"
            "保持自然皮肤质感、正确肢体结构与真实透视；不得增加或删除画面主体；"
            "不得新增字幕、水印或参考板边框。"
        )
        # Full mode may add user instructions, but it must not replace the
        # stable reference-role contract owned by the server.
        base_prompt = f"{server_template}\n\n用户补充要求：\n{clean}" if clean else server_template
    else:
        server_template = (
            "保留原图的镜头位置、人物姿态、动作、场景、构图、道具、光线与色调，"
            f"把原人物完整重构为角色库人物“{character_name}”。\n"
            f"{appearance_contract}\n"
            "完整重构头脸、发型、颈部、肤色、身形、上下装、鞋子、手部和肢体连接；"
            "严禁只替换脸部或保留原人物身体；保持自然皮肤、正确肢体和真实透视；"
            "不得增加或删除主体。"
        )
        base_prompt = f"{server_template}\n\n用户补充要求：\n{clean}" if clean else server_template
    if not base_prompt:
        return FIRST_FRAME_NO_TEXT_CONSTRAINT
    return f"{base_prompt}\n\n{FIRST_FRAME_NO_TEXT_CONSTRAINT}"


def image_extension(content_type: str) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]


def delete_created_first_frames(
    storage: StorageAdapter,
    created_assets: list[tuple[str, str]],
    *,
    actor_id: str,
) -> None:
    for _, storage_key in created_assets:
        try:
            content_store.delete_object_outside_content_namespace(
                storage, storage_key, actor_id=actor_id
            )
        except (OSError, StorageBackendUnavailable):
            pass


def first_frame_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
