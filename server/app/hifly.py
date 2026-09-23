"""Digital-human (数字人口播) provider client (C1 / 未接通能力拆解).

Implements the V2 protocol at https://api.hifly.cc/hifly.html. Current responses
use top-level fields and catalog arrays; legacy nested payloads remain accepted.
Vendor business codes map to one ``HiflyError`` for the service layer.

The transport mirrors the Metaso provider in ``app.generation`` (stdlib
urllib, injectable for tests). Credentials come exclusively from the
encrypted provider-settings storage — never from code or the environment.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, SettingsUnavailableError

HIFLY_BASE_URL = "https://hfw-api.hifly.cc"

AVATAR_CREATE_BY_VIDEO_PATH = "/api/v2/hifly/avatar/create_by_video"
AVATAR_CREATE_BY_IMAGE_PATH = "/api/v2/hifly/avatar/create_by_image"
AVATAR_TASK_PATH = "/api/v2/hifly/avatar/task"
AVATAR_LIST_PATH = "/api/v2/hifly/avatar/list"
VOICE_CREATE_PATH = "/api/v2/hifly/voice/create"
VOICE_EDIT_PATH = "/api/v2/hifly/voice/edit"
VOICE_LIST_PATH = "/api/v2/hifly/voice/list"
VOICE_TASK_PATH = "/api/v2/hifly/voice/task"
VIDEO_CREATE_BY_AUDIO_PATH = "/api/v2/hifly/video/create_by_audio"
VIDEO_CREATE_BY_TTS_PATH = "/api/v2/hifly/video/create_by_tts"
AUDIO_CREATE_BY_TTS_PATH = "/api/v2/hifly/audio/create_by_tts"
VIDEO_TASK_PATH = "/api/v2/hifly/video/task"
TOOL_CREATE_UPLOAD_URL_PATH = "/api/v2/hifly/tool/create_upload_url"
ACCOUNT_CREDIT_PATH = "/api/v2/hifly/account/credit"

# Vendor task-status integers (docs §2): 1 waiting / 2 processing / 3 done / 4 failed.
VendorTaskStatus = Literal["WAITING", "PROCESSING", "DONE", "FAILED", "UNKNOWN"]

_MAX_TITLE_CHARS = 20
_MAX_TTS_TEXT_CHARS = 10_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

logger = logging.getLogger(__name__)


class HiflyError(RuntimeError):
    """A HiFly call failed at the transport or vendor-business level.

    ``vendor_code`` carries the vendor envelope ``code`` when the failure came
    from the business envelope (1002 insufficient credits, 2003 invalid token,
    …) so callers can branch on known conditions.
    """

    def __init__(
        self,
        message: str,
        *,
        vendor_code: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.vendor_code = vendor_code
        self.http_status = http_status


class HiflySubmissionUncertain(HiflyError):
    """A create POST may have reached the vendor but no receipt was obtained."""


class HiflyTimeoutError(HiflyError):
    """A HiFly request exceeded the configured transport deadline."""


def validate_oral_subtitle(subtitle: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate the documented subtitle fields without accepting provider overrides."""
    if subtitle is None:
        return None
    if not isinstance(subtitle, Mapping):
        raise HiflyError("字幕参数必须为对象")
    integer_fields = {
        "st_font_size": (1, 100),
        "st_width": (0, 1920),
        "st_height": (0, 1080),
        "st_x": (None, None),
        "st_y": (None, None),
    }
    colors = {"st_primary_color", "st_outline_color"}
    supported = {"st_show", "st_font_name", *integer_fields, *colors}
    if any(key not in supported for key in subtitle):
        raise HiflyError("字幕参数包含不支持的字段")
    for key, value in subtitle.items():
        valid = False
        if key == "st_show":
            valid = type(value) is bool or (type(value) is int and value in (0, 1))
        elif key == "st_font_name":
            valid = isinstance(value, str) and bool(value.strip())
        elif key in colors:
            valid = (
                isinstance(value, str)
                and re.fullmatch(r"0x[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value) is not None
            )
        elif key in integer_fields:
            lower, upper = integer_fields[key]
            valid = (
                type(value) is int
                and (lower is None or value >= lower)
                and (upper is None or value <= upper)
            )
        if not valid:
            raise HiflyError("字幕参数类型或取值无效")
    return dict(subtitle)


class HiflySettingsUnavailable(RuntimeError):
    """The encrypted provider settings are missing or unreadable."""


_VENDOR_CODE_MESSAGES: dict[int, str] = {
    11: "数字人接口参数校验未通过，请检查提交内容",
    14: "数字人接口未找到对应资源",
    1001: "数字人任务并发已达上限，请稍后重试",
    1002: "数字人服务余额不足，请联系管理员",
    1005: "当前服务套餐不支持该能力（如照片制作分身）",
    1006: "当前服务套餐权限不足",
    1009: "声音克隆数量已达套餐上限",
    1011: "该素材未通过数字人服务合规校验",
    1013: "声音克隆被限制，请稍后重试",
    1015: "数字人任务提交数已达上限，请稍后重试",
    2003: "数字人服务未正确配置，请联系管理员",
}


def _vendor_message(code: int, msg: str) -> str:
    readable = _VENDOR_CODE_MESSAGES.get(code)
    if readable:
        return readable
    return f"数字人服务返回错误 {code}: {msg}"[:200]


class HiflyHttpTransport:
    """Minimal HTTP surface the client needs (mirrors the Metaso transport)."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        raise NotImplementedError


class UrllibHiflyHttpTransport(HiflyHttpTransport):
    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        try:
            request = Request(url, data=body, headers=dict(headers), method=method)
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return cast(bytes, response.read())
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:1000].decode("utf-8", "replace")
            except OSError:
                pass
            if exc.code == 429:
                # 上游限流要和"我们配错了/参数非法"区分开：前者是对方在压流，
                # 后者要人改配置。把 Retry-After 一并记下来，运维才知道该等多久。
                # 真正的退避重排属于任务重投机制的改造，不在本次范围内；这里
                # 至少保证信号可见，且 429 仍以 http_status 透出，便于调用方
                # 将来据此做退避。
                retry_after = None
                if exc.headers is not None:
                    retry_after = exc.headers.get("Retry-After")
                logger.warning(
                    "ORAL vendor rate limited (HTTP 429, retry_after=%s): %s",
                    retry_after or "unspecified",
                    detail,
                )
            else:
                logger.warning(
                    "ORAL vendor request failed with HTTP status %s: %s", exc.code, detail
                )
            if exc.code in {408, 504}:
                raise HiflyTimeoutError("数字人服务请求超时，请稍后重试") from exc
            raise HiflyError(f"数字人服务返回 HTTP {exc.code}", http_status=exc.code) from exc
        except TimeoutError as exc:
            logger.warning("ORAL vendor request timed out")
            raise HiflyTimeoutError("数字人服务请求超时，请稍后重试") from exc
        except URLError as exc:
            logger.warning("ORAL vendor request failed: %s", type(exc.reason).__name__)
            if isinstance(exc.reason, TimeoutError):
                raise HiflyTimeoutError("数字人服务请求超时，请稍后重试") from exc
            raise HiflyError("数字人服务网络异常，请稍后重试") from exc
        except OSError as exc:
            logger.warning("ORAL vendor request failed: %s", type(exc).__name__)
            raise HiflyError("数字人服务网络异常，请稍后重试") from exc


@dataclass(frozen=True)
class HiflyUploadTarget:
    upload_url: str
    content_type: str
    file_id: str


@dataclass(frozen=True)
class HiflyAvatarTaskSnapshot:
    status: VendorTaskStatus
    avatar_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class HiflyVoiceTaskSnapshot:
    status: VendorTaskStatus
    voice: str | None
    demo_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class HiflyVideoTaskSnapshot:
    status: VendorTaskStatus
    video_url: str | None
    duration: int | None
    raw: dict[str, Any]


_VENDOR_STATUS_NAMES: dict[int, VendorTaskStatus] = {
    1: "WAITING",
    2: "PROCESSING",
    3: "DONE",
    4: "FAILED",
}


def _vendor_status(status: Any) -> VendorTaskStatus:
    try:
        return _VENDOR_STATUS_NAMES[int(status)]
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


class HiflyClient:
    """Typed HiFly V2 client. One instance per configured provider token."""

    def __init__(
        self,
        *,
        api_key: str,
        transport: HiflyHttpTransport | None = None,
        base_url: str = HIFLY_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.transport = transport or UrllibHiflyHttpTransport()
        self.base_url = base_url.rstrip("/")

    # -- envelope -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        body = (
            json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            content = self.transport.request(method, url, headers=headers, body=body)
        except HiflyError:
            raise
        except Exception as exc:  # noqa: BLE001 - third-party transports may raise anything
            logger.warning("ORAL vendor request failed: %s", type(exc).__name__)
            raise HiflyError("数字人服务网络异常，请稍后重试") from exc
        try:
            envelope = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HiflyError("数字人服务返回了无法解析的响应") from exc
        if not isinstance(envelope, dict):
            raise HiflyError("数字人服务响应缺少业务状态码")
        if "code" not in envelope:
            # Only the documented upload-ticket endpoint omits code. Its
            # mandatory ticket fields are validated by create_upload_url.
            if path == TOOL_CREATE_UPLOAD_URL_PATH:
                return envelope
            raise HiflyError("数字人服务响应缺少业务状态码")
        raw_code = envelope["code"]
        if isinstance(raw_code, bool):
            raise HiflyError("数字人服务响应的业务状态码无效")
        if isinstance(raw_code, int):
            code = raw_code
        elif isinstance(raw_code, str):
            normalized_code = raw_code.strip()
            digits = normalized_code[1:] if normalized_code[:1] in {"+", "-"} else normalized_code
            if not digits or not digits.isascii() or not digits.isdigit():
                raise HiflyError("数字人服务响应的业务状态码无效")
            try:
                code = int(normalized_code)
            except ValueError as exc:
                raise HiflyError("数字人服务响应的业务状态码无效") from exc
        else:
            raise HiflyError("数字人服务响应的业务状态码无效")
        if code != 0:
            raise HiflyError(
                _vendor_message(code, str(envelope.get("message") or envelope.get("msg", ""))),
                vendor_code=code,
            )
        data = envelope.get("data")
        return data if isinstance(data, dict) else envelope

    def _creation_request(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._request("POST", path, payload=payload)
        except HiflyError as exc:
            if exc.vendor_code is not None:
                raise
            raise HiflySubmissionUncertain(str(exc)) from exc

    # -- 数字人（分身） -------------------------------------------------------

    def create_avatar_by_video(
        self,
        *,
        title: str,
        video_url: str | None = None,
        file_id: str | None = None,
        aigc_flag: bool,
    ) -> str:
        return self._create_avatar(
            AVATAR_CREATE_BY_VIDEO_PATH,
            title=title,
            video_url=video_url,
            file_id=file_id,
            aigc_flag=aigc_flag,
        )

    def create_avatar_by_image(
        self,
        *,
        title: str,
        image_url: str | None = None,
        file_id: str | None = None,
        aigc_flag: bool,
    ) -> str:
        # 企业专属会员能力（vendor code 1005/1006）；服务层按会员能力开放入口。
        return self._create_avatar(
            AVATAR_CREATE_BY_IMAGE_PATH,
            title=title,
            video_url=image_url,
            file_id=file_id,
            aigc_flag=aigc_flag,
        )

    def _create_avatar(
        self,
        path: str,
        *,
        title: str,
        video_url: str | None,
        file_id: str | None,
        aigc_flag: bool,
    ) -> str:
        clean_title = _require_text(title, "title")
        if len(clean_title) > _MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {_MAX_TITLE_CHARS} characters")
        if bool(video_url) == bool(file_id):
            raise ValueError("exactly one of video_url or file_id is required")
        payload: dict[str, Any] = {"title": clean_title, "aigc_flag": int(aigc_flag)}
        if video_url:
            source_field = "image_url" if path == AVATAR_CREATE_BY_IMAGE_PATH else "video_url"
            payload[source_field] = video_url
        if file_id:
            payload["file_id"] = file_id
        data = self._creation_request(path, payload)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HiflySubmissionUncertain("数字人服务未返回克隆任务凭证")
        return task_id

    def avatar_task(self, task_id: str) -> HiflyAvatarTaskSnapshot:
        data = self._request(
            "GET", AVATAR_TASK_PATH, query={"task_id": _require_text(task_id, "task_id")}
        )
        avatar_id = data.get("avatar") or data.get("avatar_id")
        return HiflyAvatarTaskSnapshot(
            status=_vendor_status(data.get("status")),
            avatar_id=avatar_id if isinstance(avatar_id, str) else None,
            raw=data,
        )

    def list_avatars(self, *, page: int = 1, size: int = 10) -> list[dict[str, Any]]:
        data = self._request(
            "GET", AVATAR_LIST_PATH, query={"page": max(1, page), "size": max(1, size), "kind": 2}
        )
        rows = data.get("data", data.get("list"))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    # -- 声音 ----------------------------------------------------------------

    def create_voice(
        self,
        *,
        title: str,
        audio_url: str | None = None,
        file_id: str | None = None,
        voice_type: int = 8,
    ) -> str:
        clean_title = _require_text(title, "title")
        if len(clean_title) > _MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {_MAX_TITLE_CHARS} characters")
        if bool(audio_url) == bool(file_id):
            raise ValueError("exactly one of audio_url or file_id is required")
        payload: dict[str, Any] = {"title": clean_title, "voice_type": voice_type}
        if audio_url:
            payload["audio_url"] = audio_url
        if file_id:
            payload["file_id"] = file_id
        data = self._creation_request(VOICE_CREATE_PATH, payload)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HiflySubmissionUncertain("数字人服务未返回声音任务凭证")
        return task_id

    def edit_voice(self, *, voice: str, rate: str, volume: str, pitch: str) -> None:
        self._request(
            "POST",
            VOICE_EDIT_PATH,
            payload={
                "voice": _require_text(voice, "voice"),
                "rate": str(rate),
                "volume": str(volume),
                "pitch": str(pitch),
            },
        )

    def list_voices(
        self, *, page: int = 1, size: int = 10, kind: int | None = None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"page": max(1, page), "size": max(1, size)}
        if kind is not None:
            query["kind"] = kind
        data = self._request("GET", VOICE_LIST_PATH, query=query)
        rows = data.get("data", data.get("list"))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def voice_task(self, task_id: str) -> HiflyVoiceTaskSnapshot:
        data = self._request(
            "GET", VOICE_TASK_PATH, query={"task_id": _require_text(task_id, "task_id")}
        )
        return HiflyVoiceTaskSnapshot(
            status=_vendor_status(data.get("status")),
            voice=data.get("voice") if isinstance(data.get("voice"), str) else None,
            demo_url=data.get("demo_url") if isinstance(data.get("demo_url"), str) else None,
            raw=data,
        )

    # -- 创作（口播视频 / 音频） ---------------------------------------------

    def create_video_by_audio(
        self,
        *,
        avatar: str,
        title: str,
        audio_url: str | None = None,
        file_id: str | None = None,
        aigc_flag: bool,
    ) -> str:
        clean_title = _require_text(title, "title")
        if len(clean_title) > _MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {_MAX_TITLE_CHARS} characters")
        if bool(audio_url) == bool(file_id):
            raise ValueError("exactly one of audio_url or file_id is required")
        payload: dict[str, Any] = {
            "avatar": _require_text(avatar, "avatar"),
            "title": clean_title,
            "aigc_flag": int(aigc_flag),
        }
        if audio_url:
            payload["audio_url"] = audio_url
        if file_id:
            payload["file_id"] = file_id
        data = self._creation_request(VIDEO_CREATE_BY_AUDIO_PATH, payload)
        return self._extract_task_id(data)

    def create_video_by_tts(
        self,
        *,
        voice: str,
        text: str,
        avatar: str,
        title: str,
        aigc_flag: bool,
        subtitle: Mapping[str, Any] | None = None,
    ) -> str:
        clean_title = _require_text(title, "title")
        if len(clean_title) > _MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {_MAX_TITLE_CHARS} characters")
        clean_text = _require_text(text, "text")
        if len(clean_text) > _MAX_TTS_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TTS_TEXT_CHARS} characters")
        payload: dict[str, Any] = {
            "voice": _require_text(voice, "voice"),
            "text": clean_text,
            "avatar": _require_text(avatar, "avatar"),
            "title": clean_title,
            "aigc_flag": int(aigc_flag),
        }
        clean_subtitle = validate_oral_subtitle(subtitle)
        if clean_subtitle:
            payload.update(clean_subtitle)
        data = self._creation_request(VIDEO_CREATE_BY_TTS_PATH, payload)
        return self._extract_task_id(data)

    def create_audio_by_tts(self, *, voice: str, text: str, title: str) -> str:
        clean_title = _require_text(title, "title")
        if len(clean_title) > _MAX_TITLE_CHARS:
            raise ValueError(f"title must be at most {_MAX_TITLE_CHARS} characters")
        clean_text = _require_text(text, "text")
        if len(clean_text) > _MAX_TTS_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TTS_TEXT_CHARS} characters")
        data = self._creation_request(
            AUDIO_CREATE_BY_TTS_PATH,
            {
                "voice": _require_text(voice, "voice"),
                "text": clean_text,
                "title": clean_title,
            },
        )
        return self._extract_task_id(data)

    def video_task(self, task_id: str) -> HiflyVideoTaskSnapshot:
        data = self._request(
            "GET", VIDEO_TASK_PATH, query={"task_id": _require_text(task_id, "task_id")}
        )
        # The vendor spells the field ``video_Url``; accept the sane casing too.
        video_url = data.get("video_Url") or data.get("video_url")
        duration = data.get("duration")
        return HiflyVideoTaskSnapshot(
            status=_vendor_status(data.get("status")),
            video_url=video_url if isinstance(video_url, str) else None,
            duration=duration if isinstance(duration, int) else None,
            raw=data,
        )

    # -- 系统 ----------------------------------------------------------------

    def create_upload_url(self, file_extension: str) -> HiflyUploadTarget:
        extension = _require_text(file_extension, "file_extension").lstrip(".")
        data = self._request(
            "POST", TOOL_CREATE_UPLOAD_URL_PATH, payload={"file_extension": extension}
        )
        upload_url = data.get("upload_url")
        content_type = data.get("content_type")
        file_id = data.get("file_id")
        if not all(
            isinstance(value, str) and value for value in (upload_url, content_type, file_id)
        ):
            raise HiflyError("数字人服务上传凭证响应不完整")
        return HiflyUploadTarget(
            upload_url=cast(str, upload_url),
            content_type=cast(str, content_type),
            file_id=cast(str, file_id),
        )

    def account_credit(self) -> int:
        data = self._request("GET", ACCOUNT_CREDIT_PATH)
        credit = data.get("left", data.get("credit"))
        if (
            isinstance(credit, bool)
            or not isinstance(credit, int)
            or not 0 <= credit <= _MAX_SAFE_INTEGER
        ):
            raise HiflyError("数字人服务积分余额响应不完整")
        return credit

    # -- 二进制传输 ---------------------------------------------------------

    def upload_file(self, target: HiflyUploadTarget, content: bytes) -> None:
        """PUT raw bytes to a vendor-issued upload URL (docs §通用/上传)."""
        self.transport.request(
            "PUT",
            target.upload_url,
            headers={"Content-Type": target.content_type},
            body=content,
        )

    def download(self, url: str) -> bytes:
        """Fetch a binary result (temporary video URL) from the vendor."""
        return self.transport.request("GET", url, headers={"Accept": "*/*"})

    def _extract_task_id(self, data: Mapping[str, Any]) -> str:
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HiflySubmissionUncertain("数字人服务未返回创作任务凭证")
        return task_id


def hifly_client_from_config(config: Mapping[str, str]) -> HiflyClient:
    api_key = (config.get("api_key") or "").strip()
    if not api_key:
        raise HiflySettingsUnavailable("数字人服务未正确配置，请联系管理员")
    return HiflyClient(api_key=api_key)


def hifly_client_from_settings(conn: BusinessConnection) -> HiflyClient:
    try:
        config = SettingsRepository(conn).load_provider_config("hifly")
    except SettingsUnavailableError as exc:
        raise HiflySettingsUnavailable("数字人服务暂不可用，请联系管理员") from exc
    return hifly_client_from_config(config)
