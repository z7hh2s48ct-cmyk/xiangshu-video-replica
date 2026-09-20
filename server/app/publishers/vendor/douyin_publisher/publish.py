#!/usr/bin/env python3
"""
抖音创作者中心 - 视频 / 图文上传并发布（纯 HTTP，无浏览器自动化）

基于 creator.douyin.com 抓包还原。

视频发布:
  0) GET  /web/api/media/user/info/                 解析 user_id（登录态）
  1) GET  /web/api/media/upload/auth/v5/            临时 STS（VOD / ImageX）
  2) GET  vod.bytedanceapi.com?Action=ApplyUploadInner
  3) POST {UploadHost}/upload/v1/{StoreUri}         分片 init / transfer / finish
  4) POST vod...?Action=CommitUploadInner           -> video_id (Vid)
  5) GET  /web/api/media/upload/auth/v5/            再取 STS（封面走 ImageX，可选）
  6) GET  imagex...?Action=ApplyImageUpload
  7) POST tos upload/v1/{cover} + CommitImageUpload -> poster uri（无封面则跳过，用 poster_delay 取帧）
  8) GET  /web/api/media/video/enable|transend/     轮询转码就绪
  9) POST /web/api/media/aweme/create_v2/           media_type=4

图文发布:
  0) GET  /web/api/media/user/info/                 解析 user_id
  1) GET  /web/api/media/upload/auth/v5/            STS
  2) 每张图: ApplyImageUpload → TOS 上传 → CommitImageUpload
     （可 ThreadPool 并发，workers=1~8）
  3) 可选再传自定义封面（同样 ImageX 流程）
  4) POST /web/api/media/aweme/create_v2/           media_type=2, images=[...]

创作者请求风控挂载（_creator_url / create_v2）:
  - URL query: msToken、a_bogus（及浏览器指纹参数）
  - Header: x-secsdk-csrf-token、bd-ticket-guard-*

用法:
  1. 浏览器登录创作者中心，导出 Cookie → cookies.txt
     以及 localStorage security-sdk → security_sdk.json（ticket-guard）
  2. pip install -r requirements.txt ；需本机 Node.js
  3. 改 main() 里 mode / 路径或 URL / 文案后: python publish.py
     video / images / cover 支持本地路径或 http(s) 直链
     仅测登录: python publish.py --check-login  （或 mode="check"）

说明:
  - 默认用 curl_cffi 伪装 Chrome TLS/HTTP2（无库则回退 requests）
  - DouyinPublisher(cookie, security_sdk=dict|SecurityMaterial)，不收文件路径；
    main() 自行读 json 再传入
  - 风控实现见 sign_params.py / http_client.py:
      * TLS            curl_cffi impersonate（DOUYIN_CURL_IMPERSONATE）
      * bd-ticket-guard  纯 Python ECDSA（需 security_sdk 数据）
      * msToken          Node gen_strdata --serve → POST mssdk 换票（同 TLS）
      * a_bogus          Node 跑 _reverse/bdms.min.js（进程池）
      * x-secsdk-csrf    HEAD .../user/info/ 换取
      * x-tt-session-dtrait 可选：DOUYIN_SESSION_DTRAIT / session_dtrait.txt
  - check_login(cookie) 只传 Cookie 探测是否登录
  - Cookie / security_sdk 会过期，失败时从浏览器重新导出
  - 二次验证（gateway_biz_verify）绑定浏览器客户端，仅模拟 TLS/mssdk 不能保证带走核身态
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import random
import tempfile
import time
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, quote_plus, unquote, urlencode, urlparse

import requests

from .sign_params import (
    MsTokenCache,
    SecurityMaterial,
    TicketGuardSigner,
    attach_risk_params,
)
from .publish_options import (
    IMAGE_TITLE_MAX_LEN,
    PublishOptions,
    VIDEO_TITLE_MAX_LEN,
    parse_hashtag_names,
)
from .http_client import (
    UA,
    BROWSER_VERSION,
    create_session,
    load_session_dtrait,
    tls_backend_info,
)

logger = logging.getLogger(__name__)

CREATOR = "https://creator.douyin.com"
VOD_HOST = "https://vod.bytedanceapi.com"
IMAGEX_HOST = "https://imagex.bytedanceapi.com"
APP_ID = "2906"
AID = "1128"
IMAGEX_SERVICE_ID = "jm8ajry58r"
PART_SIZE = 5 * 1024 * 1024  # 5MB，与网页一致
# gateway 对过小 transfer 常返回 code=5000；5~15MB 先直传，失败再回退分片。
DIRECT_UPLOAD_TRY_MAX = 3 * PART_SIZE

# 详细字段默认开 DEBUG；DOUYIN_LOG_VERBOSE=0 → INFO；也可用 DOUYIN_LOG_LEVEL 覆盖
LOG_VERBOSE = os.environ.get("DOUYIN_LOG_VERBOSE", "1").strip() not in ("0", "false", "False")
# 接口间随机停顿（秒）：默认 0~1.5；DOUYIN_JITTER=0 关闭；DOUYIN_JITTER_MAX 改上限
_JITTER_OFF = os.environ.get("DOUYIN_JITTER", "1").strip().lower() in (
    "0",
    "false",
    "off",
    "no",
)
try:
    JITTER_MAX = float(os.environ.get("DOUYIN_JITTER_MAX", "0.5") or "0.5")
except ValueError:
    JITTER_MAX = 1.5


def _jitter_sleep(lo: float = 0.0, hi: Optional[float] = None) -> float:
    """请求前随机 sleep，模拟人工间隔。"""
    if _JITTER_OFF:
        return 0.0
    upper = JITTER_MAX if hi is None else hi
    if upper <= 0 or upper < lo:
        return 0.0
    delay = random.uniform(lo, upper)
    if delay > 0:
        time.sleep(delay)
    return delay

# URL 下载产生的临时文件，进程退出时清理
_TEMP_MEDIA: list[Path] = []
_EXT_BY_CONTENT_TYPE = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def setup_logging() -> None:
    """配置根日志。DOUYIN_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR 优先于 VERBOSE。"""
    level_name = os.environ.get("DOUYIN_LOG_LEVEL", "").strip().upper()
    if level_name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = getattr(logging, level_name)
    else:
        level = logging.DEBUG if LOG_VERBOSE else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # 避免 DEBUG 时 urllib3 刷屏
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _log(msg: str) -> None:
    logger.info("%s", msg)


def _log_debug(msg: str) -> None:
    logger.debug("%s", msg)


def _log_warn(msg: str) -> None:
    logger.warning("%s", msg)


def _log_error(msg: str) -> None:
    logger.error("%s", msg)


def _clip(text: Any, n: int = 300) -> str:
    if text is None:
        return ""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + f"...({len(s)} chars)"


def _cookie_digest(cookie: str) -> str:
    keys = []
    for part in cookie.split(";"):
        k = part.strip().split("=", 1)[0]
        if k:
            keys.append(k)
    important = [
        k
        for k in (
            "sessionid",
            "sessionid_ss",
            "sid_tt",
            "uid_tt",
            "csrf_session_id",
            "bd_ticket_guard_client_data",
            "bd_ticket_guard_client_data_v2",
            "ttwid",
        )
        if k in keys
    ]
    return f"len={len(cookie)} keys={len(keys)} has=[{', '.join(important) or 'none'}]"


def _url_brief(url: str) -> str:
    try:
        from urllib.parse import urlparse, parse_qsl

        p = urlparse(url)
        qnames = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
        qhint = ",".join(qnames[:14]) + ("..." if len(qnames) > 14 else "")
        return f"{p.scheme}://{p.netloc}{p.path}?[{qhint}]"
    except Exception:
        return _clip(url, 160)


# ---------------------------------------------------------------------------
# 登录态检测（只传 Cookie）
# ---------------------------------------------------------------------------

def check_login(cookie: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """输入 Cookie 字符串，请求 creator user/info 判断是否已登录。

    返回:
      ok        True=已登录
      user_id   用户 uid（未登录为空）
      nickname  昵称（可能为空）
      message   简要说明
    """
    cookie = (cookie or "").strip()
    if not cookie:
        out = {"ok": False, "user_id": "", "nickname": "", "message": "Cookie 为空"}
        logger.warning("[登录] %s", out["message"])
        return out

    sess = create_session(cookie)
    try:
        _jitter_sleep()
        r = sess.get(
            f"{CREATOR}/web/api/media/user/info/",
            params={"aid": AID},
            timeout=timeout,
        )
        data = r.json()
    except Exception as e:
        out = {
            "ok": False,
            "user_id": "",
            "nickname": "",
            "message": f"请求失败: {e}",
        }
        logger.warning("[登录] %s", out["message"])
        return out

    api_code = data.get("status_code")
    api_msg = str(data.get("status_msg") or "")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    uid = str(user.get("uid") or user.get("user_id") or "") or ""
    nick = str(user.get("nickname") or "") or ""

    ok = api_code in (0, "0") and bool(uid) and "未登录" not in api_msg
    if api_code in (8, "8") or "未登录" in api_msg:
        ok = False

    out = {
        "ok": ok,
        "user_id": uid if ok else "",
        "nickname": nick if ok else "",
        "message": (
            f"已登录 user_id={uid}" + (f" nickname={nick}" if nick else "")
            if ok
            else (api_msg or f"未登录 status_code={api_code}")
        ),
    }
    logger.info("[登录] %s", out["message"])
    return out


# 兼容旧名
check_login_state = check_login


# ---------------------------------------------------------------------------
# AWS4 签名（VOD / ImageX Apply & Commit）
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def aws4_sign(
    method: str,
    url: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    region: str,
    service: str,
    body: bytes = b"",
    include_content_sha256: bool = False,
) -> dict[str, str]:
    """生成 Authorization / x-amz-* 头。

    抖音 VOD/ImageX 网页端 SignedHeaders 通常为:
      GET:  x-amz-date;x-amz-security-token
      POST: x-amz-content-sha256;x-amz-date;x-amz-security-token
    （不签 host，与标准 AWS SDK 略有不同）
    """
    from urllib.parse import urlparse, parse_qsl

    parsed = urlparse(url)
    canonical_uri = parsed.path or "/"
    qs = parse_qsl(parsed.query, keep_blank_values=True)
    canonical_qs = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(qs)
    )

    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = _sha256_hex(body)
    sign_payload = include_content_sha256 or bool(body)

    headers: dict[str, str] = {
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
    }
    if sign_payload:
        headers["x-amz-content-sha256"] = payload_hash

    signed_header_keys = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_header_keys)
    signed_headers = ";".join(signed_header_keys)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_qs,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    out = {
        "Authorization": auth,
        "X-Amz-Date": amz_date,
        "X-Amz-Security-Token": session_token,
        "User-Agent": UA,
        "Referer": f"{CREATOR}/",
    }
    if sign_payload:
        out["X-Amz-Content-Sha256"] = payload_hash
    return out


def crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _tos_part_chunks(data: bytes, part_size: int) -> list[bytes]:
    """按 part_size 切块；末片不足 part_size 时并入上一片再上传。"""
    size = len(data)
    if size <= part_size:
        return [data]
    n_full = size // part_size
    rem = size % part_size
    if rem == 0:
        return [data[i * part_size : (i + 1) * part_size] for i in range(n_full)]
    # 末片 rem < part_size，与上一整片合并
    if n_full == 1:
        return [data]
    chunks = [data[i * part_size : (i + 1) * part_size] for i in range(n_full - 1)]
    chunks.append(data[(n_full - 1) * part_size :])
    return chunks


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DouyinPublisher:
    def __init__(
        self,
        cookie: str,
        user_id: str = "",
        security_sdk: Optional[SecurityMaterial | dict[str, Any]] = None,
    ):
        cookie = cookie.strip()
        self.session = create_session(cookie)
        info = tls_backend_info()
        _log(
            f"[初始化] TLS 后端={info.get('backend')} "
            f"impersonate={info.get('impersonate') or '（无）'}"
        )
        self.user_id = user_id
        self.csrf_token = ""
        self.session_dtrait = load_session_dtrait()
        if self.session_dtrait:
            _log(f"[初始化] 已加载 x-tt-session-dtrait 长度={len(self.session_dtrait)}")
        else:
            _log(
                "[初始化] 未提供 x-tt-session-dtrait "
                "（可选：环境变量 DOUYIN_SESSION_DTRAIT 或 session_dtrait.txt）"
            )
        self.ms_token = MsTokenCache.from_sources()
        if self.ms_token.refresh_from_mssdk(user_agent=UA, cookie=cookie):
            _log(f"[初始化] msToken 已就绪（现场换票）长度={len(self.ms_token.token)}")
        elif self.ms_token.token:
            _log(
                f"[初始化] msToken 使用缓存 长度={len(self.ms_token.token)} "
                "（mssdk 刷新失败，沿用本地/环境变量）"
            )
        else:
            _log_warn(
                "[初始化] msToken 为空 — 发布可能失败；"
                "请确认已安装 Node 且存在 _reverse/bdms.min.js，或设置 DOUYIN_MS_TOKEN"
            )
        self.ticket_signer: Optional[TicketGuardSigner] = None
        material: Optional[SecurityMaterial] = None
        if isinstance(security_sdk, SecurityMaterial):
            material = security_sdk
        elif isinstance(security_sdk, dict):
            material = SecurityMaterial.from_dict(security_sdk)
        _log(f"[初始化] Cookie {_cookie_digest(cookie)}")
        _log(f"[初始化] 传入 user_id={user_id or '（空，将自动探测）'}")
        if material is not None:
            self.ticket_signer = TicketGuardSigner(material)
            _log(
                "[风控] ticket-guard 已加载 "
                f"ticket={_clip(material.ticket, 48)} "
                f"ts_sign={_clip(material.ts_sign, 40)} "
                f"证书={'有' if material.client_cert else '无'}"
            )
        else:
            _log_warn("[风控] 未提供 security_sdk，ticket-guard 缺失（发布可能失败）")
        self._refresh_csrf()

    def _track(self, response: requests.Response) -> requests.Response:
        before = self.ms_token.token
        self.ms_token.update_from_response(response)
        if self.ms_token.token and self.ms_token.token != before:
            _log_debug(f"[msToken] 已从响应更新 长度={len(self.ms_token.token)}")
        return response

    def _api(
        self,
        method: str,
        url: str,
        *,
        track: bool = True,
        session: Any = None,
        jitter: bool = True,
        **kwargs: Any,
    ):
        """统一出口：接口调用前随机 sleep，再发请求。"""
        if jitter:
            delay = _jitter_sleep()
            if delay:
                _log_debug(f"[jitter] {delay:.3f}s → {method.upper()}")
        sess = session or self.session
        r = getattr(sess, method.lower())(url, **kwargs)
        return self._track(r) if track else r

    def _log_http(
        self,
        tag: str,
        method: str,
        url: str,
        response: requests.Response,
        elapsed_ms: float,
        req_headers: Optional[dict] = None,
        body_preview: str = "",
    ) -> None:
        _log(
            f"[{tag}] {method} {_url_brief(url)} → "
            f"HTTP {response.status_code}（{elapsed_ms:.0f}ms） "
            f"响应={_clip(response.text, 240)}"
        )
        if req_headers and logger.isEnabledFor(logging.DEBUG):
            interesting = {
                k: (_clip(v, 64) if "data" in k.lower() or "token" in k.lower() or "key" in k.lower() else v)
                for k, v in req_headers.items()
                if k.lower()
                in {
                    "content-type",
                    "x-secsdk-csrf-token",
                    "bd-ticket-guard-version",
                    "bd-ticket-guard-iteration-version",
                    "bd-ticket-guard-web-version",
                    "bd-ticket-guard-web-sign-type",
                    "bd-ticket-guard-client-data",
                    "bd-ticket-guard-ree-public-key",
                    "referer",
                }
            }
            if interesting:
                _log_debug(f"[{tag}] 请求头={interesting}")
        if body_preview and logger.isEnabledFor(logging.DEBUG):
            _log_debug(f"[{tag}] 请求体={_clip(body_preview, 400)}")

    @staticmethod
    def _fix_http_header_text(s: str) -> str:
        """HTTP 头按 Latin-1 解码、服务端实发 UTF-8 时的乱码修复。

        requests/urllib3 按 RFC 把 header 当 ISO-8859-1；字节跳动网关常直接塞 UTF-8，
        打印会出现 èº«ä»½…。用 latin-1 还原字节再按 utf-8 解即可。
        """
        if not s or not isinstance(s, str):
            return s
        try:
            fixed = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
        # 仅当修复后明显更像中文/少替换符时采用
        if fixed == s:
            return s
        return fixed

    @classmethod
    def _parse_passport_verify(cls, headers: dict[str, str]) -> Optional[dict[str, Any]]:
        """解析 X-Tt-Verify-Passport-Decision（网关二次验证）。"""
        raw = None
        for k, v in headers.items():
            if k.lower() == "x-tt-verify-passport-decision":
                raw = cls._fix_http_header_text(v)
                break
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return {"raw": raw}
        out: dict[str, Any] = {
            "account_flow": data.get("account_flow"),
            "verify_ways": data.get("verify_way_name_list"),
        }
        ev = data.get("event_params") or {}
        out["verify_reason"] = ev.get("verify_reason")
        out["verify_scene"] = ev.get("verify_scene")
        user = data.get("user_info") or {}
        if isinstance(user, dict) and user.get("nickname"):
            out["nickname"] = cls._fix_http_header_text(str(user["nickname"]))
        common = data.get("common_params") or {}
        cw = common.get("copywriting")
        if isinstance(cw, str):
            cw = cls._fix_http_header_text(cw)
            try:
                cw = json.loads(cw)
            except Exception:
                pass
        if isinstance(cw, dict):
            title = cw.get("title")
            desc = cw.get("desc")
            out["title"] = cls._fix_http_header_text(title) if isinstance(title, str) else title
            out["desc"] = cls._fix_http_header_text(desc) if isinstance(desc, str) else desc
        return out

    def _explain_create_v2_block(self, response: requests.Response) -> Optional[str]:
        """把空 body / 网关头翻译成可读原因。"""
        headers = {k: v for k, v in response.headers.items()}
        verify = self._parse_passport_verify(headers)
        parts: list[str] = []
        if verify and verify.get("account_flow") == "verify":
            ways = verify.get("verify_ways") or "?"
            reason = verify.get("verify_reason") or "?"
            title = verify.get("title") or "身份验证"
            desc = verify.get("desc") or ""
            parts.append(
                f"账号网关要求二次验证（{title}）：{desc} "
                f"reason={reason} ways={ways}。"
                # "请同一账号打开创作者中心，完成短信/扫码验证后，再发布。"
                # "重新导出 cookies.txt 与 security_sdk.json 再发布。"
            )
        tg_key = headers.get("Bd-Ticket-Guard-Key-Sign-Result") or headers.get(
            "bd-ticket-guard-key-sign-result"
        )
        tg_ts = headers.get("Bd-Ticket-Guard-Sign-Res-Static-Ts-Sign") or headers.get(
            "bd-ticket-guard-sign-res-static-ts-sign"
        )
        if tg_key and str(tg_key) not in ("0", "1", ""):
            parts.append(
                f"ticket-guard Key-Sign-Result={tg_key}"
                + (f" Ts-Sign={tg_ts}" if tg_ts else "")
                # + "（常见于 security_sdk 过期/与 Cookie 不同步，请重新导出）。"
            )
        return " ".join(parts) if parts else None

    def _dump_error_response(
        self,
        response: requests.Response,
        tag: str = "error",
        parsed: Any = None,
    ) -> None:
        """异常时尽量完整打印接口响应，便于判断 403/验证码/踢登录。"""
        body = response.text if response.text is not None else ""
        headers = {k: v for k, v in response.headers.items()}
        # 优先关注风控 / 验证 / 会话相关头
        interesting_keys = (
            "content-type",
            "content-length",
            "x-tt-logid",
            "x-tt-trace-id",
            "x-ms-token",
            "x-ware-csrf-token",
            "x-secsdk-csrf-token",
            "bd-ticket-guard-",
            "x-tt-verify-passport-decision",
            "access-control-expose-headers",
            "location",
            "server",
            "via",
            "x-blocked",
            "x-captcha",
            "x-verify",
            "bdturing",
        )
        focus = {}
        for k, v in headers.items():
            lk = k.lower()
            if any(lk == ik or lk.startswith(ik) for ik in interesting_keys):
                # passport 文案头常含中文 UTF-8，需修 Latin-1 误解码
                if lk == "x-tt-verify-passport-decision":
                    focus[k] = self._fix_http_header_text(v)
                else:
                    focus[k] = v
        _log_error("=" * 60)
        _log_error(f"[{tag}] 错误响应详情")
        _log_error(
            f"[{tag}] 状态码={response.status_code} 原因={response.reason!r} "
            f"url={response.url}"
        )
        _log_error(
            f"[{tag}] 关键响应头="
            f"{json.dumps(focus, ensure_ascii=False) if focus else '（无匹配）'}"
        )
        # all headers 仍保留原始值；中文可读性看 focus / passport_verify
        _log_error(f"[{tag}] 全部响应头={json.dumps(headers, ensure_ascii=False)}")
        _log_error(f"[{tag}] 响应体长度={len(body.encode('utf-8', errors='replace'))}")
        if body:
            _log_error(f"[{tag}] 响应体={body}")
        else:
            _log_error(f"[{tag}] 响应体=（空）")
        if parsed is not None:
            _log_error(f"[{tag}] 响应JSON={json.dumps(parsed, ensure_ascii=False)}")
        verify = self._parse_passport_verify(headers)
        if verify:
            _log_error(f"[{tag}] 护照二次验证={json.dumps(verify, ensure_ascii=False)}")
        explain = self._explain_create_v2_block(response)
        if explain:
            _log_error(f"[{tag}] 说明={explain}")
        blob = (body + " " + json.dumps(headers, ensure_ascii=False)).lower()
        hints = []
        for kw in (
            "captcha",
            "verify",
            "bdturing",
            "滑块",
            "验证码",
            "二次验证",
            "用户未登录",
            "login",
            "forbidden",
            "gateway_biz_verify",
        ):
            if kw.lower() in blob or kw in body:
                hints.append(kw)
        if not body and response.status_code == 403:
            hints.append("empty_403_likely_waf_or_secsdk")
        if verify and verify.get("account_flow") == "verify":
            hints.append("passport_second_verification")
        _log_error(f"[{tag}] 风控提示={hints or ['（无）']}")
        _log_error("=" * 60)

    def _creator_url(
        self,
        path: str,
        params: Optional[dict] = None,
        body: str = "",
        method: str = "GET",
    ) -> str:
        # 与网页一致：空格编码为 +，而不是 %20 / 预替换成 %2B
        qs = urlencode(params or {}, quote_via=quote_plus)
        url = f"{CREATOR}{path}"
        if qs:
            url = f"{url}?{qs}"
        return attach_risk_params(
            url,
            body=body,
            ms_token=self.ms_token,
            user_agent=UA,
            method=method,
        )

    def _ticket_headers(self, url: str) -> dict[str, str]:
        if not self.ticket_signer:
            return {}
        return self.ticket_signer.headers_for(url)

    def _parse_ware_csrf(self, response: requests.Response) -> str:
        """解析 secsdk CSRF。

        响应头 X-Ware-Csrf-Token 形如:
          0,<token>,<expire>,success,<csrf_session_id>
        请求头 x-secsdk-csrf-token 应传: <token>,<expire>
        """
        raw = (
            response.headers.get("X-Ware-Csrf-Token")
            or response.headers.get("x-ware-csrf-token")
            or response.headers.get("X-Secsdk-Csrf-Token")
            or response.headers.get("x-secsdk-csrf-token")
            or ""
        )
        if not raw:
            return ""
        parts = [p.strip() for p in raw.split(",")]
        # 标准 ware 格式: status,token,expire,success,sid
        if len(parts) >= 3 and parts[1].startswith("0001"):
            return f"{parts[1]},{parts[2]}"
        # 已是 token,expire 或裸 token
        return raw

    def _cookie_value(self, name: str) -> str:
        """从 Cookie 头或 cookie jar 取单个值（Cookie 头写入时 jar 里可能没有）。"""
        try:
            v = self.session.cookies.get(name)
            if v:
                return str(v)
        except Exception:
            pass
        raw = self.session.headers.get("Cookie") or ""
        prefix = name + "="
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(prefix):
                return part[len(prefix) :]
        return ""

    def _refresh_csrf(self) -> None:
        """通过 secsdk 协议拿 x-secsdk-csrf-token。

        SPA 页面 GET 往往只返回 HTML、不带 CSRF 头；应对 API 路径发 HEAD。
        """
        url = f"{CREATOR}/web/api/media/user/info/"
        t0 = time.time()
        r = self._api(
            "head",
            url,
            track=False,
            headers={
                "X-Secsdk-Csrf-Request": "1",
                "X-Secsdk-Csrf-Version": "1.2.22",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
        elapsed = (time.time() - t0) * 1000
        token = self._parse_ware_csrf(r)
        if token:
            self.csrf_token = token
            self.session.headers["x-secsdk-csrf-token"] = token
            _log(f"[CSRF] 获取成功 HTTP {r.status_code}（{elapsed:.0f}ms） token={_clip(token, 48)}")
            return
        # fallback: 部分环境只校验 csrf_session_id
        sid = self._cookie_value("csrf_session_id")
        if sid:
            self.csrf_token = sid
            self.session.headers["x-secsdk-csrf-token"] = sid
            _log(
                f"[CSRF] 响应头为空 HTTP {r.status_code}（{elapsed:.0f}ms），"
                f"改用 csrf_session_id={_clip(sid, 40)}"
            )
            return
        _log(
            f"[CSRF] 获取失败 HTTP {r.status_code}（{elapsed:.0f}ms） "
            f"响应头={list(r.headers.keys())[:14]} 正文={_clip(r.text, 120)}"
        )

    def _common_qs(self) -> dict[str, str]:
        return {
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Mozilla",
            "browser_version": BROWSER_VERSION,
            "browser_online": "true",
            "timezone_name": "Asia/Shanghai",
            "aid": AID,
            "support_h265": "1",
        }

    def resolve_hashtag_id(self, name: str) -> str:
        """通过创作者中心 challengesug 解析话题 cid；失败返回空串。"""
        name = (name or "").lstrip("#").strip()
        if not name:
            return ""
        qs = {
            **self._common_qs(),
            "keyword": name,
            "count": "12",
            "aid": AID,
        }
        url = self._creator_url(
            "/aweme/v1/search/challengesug/",
            params=qs,
            method="GET",
        )
        try:
            r = self._api("get", url, timeout=30)
            data = r.json() if r.content else {}
        except Exception as e:
            _log_warn(f"[话题] 搜索失败 name={name!r}: {e}")
            return ""
        sug = data.get("sug_list") if isinstance(data, dict) else None
        if not isinstance(sug, list):
            _log_warn(f"[话题] 搜索无结果 name={name!r} status={data.get('status_code')}")
            return ""
        exact = ""
        fold = ""
        for item in sug:
            if not isinstance(item, dict):
                continue
            cha = str(item.get("cha_name") or "").strip()
            cid = str(item.get("cid") or "").strip()
            if not cha or not cid:
                continue
            if cha == name:
                exact = cid
                break
            if not fold and cha.lower() == name.lower():
                fold = cid
        hid = exact or fold
        if hid:
            _log(f"[话题] 解析成功 {name!r} → cid={hid}")
        else:
            _log_warn(f"[话题] 未命中精确名 {name!r}，将使用 hashtag_id=0")
        return hid

    def enrich_hashtag_ids(self, options: PublishOptions) -> PublishOptions:
        """把纯名字话题补上真实 hashtag_id（已有 id 的 dict 保留）。"""
        id_by_name: dict[str, str] = {}
        for tag in options.hashtags:
            if not isinstance(tag, dict):
                continue
            n = str(tag.get("hashtag_name") or tag.get("name") or "").lstrip("#").strip()
            if not n:
                continue
            hid = tag.get("hashtag_id")
            if hid is None or hid == "" or str(hid) == "0":
                hid = tag.get("cid")
            if hid is not None and str(hid) not in ("", "0"):
                id_by_name[n] = str(hid)

        names = parse_hashtag_names(options.hashtags)
        if not names:
            return options
        enriched: list[Any] = []
        for name in names:
            hid = id_by_name.get(name) or self.resolve_hashtag_id(name)
            if hid:
                enriched.append({"hashtag_name": name, "hashtag_id": hid})
            else:
                enriched.append(name)
        options.hashtags = enriched
        return options

    def ensure_user_id(self) -> str:
        if self.user_id:
            _log(f"[鉴权] 使用传入的 user_id={self.user_id}")
            return self.user_id

        def _dig(data: Any, path: tuple[str, ...]) -> Any:
            cur = data
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    return None
            return cur

        # mcn/account_base_info 只有机构状态，没有 uid；应用 media/user/info
        candidates = (
            ("/web/api/media/user/info/", self._common_qs()),
            ("/web/api/media/user/info/", None),
        )
        paths = (
            ("user", "uid"),
            ("user", "user_id"),
            ("data", "user", "uid"),
            ("user_info", "uid"),
            ("uid",),
            ("user_id",),
        )
        for path, params in candidates:
            url = f"{CREATOR}{path}"
            try:
                t0 = time.time()
                r = self._api("get", url, params=params, timeout=30)
                elapsed = (time.time() - t0) * 1000
                _log(
                    f"[用户] GET {path} → HTTP {r.status_code}（{elapsed:.0f}ms） "
                    f"响应={_clip(r.text, 200)}"
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                _log(f"[用户] GET {path} 失败: {e}")
                continue
            for p in paths:
                cur = _dig(data, p)
                if cur:
                    self.user_id = str(cur)
                    _log(f"[鉴权] 自动获取 user_id={self.user_id}（来自 {path} {'.'.join(p)}）")
                    return self.user_id

        raise RuntimeError(
            "无法自动获取 user_id，请在请求里传 user_id="
            "（发布页 ApplyUploadInner 的 user_id / author_id）"
        )

    def check_login(self) -> dict[str, Any]:
        """用当前实例 Cookie 检测是否登录；成功则缓存 user_id。"""
        cookie = self.session.headers.get("Cookie") or ""
        result = check_login(cookie)
        if result.get("ok") and result.get("user_id") and not self.user_id:
            self.user_id = str(result["user_id"])
        return result

    # ----- step1: upload auth -----
    def get_upload_auth(self) -> dict[str, Any]:
        url = self._creator_url("/web/api/media/upload/auth/v5/")
        t0 = time.time()
        r = self._api("get", url, timeout=30)
        elapsed = (time.time() - t0) * 1000
        self._log_http("鉴权", "GET", url, r, elapsed)
        try:
            data = r.json()
        except Exception:
            logger.warning(
                "upload auth 非 JSON HTTP %s: %s",
                r.status_code,
                _clip(r.text, 400),
            )
            raise RuntimeError("鉴权失败，响应非 JSON")
        if r.status_code >= 400:
            logger.warning("upload auth HTTP %s: %s", r.status_code, data)
            raise RuntimeError("鉴权失败，请求失败")
        if data.get("status_code") not in (0, None) and "auth" not in data:
            logger.warning("upload auth failed: %s", data)
            raise RuntimeError("鉴权失败，请求失败")
        auth = json.loads(data["auth"])
        _log(
            f"[鉴权] 上传凭证就绪 AccessKeyID={_clip(auth.get('AccessKeyID'), 24)} "
            f"含SessionToken={'SessionToken' in auth}"
        )
        return {
            "access_key": auth["AccessKeyID"],
            "secret_key": auth["SecretAccessKey"],
            "session_token": auth["SessionToken"],
            "raw": data,
        }

    # ----- 音乐（创作者中心选歌）-----
    def music_categories(self) -> list[dict[str, Any]]:
        """GET /web/api/media/music/category → [{category_id, category_name, type}, ...]"""
        params = {**self._common_qs()}
        url = self._creator_url("/web/api/media/music/category", params=params)
        r = self._api("get", url, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status_code") not in (0, None):
            logger.warning("music/category failed: %s", data)
            raise RuntimeError("music/category 失败")
        cats = data.get("categories") or []
        _log(f"[音乐] 分类数={len(cats)}")
        return cats

    def list_music(
        self,
        *,
        category_id: str = "1",
        type: str = "recommend",
        cursor: int | str = 0,
        count: int = 20,
    ) -> dict[str, Any]:
        """GET /web/api/media/music/list → {songs, cursor, has_more}

        songs[]: music_id / music_name / music_author / duration(秒) / play_url / user_count
        """
        params = {
            "cursor": str(cursor),
            "category_id": str(category_id),
            "type": type,
            "count": str(count),
            **self._common_qs(),
        }
        url = self._creator_url("/web/api/media/music/list", params=params)
        r = self._api("get", url, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status_code") not in (0, None):
            logger.warning("music/list failed: %s", data)
            raise RuntimeError("music/list 失败")
        songs = data.get("songs") or []
        _log(
            f"[音乐] 列表 type={type} category_id={category_id} "
            f"数量={len(songs)} 还有更多={data.get('has_more')}"
        )
        return data

    def pick_recommend_music(self, index: int = 0) -> dict[str, Any]:
        """从推荐列表取一首，返回 {music_id, music_end_time, music_name, ...}。"""
        data = self.list_music(category_id="1", type="recommend", count=max(20, index + 1))
        songs = data.get("songs") or []
        if not songs:
            raise RuntimeError("推荐音乐列表为空")
        if index < 0 or index >= len(songs):
            raise IndexError(f"music index {index} out of range 0..{len(songs)-1}")
        song = songs[index]
        duration_s = int(song.get("duration") or 0)
        out = {
            "music_id": str(song["music_id"]),
            "music_end_time": duration_s * 1000 if duration_s else None,
            "music_name": song.get("music_name"),
            "music_author": song.get("music_author"),
            "duration": duration_s,
            "play_url": song.get("play_url"),
        }
        _log(
            f"[音乐] 选中 #{index} id={out['music_id']} "
            f"歌名={out['music_name']!r} 结束毫秒={out['music_end_time']}"
        )
        return out

    # ----- step2-4: video upload -----
    def apply_upload(self, creds: dict[str, str], file_size: int) -> dict[str, Any]:
        uid = self.ensure_user_id()
        params = {
            "Action": "ApplyUploadInner",
            "Version": "2020-11-19",
            "SpaceName": "aweme",
            "FileType": "video",
            "IsInner": "1",
            "FileSize": str(file_size),
            "app_id": APP_ID,
            "user_id": uid,
            "s": uuid.uuid4().hex[:10],
        }
        url = f"{VOD_HOST}/?{urlencode(params)}"
        headers = aws4_sign(
            "GET",
            url,
            creds["access_key"],
            creds["secret_key"],
            creds["session_token"],
            region="cn-north-1",
            service="vod",
        )
        t0 = time.time()
        r = self._api("get", url, track=False, headers=headers, timeout=60)
        elapsed = (time.time() - t0) * 1000
        self._log_http("申请上传", "GET", url, r, elapsed)
        r.raise_for_status()
        data = r.json()
        nodes = data["Result"]["InnerUploadAddress"]["UploadNodes"]
        node = nodes[0]
        store = node["StoreInfos"][0]
        info = {
            "vid": node["Vid"],
            "store_uri": store["StoreUri"],
            "auth": store["Auth"],
            "upload_host": node["UploadHost"],
            "session_key": node["SessionKey"],
            "user_id": store.get("StorageHeader", {}).get("USER_ID", uid),
        }
        _log(
            f"[申请上传] vid={info['vid']} 上传域名={info['upload_host']} "
            f"对象={_clip(info['store_uri'], 80)} uid={info['user_id']}"
        )
        return info

    def _tos_direct_upload(
        self,
        base: str,
        common: dict[str, str],
        data: bytes,
        *,
        allow_multipart_fallback: bool = False,
    ) -> bool:
        """一次性直传。成功返回 True；allow_multipart_fallback 时失败返回 False 以便回退分片。"""
        crc = crc32_hex(data)
        _log(f"[对象存储] 直传 大小={len(data)} crc={crc}")
        try:
            r = self._api(
                "post",
                base,
                track=False,
                jitter=False,
                headers={
                    **common,
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="video.mp4"',
                    "Content-CRC32": crc,
                },
                data=data,
                timeout=300,
            )
        except Exception as e:
            if allow_multipart_fallback:
                logger.warning("视频直传异常，改走分片: %s", e)
                return False
            raise
        try:
            r.raise_for_status()
        except Exception as e:
            if allow_multipart_fallback:
                logger.warning(
                    "视频直传 HTTP 失败，改走分片: %s body=%s",
                    e,
                    _clip(r.text, 400),
                )
                return False
            raise
        try:
            result = r.json()
        except Exception:
            result = {"raw": r.text[:300]}
        _log(f"[对象存储] 直传响应: {_clip(result, 240)}")
        code = result.get("code") if isinstance(result, dict) else None
        if code in (2000, "2000"):
            return True
        if allow_multipart_fallback:
            logger.warning(
                "视频直传失败，改走分片: %s",
                _clip(result, 400),
            )
            return False
        logger.warning("视频直传失败: %s", _clip(result, 400))
        raise RuntimeError("视频直传失败")

    def _tos_multipart_upload(
        self,
        base: str,
        common: dict[str, str],
        data: bytes,
        file_size: int,
    ) -> None:
        r = self._api(
            "post",
            f"{base}?uploadmode=part&phase=init",
            track=False,
            jitter=False,
            headers={**common},
            timeout=60,
        )
        r.raise_for_status()
        init_data = r.json()
        if init_data.get("code") not in (2000, "2000", None):
            logger.warning("视频分片初始化失败: %s", _clip(init_data, 400))
            raise RuntimeError("视频分片初始化失败")
        upload_id = init_data["data"]["uploadid"]
        chunks = _tos_part_chunks(data, PART_SIZE)
        total_parts = len(chunks)
        _log(
            f"[对象存储] 分片初始化 uploadid={upload_id} 大小={file_size} "
            f"约{total_parts}片 每片≤{PART_SIZE}"
        )

        crcs: list[str] = []
        offset = 0
        for part, chunk in enumerate(chunks, start=1):
            crc = crc32_hex(chunk)
            crcs.append(f"{part}:{crc}")
            r = self._api(
                "post",
                f"{base}?uploadid={quote(upload_id)}&part_number={part}"
                f"&phase=transfer&part_offset={offset}",
                track=False,
                jitter=False,
                headers={
                    **common,
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="video.mp4"',
                    "Content-CRC32": crc,
                },
                data=chunk,
                timeout=300,
            )
            r.raise_for_status()
            try:
                part_body = r.json()
            except Exception:
                part_body = {"raw": r.text[:200]}
            part_code = part_body.get("code") if isinstance(part_body, dict) else None
            if part_code not in (2000, "2000", None):
                logger.warning(
                    "视频分片上传失败 part=%s/%s: %s",
                    part,
                    total_parts,
                    _clip(part_body, 400),
                )
                raise RuntimeError("视频分片上传失败")
            if logger.isEnabledFor(logging.DEBUG) or part == 1 or part % 5 == 0 or part == total_parts:
                _log(
                    f"[对象存储] 分片 {part}/{total_parts} 完成 "
                    f"（{len(chunk)} 字节, crc={crc}, 偏移={offset}）"
                )
            offset += len(chunk)

        body = ",".join(crcs).encode("utf-8")
        r = self._api(
            "post",
            f"{base}?uploadmode=part&phase=finish&uploadid={quote(upload_id)}",
            track=False,
            jitter=False,
            headers={**common, "Content-Type": "text/plain;charset=UTF-8"},
            data=body,
            timeout=120,
        )
        r.raise_for_status()
        try:
            finish = r.json()
        except Exception:
            finish = {"raw": r.text[:300]}
        _log(f"[对象存储] 合并完成: {_clip(finish, 240)}")
        code = finish.get("code") if isinstance(finish, dict) else None
        if code not in (None, 2000, "2000"):
            logger.warning("视频分片合并失败: %s", _clip(finish, 400))
            raise RuntimeError("视频分片合并失败")

    def tos_upload_video(self, info: dict[str, Any], video_path: Path) -> None:
        host = info["upload_host"]
        store_uri = info["store_uri"]
        auth = info["auth"]
        uid = info["user_id"]
        base = f"https://{host}/upload/v1/{store_uri}"
        data = video_path.read_bytes()
        file_size = len(data)

        common = {
            "Authorization": auth,
            "X-Storage-Mode": "gateway",
            "X-Storage-U": str(uid),
            "User-Agent": UA,
            "Referer": f"{CREATOR}/",
            "Origin": CREATOR,
        }

        if file_size > DIRECT_UPLOAD_TRY_MAX:
            # >15MB：直接分片
            self._tos_multipart_upload(base, common, data, file_size)
        elif file_size > PART_SIZE:
            # (5MB, 15MB]：先直传，失败再回退分片
            if not self._tos_direct_upload(
                base, common, data, allow_multipart_fallback=True
            ):
                _log("[对象存储] 直传未成功，回退分片上传")
                self._tos_multipart_upload(base, common, data, file_size)
        else:
            # ≤5MB：仅直传（单分片 transfer 实测易 5000）
            self._tos_direct_upload(base, common, data)

    def commit_upload(self, creds: dict[str, str], session_key: str) -> str:
        params = {
            "Action": "CommitUploadInner",
            "Version": "2020-11-19",
            "SpaceName": "aweme",
            "app_id": APP_ID,
            "user_id": self.ensure_user_id(),
        }
        url = f"{VOD_HOST}/?{urlencode(params)}"
        payload = json.dumps(
            {
                "SessionKey": session_key,
                "Functions": [
                    {"name": "GetMeta"},
                    {"name": "Snapshot", "input": {"SnapshotTime": 0}},
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = aws4_sign(
            "POST",
            url,
            creds["access_key"],
            creds["secret_key"],
            creds["session_token"],
            region="cn-north-1",
            service="vod",
            body=payload,
        )
        headers["Content-Type"] = "text/plain;charset=UTF-8"
        r = self._api(
            "post", url, track=False, headers=headers, data=payload, timeout=120
        )
        r.raise_for_status()
        data = r.json()
        result0 = (data.get("Result") or {}).get("Results") or [{}]
        result0 = result0[0] if result0 else {}
        vid = result0.get("Vid") or ""
        meta = result0.get("VideoMeta") or {}
        width = int(meta.get("Width") or 0)
        height = int(meta.get("Height") or 0)
        duration = float(meta.get("Duration") or 0)
        _log(
            f"[视频入库] vid={vid} 分辨率={width}x{height} "
            f"时长={duration}秒 元数据={_clip(meta, 160)}"
        )
        if not vid:
            logger.warning("CommitUpload 未返回 Vid: %s", _clip(data, 400))
            raise RuntimeError("CommitUpload 未返回 Vid")
        # 入库时已无宽高/时长，后面 transend 几乎必定一直 format=unknown / encode=0
        if width <= 0 and height <= 0 and duration <= 0:
            logger.warning("视频入库后无元数据: %s", _clip(result0, 400))
            raise RuntimeError(
                "视频入库后无元数据（Width/Height/Duration 全空）。"
                "通常是分片上传未真正成功或文件服务端无法解析；"
                "请换一段正常 H.264 MP4 重试。"
            )
        return vid

    # ----- step5-7: cover / image upload (ImageX) -----
    def _image_http_session(self):
        """并发上传用独立 Session（Session 非线程安全）。"""
        cookie = self.session.headers.get("Cookie") or ""
        return create_session(cookie)

    def upload_image(
        self,
        creds: dict[str, str],
        image_path: Path,
        session=None,
    ) -> dict[str, Any]:
        """上传单张图片，返回 {uri, width, height}（图文 images[] / 封面 poster）。"""
        sess = session or self.session
        uid = self.ensure_user_id()
        params = {
            "Action": "ApplyImageUpload",
            "Version": "2018-08-01",
            "ServiceId": IMAGEX_SERVICE_ID,
            "app_id": APP_ID,
            "user_id": uid,
            "s": uuid.uuid4().hex[:10],
        }
        url = f"{IMAGEX_HOST}/?{urlencode(params)}"
        headers = aws4_sign(
            "GET",
            url,
            creds["access_key"],
            creds["secret_key"],
            creds["session_token"],
            region="cn-north-1",
            service="imagex",
        )
        r = self._api(
            "get", url, track=False, session=sess, headers=headers, timeout=60
        )
        r.raise_for_status()
        data = r.json()
        addr = data["Result"]["UploadAddress"]
        store = addr["StoreInfos"][0]
        host = addr["UploadHosts"][0]
        store_uri = store["StoreUri"]
        auth = store["Auth"]
        session_key = addr["SessionKey"]

        img = image_path.read_bytes()
        crc = crc32_hex(img)
        upload_url = f"https://{host}/upload/v1/{store_uri}"
        r = self._api(
            "post",
            upload_url,
            track=False,
            session=sess,
            headers={
                "Authorization": auth,
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{image_path.name}"',
                "Content-CRC32": crc,
                "User-Agent": UA,
                "Referer": f"{CREATOR}/",
            },
            data=img,
            timeout=120,
        )
        r.raise_for_status()
        _log(f"[图片上传] 已上传 大小={len(img)} crc={crc} → {store_uri}")

        params = {
            "Action": "CommitImageUpload",
            "Version": "2018-08-01",
            "ServiceId": IMAGEX_SERVICE_ID,
            "app_id": APP_ID,
            "user_id": uid,
        }
        url = f"{IMAGEX_HOST}/?{urlencode(params)}"
        payload = json.dumps({"SessionKey": session_key}, separators=(",", ":")).encode("utf-8")
        headers = aws4_sign(
            "POST",
            url,
            creds["access_key"],
            creds["secret_key"],
            creds["session_token"],
            region="cn-north-1",
            service="imagex",
            body=payload,
        )
        headers["Content-Type"] = "application/json"
        r = self._api(
            "post",
            url,
            track=False,
            session=sess,
            headers=headers,
            data=payload,
            timeout=60,
        )
        r.raise_for_status()
        result = r.json()
        uri = result["Result"]["Results"][0]["Uri"]
        plugin = (result["Result"].get("PluginResult") or [{}])[0]
        width = int(plugin.get("ImageWidth") or 0)
        height = int(plugin.get("ImageHeight") or 0)
        _log(
            f"[图片上传] 提交完成 uri={uri} {width}x{height} "
            f"响应={_clip(result, 200)}"
        )
        return {"uri": uri, "width": width, "height": height}

    def upload_cover(self, creds: dict[str, str], cover_path: Path) -> dict[str, Any]:
        """上传封面，返回 {uri, width, height}。"""
        return self.upload_image(creds, cover_path)

    def wait_video_ready(self, video_id: str, timeout: int = 180) -> None:
        """网页在 create 前会轮询 enable / transend，确认转码可用。

        仅 status_code=0 不够：transend 里 encode=0 / codec 空时发布易被风控拦。
        """
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        empty_streak = 0
        dumped_full = False
        while time.time() < deadline:
            for path in (
                "/web/api/media/video/enable/",
                "/web/api/media/video/transend/",
            ):
                params = {"video_id": video_id, **self._common_qs()}
                url = self._creator_url(path, params=params)
                t0 = time.time()
                try:
                    r = self._api("get", url, timeout=30)
                    elapsed = (time.time() - t0) * 1000
                    try:
                        data = r.json()
                    except Exception:
                        data = {"raw": r.text[:200]}
                    last[path] = data
                    # 首次把 transend 打全，避免截断看不到 format/poster 等字段
                    if path.endswith("transend/") and not dumped_full:
                        _log(f"[视频就绪] transend 完整响应={_clip(data, 1200)}")
                        dumped_full = True
                    else:
                        _log(
                            f"[视频就绪] {path} HTTP {r.status_code}（{elapsed:.0f}ms） "
                            f"{_clip(data, 180)}"
                        )
                except Exception as e:
                    _log(f"[视频就绪] {path} 出错: {e}")
                    last[path] = {"error": str(e)}

            enable = last.get("/web/api/media/video/enable/") or {}
            transend = last.get("/web/api/media/video/transend/") or {}
            enable_ok = (
                isinstance(enable, dict)
                and "error" not in enable
                and enable.get("status_code") in (0, "0", None)
            )
            trans_ok = False
            encode = codec = None
            bitrate = 0
            duration = 0
            if isinstance(transend, dict) and "error" not in transend:
                sc = transend.get("status_code")
                encode = transend.get("encode")
                codec = (transend.get("codec_type") or "").strip()
                try:
                    bitrate = int(transend.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                try:
                    duration = float(transend.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                # encode=1 或已有码率/编码信息，才认为可发
                if sc in (0, "0", None) and (
                    encode in (1, "1", True) or bool(codec) or bitrate > 0
                ):
                    trans_ok = True
                _log(
                    f"[视频就绪] 启用={enable_ok} 转码={trans_ok} "
                    f"encode={encode!r} 编码={codec!r} 码率={bitrate} "
                    f"时长={duration} format={transend.get('format')!r}"
                )

            if enable_ok and trans_ok:
                _log(f"[视频就绪] 可发布 video_id={video_id}")
                return

            # 连续多次「启用 OK 但元数据全空」= 服务端没在转，继续轮询无意义
            if enable_ok and not trans_ok and bitrate <= 0 and not codec and (
                encode in (0, "0", None)
            ) and duration <= 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_streak >= 12:
                logger.warning(
                    "转码元数据持续为空 video_id=%s last=%s",
                    video_id,
                    _clip(last, 500),
                )
                raise RuntimeError(
                    "转码元数据持续为空（encode=0/codec/bitrate/duration 全空，"
                    f"已连续 {empty_streak} 轮）。video_id={video_id} "
                    "不是「还在转」，而是入库视频未被处理。"
                    "请检查 [视频入库] 的宽高/时长，换正常 H.264 MP4 重传，"
                    "或刷新 Cookie 后再试。"
                )
            time.sleep(2)
        logger.warning(
            "视频未就绪超时 video_id=%s last=%s",
            video_id,
            _clip(last, 400),
        )
        raise RuntimeError(f"视频未就绪超时 video_id={video_id}")

    # ----- step8: publish -----
    def create_aweme(
        self,
        video_id: str,
        poster: str,
        options: PublishOptions,
        cover_width: int = 0,
        cover_height: int = 0,
    ) -> dict[str, Any]:
        self.enrich_hashtag_ids(options)
        body = options.to_create_item(
            video_id,
            poster,
            self._new_creation_id(),
            cover_width=cover_width,
            cover_height=cover_height,
        )
        return self._post_create_v2(body, options, referer_kind="video")

    def create_image_aweme(
        self,
        images: list[dict[str, Any]],
        poster: str,
        options: PublishOptions,
    ) -> dict[str, Any]:
        if not images:
            raise ValueError("图文至少需要 1 张图片")
        self.enrich_hashtag_ids(options)
        body = options.to_create_image_item(images, poster, self._new_creation_id())
        return self._post_create_v2(body, options, referer_kind="image")

    def _new_creation_id(self) -> str:
        return f"py{uuid.uuid4().hex[:8]}{int(time.time() * 1000)}"

    def _post_create_v2(
        self,
        body: dict[str, Any],
        options: PublishOptions,
        referer_kind: str = "video",
    ) -> dict[str, Any]:
        # 上传耗时长，发布前刷新 CSRF，避免 secsdk 直接 403
        _log("[发布] 发布前刷新 CSRF …")
        self._refresh_csrf()
        qs = {
            "read_aid": APP_ID,
            **self._common_qs(),
        }
        _log('body: '+str(body))
        common = body.get("item", {}).get("common", {})
        _log(
            "[发布] 请求摘要: "
            f"类型={common.get('media_type')} "
            f"标题={options.title!r} "
            f"正文={common.get('text')!r} "
            f"可见性={common.get('visibility_type')} "
            f"定时={common.get('timing')} "
            f"video_id={common.get('video_id')} "
            f"图片数={len(common.get('images') or [])} "
            f"封面={(body.get('item') or {}).get('cover', {}).get('poster')} "
            f"creation_id={common.get('creation_id')} "
            f"话题={common.get('challenges')} "
            f"text_extra={common.get('text_extra')}"
        )
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        url = self._creator_url(
            "/web/api/media/aweme/create_v2/",
            params=qs,
            body=body_str,
            method="POST",
        )
        has_ab = "a_bogus=" in url
        referer = (
            f"{CREATOR}/creator-micro/content/post/image?enter_from=publish_page"
            if referer_kind == "image"
            else f"{CREATOR}/creator-micro/content/post/video?enter_from=publish_page"
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "x-secsdk-csrf-token": self.csrf_token,
            **self._ticket_headers(url),
        }
        if self.session_dtrait:
            headers["x-tt-session-dtrait"] = self.session_dtrait
        _log(
            f"[发布] 提交 create_v2 "
            f"csrf={'有' if self.csrf_token else '无'} "
            f"msToken={'有' if self.ms_token.token else '无'} "
            f"a_bogus={'有' if has_ab else '无'} "
            f"ticket={'有' if self.ticket_signer else '无'} "
            f"dtrait={'有' if self.session_dtrait else '无'} "
            f"TLS={tls_backend_info().get('backend')} "
            f"正文字节={len(body_str.encode('utf-8'))} "
            f"url={_url_brief(url)}"
        )
        if "msToken=" not in url:
            _log_warn(
                "[发布] create_v2 URL 缺少 msToken。"
                "浏览器实发必带；这是空响应/403 的高危信号。"
            )
        t0 = time.time()
        r = self._api(
            "post",
            url,
            headers=headers,
            data=body_str.encode("utf-8"),
            timeout=60,
        )
        elapsed = (time.time() - t0) * 1000
        self._log_http("发布", "POST", url, r, elapsed, headers, body_str)
        if r.status_code >= 400:
            self._dump_error_response(r, tag="发布接口")
            logger.warning(
                "create_v2 HTTP %s: %s (csrf=%s, msToken=%s, ticket=%s)",
                r.status_code,
                _clip(r.text, 800) or "(empty body)",
                "yes" if self.csrf_token else "no",
                "yes" if self.ms_token.token else "no",
                "yes" if self.ticket_signer else "no",
            )
            raise RuntimeError("create_v2 请求失败")
        try:
            data = r.json()
        except Exception:
            self._dump_error_response(r, tag="发布接口")
            explain = self._explain_create_v2_block(r)
            detail = _clip(r.text, 800) or "(empty)"
            if explain:
                logger.warning(
                    "create_v2 被拦截 HTTP %s: %s",
                    r.status_code,
                    explain,
                )
                raise RuntimeError(f"create_v2 被拦截: {explain}")
            logger.warning(
                "create_v2 非 JSON 响应 HTTP %s: %s",
                r.status_code,
                detail,
            )
            raise RuntimeError("create_v2 响应非 JSON")
        if data.get("status_code") != 0:
            self._dump_error_response(r, tag="发布接口", parsed=data)
            msg = (data.get("status_msg") or "").strip()
            if msg:
                logger.warning(
                    "create_v2 failed: status_code=%s %s",
                    data.get("status_code"),
                    msg,    
                )
                raise RuntimeError(f"create_v2 失败: {msg}")
            logger.warning(f"create_v2 failed: {data}" )
            raise RuntimeError(f"create_v2 失败: {data}")
        _log(f"[发布] 成功 作品ID={data.get('item_id')} 详情={_clip(data, 400)}")
        return data

    def publish(
        self,
        video_path: Path,
        options: PublishOptions,
        cover_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        if not video_path.is_file():
            raise FileNotFoundError(video_path)
        options.normalize_title_caption(VIDEO_TITLE_MAX_LEN)
        file_size = video_path.stat().st_size
        t_all = time.time()
        _log("=" * 60)
        _log(
            f"[开始] 视频={video_path} 大小={file_size} "
            f"标题={options.title!r} 简介={options.caption!r} "
            f"话题={options.hashtags!r} 可见性={options.visibility_type} "
            f"允许下载={options.download} 定时={options.timing}"
        )
        _log("=" * 60)

        # 发布前先验证登录，避免传完视频才报「用户未登录」
        try:
            creds = self.get_upload_auth()
        except RuntimeError as e:
            logger.warning("登录校验失败: %s", e)
            raise RuntimeError(
                "登录校验失败"
            ) from e
        _log("[步骤] 1/8 获取上传凭证成功")

        info = self.apply_upload(creds, file_size)
        _log(f"[步骤] 2/8 申请上传地址 vid={info['vid']}")

        self.tos_upload_video(info, video_path)
        _log("[步骤] 3/8 视频分片上传完成")

        video_id = self.commit_upload(creds, info["session_key"])
        _log(f"[步骤] 4/8 视频入库完成 video_id={video_id}")

        # 入库后立刻确认转码，避免封面上传完了才发现 encode 永久为 0
        self.wait_video_ready(video_id, timeout=30)
        _log("[步骤] 4.5/8 转码就绪确认通过")

        # 封面可选：未传时尝试视频同名 .jpg；仍无则用空 poster + poster_delay 取帧
        if cover_path is None:
            guess = video_path.with_suffix(".jpg")
            if guess.is_file():
                cover_path = guess

        poster = ""
        cover_width = 0
        cover_height = 0
        if cover_path is not None and cover_path.is_file():
            _log(f"[步骤] 5/8 封面={cover_path} 大小={cover_path.stat().st_size}")
            cover_creds = self.get_upload_auth()
            cover_meta = self.upload_cover(cover_creds, cover_path)
            poster = cover_meta["uri"]
            cover_width = int(cover_meta.get("width") or 0)
            cover_height = int(cover_meta.get("height") or 0)
            _log(
                f"[步骤] 6-7/8 封面上传完成 poster={poster} "
                f"{cover_width}x{cover_height}"
            )
        else:
            _log(
                f"[步骤] 5-7/8 未提供封面，使用默认取帧 "
                f"poster_delay={options.poster_delay}"
            )

        result = self.create_aweme(
            video_id,
            poster,
            options,
            cover_width=cover_width,
            cover_height=cover_height,
        )
        _log(f"[完成] 耗时={(time.time() - t_all):.1f}秒 作品ID={result.get('item_id')}")
        return result

    def publish_images(
        self,
        image_paths: list[Path],
        options: PublishOptions,
        cover_path: Optional[Path] = None,
        workers: int = 4,
    ) -> dict[str, Any]:
        """发布图文：ImageX 上传多图（可并发）→ create_v2 (media_type=2)。

        workers: 并发上传线程数，1=串行；建议 3~8。
        """
        if not image_paths:
            raise ValueError("至少提供 1 张图片")
        if len(image_paths) > 35:
            raise ValueError("最多 35 张图片")
        for p in image_paths:
            if not p.is_file():
                raise FileNotFoundError(p)
        options.normalize_title_caption(IMAGE_TITLE_MAX_LEN)

        t_all = time.time()
        workers = max(1, min(int(workers), len(image_paths), 8))
        _log("=" * 60)
        _log(
            f"[开始] 图文张数={len(image_paths)} 并发={workers} "
            f"标题={options.title!r} 简介={options.caption!r} "
            f"可见性={options.visibility_type} 定时={options.timing}"
        )
        _log("=" * 60)

        try:
            # 先解析 uid，再拿 STS，避免并发时重复探测
            self.ensure_user_id()
            creds = self.get_upload_auth()
        except RuntimeError as e:
            logger.warning("登录校验失败: %s", e)
            raise RuntimeError(
                "登录校验失败"
            ) from e
        _log("[步骤] 1/3 获取上传凭证成功")

        def _upload_one(idx: int, path: Path) -> tuple[int, dict[str, Any]]:
            sess = self._image_http_session() if workers > 1 else self.session
            meta = self.upload_image(creds, path, session=sess)
            if not meta.get("width") or not meta.get("height"):
                logger.warning("图片元数据缺失: %s -> %s", path, meta)
                raise RuntimeError(f"图片元数据缺失: {path}")
            return idx, {
                "uri": meta["uri"],
                "width": meta["width"],
                "height": meta["height"],
            }

        images: list[Optional[dict[str, Any]]] = [None] * len(image_paths)
        if workers == 1:
            for i, path in enumerate(image_paths):
                idx, item = _upload_one(i, path)
                images[idx] = item
                _log(
                    f"[步骤] 2/3 已上传 {idx + 1}/{len(image_paths)} "
                    f"{path.name} → {item['uri']}"
                )
        else:
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [
                    pool.submit(_upload_one, i, p) for i, p in enumerate(image_paths)
                ]
                for fut in as_completed(futs):
                    idx, item = fut.result()
                    images[idx] = item
                    done += 1
                    _log(
                        f"[步骤] 2/3 已上传 {done}/{len(image_paths)} "
                        f"（第{idx + 1}张 {image_paths[idx].name}） → {item['uri']}"
                    )

        assert all(images), "部分图片上传失败"
        uploaded: list[dict[str, Any]] = [x for x in images if x is not None]

        poster = uploaded[0]["uri"]
        if cover_path is not None:
            if not cover_path.is_file():
                raise FileNotFoundError(cover_path)
            cover_creds = self.get_upload_auth()
            poster = self.upload_cover(cover_creds, cover_path)["uri"]
            _log(f"[步骤] 2.5/3 自定义封面 poster={poster}")

        result = self.create_image_aweme(uploaded, poster, options)
        _log(f"[完成] 耗时={(time.time() - t_all):.1f}秒 作品ID={result.get('item_id')}")
        return result


def load_cookie(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    # 允许 cookies.example 风格的注释行
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return " ".join(lines) if len(lines) > 1 else (lines[0] if lines else "")


def _cleanup_temp_media() -> None:
    for p in list(_TEMP_MEDIA):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    _TEMP_MEDIA.clear()


def _is_http_url(s: str) -> bool:
    try:
        u = urlparse(s.strip())
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def _guess_media_ext(url: str, content_type: str, kind: str) -> str:
    path = unquote(urlparse(url).path)
    suf = Path(path).suffix.lower()
    if suf == ".jpeg":
        return ".jpg"
    if suf in {".mp4", ".mov", ".webm", ".mkv", ".avi", ".jpg", ".png", ".webp", ".gif"}:
        return suf
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[ct]
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[guessed]
    return ".mp4" if kind == "video" else ".jpg"


def resolve_media(src: str, *, kind: str = "image") -> Path:
    """本地路径或 http(s) URL → 本地 Path（URL 会下载到临时文件，进程退出时删除）。"""
    s = (src or "").strip().strip('"').strip("'")
    if not s:
        raise ValueError(f"空的{kind}路径/URL")
    if _is_http_url(s):
        _log(f"[素材] 正在下载{'视频' if kind == 'video' else ('图片' if kind == 'image' else kind)}: {_clip(s, 120)}")
        with requests.get(
            s,
            stream=True,
            timeout=180,
            headers={"User-Agent": UA},
        ) as r:
            r.raise_for_status()
            ext = _guess_media_ext(s, r.headers.get("Content-Type", ""), kind)
            fd, name = tempfile.mkstemp(prefix=f"douyin_{kind}_", suffix=ext)
            os.close(fd)
            path = Path(name)
            written = 0
            try:
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            f.flush()
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        if written <= 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"下载为空: {s}")
        _TEMP_MEDIA.append(path)
        _log(f"[素材] 下载完成 {'视频' if kind == 'video' else ('图片' if kind == 'image' else kind)} → {path.name} 大小={written}")
        return path
    path = Path(s)
    if not path.is_file():
        raise FileNotFoundError(f"{kind} 文件不存在: {path}")
    return path
