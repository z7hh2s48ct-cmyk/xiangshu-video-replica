from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.db_portable import BusinessConnection

ANALYSIS_KIND = "analysis"
SHOT_CARD_KIND = "shot_card"
SCHEMA_VERSION = "b3.analysis.v1"
APILIO_DEFAULT_BASE_URL = "https://api.apilio.ai"
# 图像语义检查（人物识别源图、首帧复核）2026-09-26 起与视频拆解统一用
# gemini-3.8-flash：业务确认两链路同用正式版 flash。此前单独留在 preview 名
# 上是因为 2026-09-20 故障无证据指向模型名；现在任何默认槽位都不再使用
# preview 名（曾发生过无预警下线）。
APILIO_GEMINI_MODEL = "gemini-3.8-flash"
# 视频拆解单独一个默认值：2026-09-20 线上拆解 100% 失败在上游 HTTP 400，
# preview 模型疑似已下线，改用正式版。上游再次调整时，设置页的「视频分析
# 模型」可直接覆盖这里，不必等下一次发版。
APILIO_ANALYSIS_MODEL = "gemini-3.8-flash"

# 结构化运动枚举：拆解结果必须对“人物是否在动、机位是否在动”显式表态，
# 下游（H3 Prompt 编译）据此确定性渲染运动指令，避免“边走边说”被
# 概括成“站着说话”后生成的人物僵立原地。
SUBJECT_MOTION_STATES = (
    "STATIC",
    "WALKING",
    "RUNNING",
    "TURNING",
    "GESTURING_ONLY",
    "OBJECT_MOTION",
    "NO_PERSON",
    "UNKNOWN",
)
SUBJECT_DIRECTIONS = (
    "toward_camera",
    "away_from_camera",
    "left",
    "right",
    "lateral",
    "in_place",
    "none",
    "unknown",
)
MOTION_CAMERA_MODES = (
    "STATIC",
    "PUSH_IN",
    "PULL_BACK",
    "HANDHELD_TRACKING",
    "PAN",
    "TILT",
    "FOLLOW",
    "ORBIT",
    "CRANE_UP",
    "CRANE_DOWN",
    "ZOOM_IN",
    "ZOOM_OUT",
    "UNKNOWN",
)
# 并列主态分隔符：真实视频里位移与手势常常同等主导（边走边做手势），只留其一
# 会把「在动」拆成两个都不完整的描述，下游合成因此丢失运动。
SUBJECT_MOTION_STATE_SEPARATOR = "+"


def _canonical_subject_motion_state(value: str) -> str:
    """校验人物运动状态；允许用「+」并列多个主导状态。

    并列只放宽「可同时表态」，分量仍必须是 ``SUBJECT_MOTION_STATES`` 成员，
    且不得重复——未知分量与重复分量照旧 fail closed。
    """
    parts = [part.strip() for part in str(value).split(SUBJECT_MOTION_STATE_SEPARATOR)]
    if not parts or any(part not in SUBJECT_MOTION_STATES for part in parts):
        raise ValueError(
            "subject_motion_state must be one of "
            f"{', '.join(SUBJECT_MOTION_STATES)}, optionally joined by "
            f"'{SUBJECT_MOTION_STATE_SEPARATOR}'"
        )
    if len(set(parts)) != len(parts):
        raise ValueError("subject_motion_state must not repeat the same state")
    return SUBJECT_MOTION_STATE_SEPARATOR.join(parts)


# Failure phases let the desktop tell "the network hiccuped, retry" apart from
# "the model answered with something we cannot use".
REQUEST_FAILURE_PHASE = "request"
NETWORK_FAILURE_PHASE = "network"
HTTP_FAILURE_PHASE = "http"
RESPONSE_FAILURE_PHASE = "response"
# Provider timelines are commonly rounded to 1–3 decimal places while ffprobe
# reports microsecond precision.  A small frame-scale tolerance absorbs that
# harmless representation drift without accepting materially invalid timelines.
TIMELINE_ROUNDING_TOLERANCE_SECONDS = 0.05

logger = logging.getLogger(__name__)

# Provider-call warnings are emitted deep inside the transport while the task
# identity lives with the worker; a context variable carries the reference so
# every line answers "which task/project/asset" without threading extra
# parameters through the transport protocol.
_analysis_task_ref: ContextVar[str | None] = ContextVar("analysis_task_ref", default=None)


@contextmanager
def analysis_task_context(*, task_id: str, project_id: str, asset_id: str) -> Iterator[None]:
    """Bind the task reference for provider-call log lines in this scope."""
    token = _analysis_task_ref.set(f"task={task_id} project={project_id} asset={asset_id}")
    try:
        yield
    finally:
        _analysis_task_ref.reset(token)


def analysis_log_ref() -> str:
    """Log-line prefix tying a provider call to its task, placeholder when unbound."""
    return _analysis_task_ref.get() or "task=- project=- asset=-"


class ShotMotion(BaseModel):
    """镜头运动的结构化描述：人物运动 / 机位运动 / 相对运动三分。"""

    model_config = ConfigDict(extra="forbid")

    subject_motion_state: str
    subject_direction: Literal[
        "toward_camera",
        "away_from_camera",
        "left",
        "right",
        "lateral",
        "in_place",
        "none",
        "unknown",
    ]
    subject_displacement: str = Field(min_length=1)
    hand_action: str = Field(min_length=1)
    camera_motion: Literal[
        "STATIC",
        "PUSH_IN",
        "PULL_BACK",
        "HANDHELD_TRACKING",
        "PAN",
        "TILT",
        "FOLLOW",
        "ORBIT",
        "CRANE_UP",
        "CRANE_DOWN",
        "ZOOM_IN",
        "ZOOM_OUT",
        "UNKNOWN",
    ]
    relative_motion: str = Field(min_length=1)

    @field_validator("subject_motion_state")
    @classmethod
    def validate_subject_motion_state(cls, value: str) -> str:
        return _canonical_subject_motion_state(value)


class ShotCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    shot_type: str = Field(min_length=1)
    composition: str = Field(min_length=1)
    camera_motion: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    # Optional only for backward compatibility. New provider responses still
    # populate it as useful scene metadata; first-frame generation targets the
    # analysed primary subject and preserves other people in place.
    person_count: int | None = Field(default=None, ge=0)
    action: str = Field(min_length=1)
    scene: str = Field(min_length=1)
    scene_dressing: str = ""
    scene_lighting: str = ""
    wardrobe_pose_detail: str = ""
    ambient_sound: str = ""
    music_style_hint: str = ""
    spoken_text: str
    transition: str = Field(min_length=1)
    # ``shots`` remains the compatibility name consumed by the existing
    # editor/compiler, but it now represents executable timeline segments.
    # A segment can start at a physical cut or at an action/semantic beat
    # inside one continuous camera take.
    segment_kind: Literal["SHOT_CUT", "ACTION_BEAT"] | None = None
    boundary_reason: str | None = Field(default=None, min_length=1)
    # 旧版本拆解结果与手动保存的镜头卡没有 motion；缺失时 H3 Prompt
    # 编译回退到 action 文本拼接（行为不劣化），新生成的分析必须携带。
    motion: ShotMotion | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> ShotCard:
        if self.end_time <= self.start_time:
            raise ValueError("shot end_time must be greater than start_time")
        return self


class VideoAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
    aspect_ratio: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    fps: float = Field(gt=0)
    theme: str = Field(min_length=1)
    visual_style: str = Field(min_length=1)
    color_tone: str = ""
    pace: str = Field(min_length=1)
    camera_language: str = Field(min_length=1)
    original_script: str
    shots: list[ShotCard] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shot_timeline(self) -> VideoAnalysis:
        previous_end = 0.0
        for shot in self.shots:
            if shot.start_time < previous_end - TIMELINE_ROUNDING_TOLERANCE_SECONDS:
                raise ValueError("shots must not overlap")
            if shot.start_time > previous_end + TIMELINE_ROUNDING_TOLERANCE_SECONDS:
                raise ValueError("shots must form a continuous timeline")
            if shot.end_time > self.duration_seconds:
                raise ValueError("shot end_time must not exceed duration_seconds")
            previous_end = shot.end_time
        if abs(previous_end - self.duration_seconds) > TIMELINE_ROUNDING_TOLERANCE_SECONDS:
            raise ValueError("shots must cover the full video")
        return self


class ProviderShotCard(ShotCard):
    """Strict contract for newly purchased provider analysis output.

    ``ShotCard`` remains backward-compatible for stored/manual legacy rows.
    Fresh provider responses must never silently downgrade the fields used by
    primary-subject targeting and deterministic H3 prompt compilation.
    """

    person_count: int = Field(ge=0)
    segment_kind: Literal["SHOT_CUT", "ACTION_BEAT"]
    boundary_reason: str = Field(min_length=1)
    motion: ShotMotion


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    raw: dict[str, Any]


class VideoAnalysisProvider(Protocol):
    requires_https_video_url: bool

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse: ...


class AnalysisProviderFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        failure_phase: str = RESPONSE_FAILURE_PHASE,
        retryable: bool = False,
        upstream_diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.failure_phase = failure_phase
        self.retryable = retryable
        # Structured, already-redacted provider evidence (status / phase /
        # bounded reason) so the FAILED terminal state can persist *why* the
        # provider refused instead of only the status code.
        self.upstream_diagnostic = upstream_diagnostic


class ApilioChatTransport(Protocol):
    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]: ...


# The refusal body is the only place that says *why* the provider said no, but
# providers routinely echo the request back — signed video URL included.  Strip
# anything URL-shaped and cap the length before the reason reaches a log line or
# the ``error_message_redacted`` column.
_URL_PATTERN = re.compile(r"https?://\S+")
UPSTREAM_REASON_LIMIT = 300


def redacted_upstream_reason(raw: bytes | str) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            text = error["message"]
        elif isinstance(error, str):
            text = error
        elif isinstance(payload.get("message"), str):
            text = payload["message"]
    text = " ".join(_URL_PATTERN.sub("[已脱敏地址]", text).split())
    if len(text) > UPSTREAM_REASON_LIMIT:
        text = text[:UPSTREAM_REASON_LIMIT] + "…"
    return text


class UrllibApilioChatTransport:
    # Measured on a real 15s reference video: 71–91s per call for the full
    # shot-card prompt. 90s cut real traffic in half; 240s leaves headroom for
    # slower days while the desktop timeout (300s) still bounds the wait.
    def __init__(self, *, timeout_seconds: float = 240.0) -> None:
        self.timeout_seconds = timeout_seconds

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        try:
            request = Request(url, data=body, headers=dict(headers), method="POST")
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read(), dict(response.headers.items())
        except HTTPError as exc:
            try:
                reason = redacted_upstream_reason(exc.read())
            except (OSError, ValueError, AttributeError):
                # Reading the refusal body is best-effort diagnostics; it must
                # never replace the failure it was meant to explain.
                reason = ""
            logger.warning(
                "%s Apilio video analysis request failed with HTTP status %s: %s",
                analysis_log_ref(),
                exc.code,
                reason or "(上游未返回可读原因)",
            )
            raise AnalysisProviderFailed(
                f"视频拆解服务拒绝了请求（HTTP {exc.code}）"
                + (f"：{reason}" if reason else "，且未说明原因。"),
                http_status=exc.code,
                failure_phase=HTTP_FAILURE_PHASE,
                retryable=exc.code == 429 or exc.code >= 500,
                upstream_diagnostic={
                    "http_status": exc.code,
                    "failure_phase": HTTP_FAILURE_PHASE,
                    "reason": reason or None,
                },
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            # Only the exception class name is carried forward: the original reason can
            # embed the signed video URL or the request headers.
            reason = type(exc).__name__
            logger.warning(
                "%s Apilio video analysis request failed: %s", analysis_log_ref(), reason
            )
            raise AnalysisProviderFailed(
                f"Apilio video analysis request failed ({reason})",
                failure_phase=NETWORK_FAILURE_PHASE,
                retryable=True,
                upstream_diagnostic={
                    "http_status": None,
                    "failure_phase": NETWORK_FAILURE_PHASE,
                    "reason": reason,
                },
            ) from exc


class ApilioGemini:
    """Apilio's OpenAI-compatible Gemini adapter for a signed reference-video URL."""

    requires_https_video_url = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        model: str = APILIO_ANALYSIS_MODEL,
        transport: ApilioChatTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport or UrllibApilioChatTransport()

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        if not is_https_video_url(video_uri):
            logger.warning(
                "%s Video analysis refused: the reference video URL is not HTTPS",
                analysis_log_ref(),
            )
            raise AnalysisProviderFailed(
                "参考视频没有可用的 HTTPS 签名地址，无法送去拆解；请重新上传参考视频后再试。",
                failure_phase=REQUEST_FAILURE_PHASE,
            )
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_instruction(duration_seconds)},
                        {"type": "image_url", "image_url": {"url": video_uri}},
                    ],
                }
            ],
        }
        text, raw = self._complete(payload)
        return ProviderResponse(text=text, raw=raw)

    def analyze_with_context(
        self,
        *,
        video_uri: str,
        duration_seconds: float,
        context: dict[str, Any],
        media: list[dict[str, Any]],
        analysis_guidance: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        from app.h3_prompts import RULES

        if not is_https_video_url(video_uri):
            logger.warning(
                "%s Video analysis refused: the reference video URL is not HTTPS",
                analysis_log_ref(),
            )
            raise AnalysisProviderFailed(
                "参考视频没有可用的 HTTPS 签名地址，无法送去拆解；请重新上传参考视频后再试。",
                failure_phase=REQUEST_FAILURE_PHASE,
            )
        instruction = (RULES / "analysis.txt").read_text(encoding="utf-8")
        # 目标生成时长不得进入拆解提示词：拆解只描述源视频事实，成片时长由生成
        # 阶段的 H3 API duration 参数承载；注入前剥离，防止模型把目标时长当源
        # 时长或按目标时长凑段。
        prompt_context = {key: value for key, value in context.items() if key != "duration_seconds"}
        instruction = instruction.replace(
            "{MEDIA_INFO_JSON}",
            json.dumps(
                {
                    **context.get("media_info", {}),
                    "duration_seconds": duration_seconds,
                },
                ensure_ascii=False,
            ),
        ).replace("{GENERATION_CONTEXT_JSON}", json.dumps(prompt_context, ensure_ascii=False))
        instruction = instruction.replace(
            "{SCENE_BOUNDARY_GUIDANCE_JSON}",
            json.dumps(analysis_guidance or {}, ensure_ascii=False),
        )
        text, raw = self._complete(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 16000,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": instruction},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "以下视频仅用于分析；生成参考素材另列。"},
                            {"type": "image_url", "image_url": {"url": video_uri}},
                            *media,
                        ],
                    },
                ],
            }
        )
        return ProviderResponse(text=text, raw=raw)

    def _complete(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw_body, _ = self.transport.post(
            f"{self.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(),
        )
        try:
            response = json.loads(raw_body.decode("utf-8"))
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "%s Apilio Gemini response is not a readable completion: %s",
                analysis_log_ref(),
                type(exc).__name__,
            )
            raise AnalysisProviderFailed(
                "视频拆解服务返回的数据结构无法识别；请重试或更换参考视频。",
                upstream_diagnostic={
                    "http_status": None,
                    "failure_phase": RESPONSE_FAILURE_PHASE,
                    "reason": type(exc).__name__,
                },
            ) from exc
        if not isinstance(content, str) or not content.strip():
            logger.warning(
                "%s Apilio Gemini completion carried no analysis content", analysis_log_ref()
            )
            raise AnalysisProviderFailed(
                "视频拆解服务返回了空结果；请重试或更换参考视频。",
                upstream_diagnostic={
                    "http_status": None,
                    "failure_phase": RESPONSE_FAILURE_PHASE,
                    "reason": "empty_completion",
                },
            )
        return content, {
            "provider": "apilio_gemini",
            "model": self.model,
            "response_id": response.get("id") if isinstance(response.get("id"), str) else None,
        }


@dataclass(init=False)
class FakeGemini:
    analysis_json: str | None = None
    requires_https_video_url = False

    def __init__(self, analysis_json: str | None = None) -> None:
        self.analysis_json = analysis_json

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        text = self.analysis_json or json.dumps(
            _default_analysis_payload(duration_seconds), ensure_ascii=True, sort_keys=True
        )
        return ProviderResponse(
            text=text,
            raw={"provider": "fake_gemini", "text": text},
        )


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: VideoAnalysis
    provider_response_ref: dict[str, Any]
    generation_prompt: dict[str, Any] | None = None


def analyze_video(
    *,
    video_uri: str,
    video_duration_seconds: float,
    provider: VideoAnalysisProvider,
    on_provider_result: Callable[[], None] | None = None,
    generation_context: dict[str, Any] | None = None,
    generation_media: list[dict[str, Any]] | None = None,
    analysis_guidance: dict[str, Any] | None = None,
) -> AnalysisResult:
    from app.h3_prompts import analysis_prompt_result

    if isinstance(provider, ApilioGemini) and (
        generation_context is not None or analysis_guidance is not None
    ):
        provider_context = generation_context or {
            "mode": None,
            "duration_seconds": video_duration_seconds,
            "generation_assets": [],
            "issues": [],
            "media_info": {},
        }
        response = provider.analyze_with_context(
            video_uri=video_uri,
            duration_seconds=video_duration_seconds,
            context=provider_context,
            media=generation_media or [],
            analysis_guidance=analysis_guidance,
        )
    else:
        response = provider.analyze(video_uri=video_uri, duration_seconds=video_duration_seconds)
    if on_provider_result is not None:
        on_provider_result()
    try:
        payload = json.loads(response.text)
        candidate = None
        if isinstance(payload, dict) and payload.get("schema_version") == "analysis-h3.v1":
            candidate = payload.get("generation_prompt")
            payload = payload.get("analysis")
        if not isinstance(payload, dict):
            raise ValueError("analysis must be an object")
        if generation_context is not None:
            for key, value in generation_context.get("media_info", {}).items():
                if key in ("fps", "resolution", "aspect_ratio"):
                    payload[key] = value
        analysis = parse_analysis_response(
            json.dumps(payload), duration_seconds=video_duration_seconds
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        logger.warning("Video analysis validation failed: %s", _validation_diagnostic(exc))
        raise AnalysisProviderFailed(
            "拆解结果格式无效，未自动调用付费修复；请检查后主动重试。"
        ) from exc
    generation_prompt = None
    if generation_context is not None:
        try:
            generation_prompt = analysis_prompt_result(
                candidate, context=generation_context, analysis=analysis.model_dump(mode="json")
            )
        except (ValidationError, ValueError, TypeError):
            generation_prompt = {
                "mode": generation_context["mode"],
                "status": "INVALID",
                "prompt_text": None,
                "issues": [
                    {
                        "code": "H3_RESPONSE_INVALID",
                        "message": "H3 提示词格式无效，已保留分镜分析。",
                    }
                ],
            }
    return AnalysisResult(
        analysis=analysis,
        generation_prompt=generation_prompt,
        provider_response_ref=_provider_response_ref(response.raw),
    )


def parse_analysis_response(text: str, *, duration_seconds: float) -> VideoAnalysis:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("analysis response must be a JSON object")
    _normalize_timeline_rounding(payload, duration_seconds=duration_seconds)
    payload["duration_seconds"] = duration_seconds
    shots = payload.get("shots")
    if isinstance(shots, list):
        for shot in shots:
            ProviderShotCard.model_validate(shot)
    return _with_original_script_fallback(VideoAnalysis.model_validate(payload))


def _with_original_script_fallback(analysis: VideoAnalysis) -> VideoAnalysis:
    """校验通过后，空 original_script 用分段台词回填。

    服务商偶尔把口播正确切进各段 ``spoken_text``，却把全片 ``original_script``
    留成空串；不兜底时爆款复刻页文案栏空白（前端直用空串）。这里与
    ``generation.py`` 的 ``full_text = original_script or "".join(spoken_texts)``
    编译兜底语义对齐，前端展示、提示词编译与历史恢复因此拿到同一份文案。

    源片确实无口播（分段台词也为空）时保持空字符串，不制造假文案。
    """
    if analysis.original_script.strip():
        return analysis
    joined = "".join(shot.spoken_text for shot in analysis.shots)
    if not joined.strip():
        return analysis
    return analysis.model_copy(update={"original_script": joined})


def _normalize_timeline_rounding(
    payload: dict[str, Any],
    *,
    duration_seconds: float,
) -> None:
    """Snap harmless provider rounding to the ffprobe-canonical timeline.

    The prompt and ffprobe can represent the same instant with different
    decimal precision (for example 12.067 versus 12.066667).  Only boundaries
    within a small frame-scale window are adjusted; larger overlaps and overruns remain schema
    errors so genuinely broken analyses still fail closed.
    """
    shots = payload.get("shots")
    if not isinstance(shots, list) or not shots:
        return

    previous_end = 0.0
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        start_time = _finite_number(shot.get("start_time"))
        if start_time is not None:
            expected_start = 0.0 if index == 0 else previous_end
            if abs(start_time - expected_start) <= TIMELINE_ROUNDING_TOLERANCE_SECONDS:
                shot["start_time"] = expected_start
        end_time = _finite_number(shot.get("end_time"))
        if end_time is not None:
            if index == len(shots) - 1 and (
                abs(end_time - duration_seconds) <= TIMELINE_ROUNDING_TOLERANCE_SECONDS
            ):
                end_time = duration_seconds
                shot["end_time"] = duration_seconds
            previous_end = end_time


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _canonical_duration_text(duration_seconds: float) -> str:
    return f"{duration_seconds:.6f}".rstrip("0").rstrip(".")


def _validation_diagnostic(exc: Exception) -> str:
    """Return field/type-only diagnostics without logging provider content."""
    if isinstance(exc, ValidationError):
        issues: list[str] = []
        for item in exc.errors(include_url=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "root"
            issues.append(f"{location}:{item.get('type', 'validation_error')}")
        return "validation:" + ",".join(issues)
    if isinstance(exc, json.JSONDecodeError):
        return f"json_decode:line={exc.lineno}:column={exc.colno}"
    return type(exc).__name__


def is_https_video_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def analysis_instruction(duration_seconds: float) -> str:
    canonical_duration = _canonical_duration_text(duration_seconds)
    return (
        "分析这条参考短视频，只返回合法 JSON 对象，不要 markdown 代码块。\n"
        "JSON 结构：summary, aspect_ratio, resolution, fps, theme, visual_style, "
        "pace, camera_language, color_tone, original_script, shots。\n"
        "shots 内每个镜头必须包含 shot_id, start_time, end_time, shot_type, "
        "composition, camera_motion, subject, person_count, action, scene, "
        "scene_dressing, scene_lighting, wardrobe_pose_detail, ambient_sound, "
        "music_style_hint, spoken_text, transition, motion, segment_kind, "
        "boundary_reason。shots 的业务含义是可执行时间段，"
        "既可以来自真实剪辑切点，也可以来自同一连续镜头内的动作或语义阶段变化；"
        "时间段必须从 0 秒开始、连续覆盖全片、互不重叠且不得留空洞。\n"
        f"已验证的视频总时长为 {canonical_duration} 秒；最后一个镜头的 end_time "
        f"必须精确等于 {canonical_duration}。\n"
        "除 shot_id 和枚举值外，所有文本字段一律用中文填写。\n"
        "\n"
        "motion 是结构化运动描述，每个镜头都必须完整填写以下六个字段：\n"
        "- subject_motion_state（人物运动状态，枚举）：STATIC 静止 / WALKING 行走 / "
        "RUNNING 跑动 / TURNING 转身 / GESTURING_ONLY 仅手势站位不动 / "
        "OBJECT_MOTION 仅物体运动 / NO_PERSON 无人物；并列主态用“+”连接，"
        "如 WALKING+GESTURING_ONLY（边走边做手势），位移与手势同等主导时"
        "不得只留其一；\n"
        "- subject_direction（人物位移方向，枚举）：toward_camera 向镜头 / "
        "away_from_camera 背离镜头 / left 向画面左 / right 向画面右 / lateral 横向 / "
        "in_place 原地 / none 无；\n"
        "- subject_displacement（位移幅度，中文）：如“向镜头走近两三步”“无位移”；\n"
        "- hand_action（左右手动作，中文）：如“双臂随步态交替自然摆动，不指点不握拳”；\n"
        "- camera_motion（机位运动，枚举）：STATIC 固定 / PUSH_IN 推近 / PULL_BACK 拉远 / "
        "HANDHELD_TRACKING 手持跟拍 / PAN 横摇 / TILT 纵摇 / FOLLOW 跟随 / "
        "ORBIT 环绕 / CRANE_UP 升镜 / CRANE_DOWN 降镜 / ZOOM_IN 光学变焦推近 / "
        "ZOOM_OUT 光学变焦拉远；机位在动时禁止填 STATIC；\n"
        "- relative_motion（人物与摄影机相对运动，中文）：如“人物逐渐靠近镜头，画面占比增大”。\n"
        "\n"
        "color_tone（全片主色调，中文，如“暖橙偏黄，高对比”）用于统一整条视频的视觉风格锚点。\n"
        "scene_dressing（该镜头背景陈设/道具，中文，如“木质工作台，墙面挂满工具”）、"
        "scene_lighting（该镜头光线方向与质感，中文，如“侧逆光，硬光源，高反差”）、"
        "ambient_sound（该镜头环境音，中文，如“雨声，远处车流”）、"
        "music_style_hint（该镜头配乐风格倾向，中文，如“轻电子，节奏偏快”）"
        "均需逐镜头填写，不得只写“无”敷衍，除非画面确实无背景陈设/无环境声可辨识。\n"
        "wardrobe_pose_detail（该镜头人物非身份类外观细节，中文）：只描述服装款式、"
        "配饰、姿态动作等不涉及身份识别的信息，严禁描述面部长相、五官、发型等身份特征；"
        "无人物出镜的镜头写“无人物出镜”。\n"
        "\n"
        "关键规则：\n"
        "-1. person_count 必须统计该时间段画面内所有可见真人（包括局部露出者）；"
        "同一人的镜面反射不重复计数，海报、照片和屏幕中的人物不计数。\n"
        "0. 先逐段核对全片的真实剪辑边界，再在连续镜头内按清晰的动作、"
        "站位或运镜阶段变化细分。段数服从实际内容，不设固定目标；"
        "不得为了凑数制造不存在的切镜或动作。真实切镜填写 "
        "segment_kind=SHOT_CUT，同镜头内阶段变化填写 segment_kind=ACTION_BEAT，"
        "boundary_reason 用中文说明拆分原因。\n"
        "1. 人物位移、行走、寻找目标、走近或离开画面是首要产出，必须优先记录；"
        "分段边界不得默认落在静止姿态上。运动状态发生转换（如行走→停下、"
        "转身→面向镜头、寻找→发现）时应当分段，不要合并成一段笼统描述。\n"
        "2. 人物在镜头内移动（行走、跑动、转身）时，subject_motion_state 必须选对应"
        "运动状态，action 必须写明运动方向与幅度；不得把移动中的人物概括成“说话”或"
        "“站立”，也不得把运动镜头写成固定机位。\n"
        "3. 人物确实静止时选 STATIC，不得凭空增加运动。\n"
        "4. 无人物出镜的镜头 subject_motion_state 选 NO_PERSON 或 OBJECT_MOTION，"
        "文本字段写“无人物出镜”。\n"
        "5. wardrobe_pose_detail 一旦出现任何面部/五官/发型描述视为不合规，必须"
        "改写为纯服装、配饰、姿态描述。"
    )


def create_analysis_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    asset_uri: str,
    created_by_user_id: str,
    result: AnalysisResult,
    commit: bool = True,
) -> sqlite3.Row:
    return insert_version(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        kind=ANALYSIS_KIND,
        created_by_user_id=created_by_user_id,
        payload=analysis_version_payload(
            asset_id=asset_id,
            asset_uri=asset_uri,
            result=result,
        ),
        commit=commit,
    )


def create_or_recover_analysis_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    asset_uri: str,
    created_by_user_id: str,
    result: AnalysisResult,
) -> tuple[sqlite3.Row, bool]:
    """Create one analysis version, or reuse the winner of a concurrent recovery."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = find_analysis_version_for_asset(
            conn,
            project_id=project_id,
            asset_id=asset_id,
        )
        if existing is not None:
            conn.commit()
            return existing, False
        row = _insert_version(
            conn,
            project_id=project_id,
            asset_id=asset_id,
            kind=ANALYSIS_KIND,
            created_by_user_id=created_by_user_id,
            payload=analysis_version_payload(
                asset_id=asset_id,
                asset_uri=asset_uri,
                result=result,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row, True


def find_analysis_version_for_asset(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json,
               created_by_user_id, created_at
        FROM versions
        WHERE project_id = %s AND asset_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, asset_id, ANALYSIS_KIND),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def analysis_version_payload(
    *,
    asset_id: str,
    asset_uri: str,
    result: AnalysisResult,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis": result.analysis.model_dump(mode="json"),
        "source_asset": {"id": asset_id, "storage_uri": asset_uri},
        "provider_response_ref": result.provider_response_ref,
        "generation_prompt": result.generation_prompt,
    }


def create_shot_card_version(
    conn: BusinessConnection,
    *,
    analysis_version: sqlite3.Row,
    created_by_user_id: str,
    shots: list[ShotCard],
    commit: bool = True,
) -> sqlite3.Row:
    source_payload = json.loads(str(analysis_version["payload_json"]))
    analysis_payload = source_payload["analysis"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_analysis_version_id": str(analysis_version["id"]),
        "duration_seconds": analysis_payload["duration_seconds"],
        # 顶层风格字段随镜头卡一起落库，否则 H3 Prompt 编译层的 style 段
        # 在正式保存后的镜头卡上永远读不到（分析版本本身不会再被读取）。
        "theme": analysis_payload.get("theme"),
        "visual_style": analysis_payload.get("visual_style"),
        "pace": analysis_payload.get("pace"),
        "camera_language": analysis_payload.get("camera_language"),
        "color_tone": analysis_payload.get("color_tone"),
        "shots": [shot.model_dump(mode="json") for shot in shots],
    }
    return insert_version(
        conn,
        project_id=str(analysis_version["project_id"]),
        asset_id=None
        if analysis_version["asset_id"] is None
        else str(analysis_version["asset_id"]),
        kind=SHOT_CARD_KIND,
        created_by_user_id=created_by_user_id,
        payload=payload,
        commit=commit,
    )


def validate_shot_cards(shots: list[dict[str, Any]], *, duration_seconds: float) -> list[ShotCard]:
    analysis = VideoAnalysis(
        summary="manual shot cards",
        duration_seconds=duration_seconds,
        aspect_ratio="manual",
        resolution="manual",
        fps=1,
        theme="manual",
        visual_style="manual",
        pace="manual",
        camera_language="manual",
        original_script="",
        shots=[ShotCard.model_validate(shot) for shot in shots],
    )
    return analysis.shots


def insert_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str | None,
    kind: str,
    created_by_user_id: str,
    payload: dict[str, Any],
    commit: bool = True,
) -> sqlite3.Row:
    row = _insert_version(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        kind=kind,
        created_by_user_id=created_by_user_id,
        payload=payload,
    )
    if commit:
        conn.commit()
    return row


def find_latest_analysis_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM analysis_tasks
        WHERE project_id = %s AND asset_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def enqueue_analysis_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    created_by_user_id: str,
    duration_seconds: float,
    generation_context: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """Enqueue one analysis task, stamping the caller's request id.

    ``request_id`` is the API request that asked for the analysis (P0-1
    logging correlation key). It travels with the task row so the desktop
    failure card and support can name both the task and the request that
    created it, instead of grepping the API log first.
    """
    if generation_context is None:
        from app.auth import CurrentUser, Role
        from app.h3_prompts import GenerationContext
        from app.prompt_context import resolve_context

        user = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE id=%s", (created_by_user_id,)
        ).fetchone()
        if user is None:
            raise ValueError("analysis owner no longer exists")
        actor = CurrentUser(
            id=str(user["id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=cast(Role, str(user["role"])),
        )
        generation_context = resolve_context(
            conn,
            actor=actor,
            request=GenerationContext(project_id=project_id, source_asset_id=asset_id),
        )
        asset = conn.execute("SELECT metadata_json FROM assets WHERE id=%s", (asset_id,)).fetchone()
        metadata = json.loads(str(asset["metadata_json"] or "{}")) if asset else {}
        generation_context["media_info"] = {
            key: metadata[key]
            for key in ("fps", "resolution", "aspect_ratio")
            if metadata.get(key) is not None
        }
    existing = conn.execute(
        """
        SELECT * FROM analysis_tasks
        WHERE project_id = %s AND asset_id = %s
          AND status IN ('PENDING', 'RUNNING')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    if existing is not None:
        if json.loads(str(existing["generation_context_json"] or "null")) != generation_context:
            from fastapi import HTTPException

            raise HTTPException(
                409,
                detail={
                    "code": "ANALYSIS_CONTEXT_CONFLICT",
                    "message": "该视频正在按其他素材或参数拆解，请等待当前任务完成后重试。",
                },
            )
        return existing, False

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO analysis_tasks (
            id, project_id, asset_id, created_by_user_id,
            duration_seconds, status, generation_context_json, request_id
        ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            asset_id,
            created_by_user_id,
            duration_seconds,
            json.dumps(generation_context) if generation_context is not None else None,
            request_id,
        ),
    )
    inserted = conn.execute(
        "SELECT * FROM analysis_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if inserted is not None:
        from app.usage_billing import accept_operation

        accept_operation(
            conn, user_id=created_by_user_id, service="analysis", source_id=task_id, units=1
        )
        return inserted, True

    concurrent = conn.execute(
        """
        SELECT * FROM analysis_tasks
        WHERE project_id = %s AND asset_id = %s
          AND status IN ('PENDING', 'RUNNING')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    if concurrent is None:
        raise RuntimeError("analysis task enqueue conflict left no active task")
    if json.loads(str(concurrent["generation_context_json"] or "null")) != generation_context:
        from fastapi import HTTPException

        raise HTTPException(
            409,
            detail={
                "code": "ANALYSIS_CONTEXT_CONFLICT",
                "message": "该视频正在按其他素材或参数拆解，请等待当前任务完成后重试。",
            },
        )
    return concurrent, False


def _insert_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str | None,
    kind: str,
    created_by_user_id: str,
    payload: dict[str, Any],
) -> sqlite3.Row:
    version_number = next_version_number(conn, project_id=project_id, kind=kind)
    version_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO versions (
            id,
            project_id,
            asset_id,
            kind,
            version_number,
            payload_json,
            created_by_user_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version_id,
            project_id,
            asset_id,
            kind,
            version_number,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            created_by_user_id,
        ),
    )
    return get_version(conn, version_id)


def get_version(conn: BusinessConnection, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE id = %s
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise LookupError("version not found")
    return cast(sqlite3.Row, row)


def next_version_number(conn: BusinessConnection, *, project_id: str, kind: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = %s AND kind = %s
        """,
        (project_id, kind),
    ).fetchone()
    return int(row[0])


def _provider_response_ref(raw_response: dict[str, Any]) -> dict[str, Any]:
    return {"stored_as": "versions.payload_json", "raw": raw_response}


def _default_analysis_payload(duration_seconds: float) -> dict[str, Any]:
    midpoint = duration_seconds / 2
    return {
        "summary": "FakeGemini 演示拆解（未配置真实分析服务）",
        "duration_seconds": duration_seconds,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 30,
        "theme": "人物口播",
        "visual_style": "写实竖屏",
        "pace": "快",
        "camera_language": "近景为主",
        "original_script": "",
        "shots": [
            {
                "shot_id": "S01",
                "start_time": 0,
                "end_time": midpoint,
                "shot_type": "中景",
                "composition": "人物居中",
                "camera_motion": "手持跟拍",
                "subject": "主讲人",
                "person_count": 1,
                "action": "边向镜头走近边口播",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
                "segment_kind": "ACTION_BEAT",
                "boundary_reason": "开场建立人物与场景",
                "motion": {
                    "subject_motion_state": "WALKING",
                    "subject_direction": "toward_camera",
                    "subject_displacement": "向镜头走近两三步",
                    "hand_action": "双臂随步态交替自然摆动，不指点不握拳",
                    "camera_motion": "HANDHELD_TRACKING",
                    "relative_motion": "人物逐渐靠近镜头，画面占比增大",
                },
            },
            {
                "shot_id": "S02",
                "start_time": midpoint,
                "end_time": duration_seconds,
                "shot_type": "近景",
                "composition": "三分法",
                "camera_motion": "缓慢推近",
                "subject": "主讲人",
                "person_count": 1,
                "action": "站位固定，边做讲解手势边口播",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
                "segment_kind": "ACTION_BEAT",
                "boundary_reason": "人物动作与运镜阶段发生变化",
                "motion": {
                    "subject_motion_state": "GESTURING_ONLY",
                    "subject_direction": "in_place",
                    "subject_displacement": "无位移",
                    "hand_action": "双手在胸前做自然讲解手势",
                    "camera_motion": "PUSH_IN",
                    "relative_motion": "镜头缓慢推近，人物站位不变",
                },
            },
        ],
    }
