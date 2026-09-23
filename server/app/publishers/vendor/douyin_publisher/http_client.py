"""HTTP 客户端：优先 curl_cffi 伪装 Chrome TLS/HTTP2，回退 requests。

环境变量:
  DOUYIN_CURL_IMPERSONATE   目标指纹，默认 chrome131；设 0/off 强制用 requests
  DOUYIN_SESSION_DTRAIT     浏览器导出的 x-tt-session-dtrait 全文
  DOUYIN_SESSION_DTRAIT_FILE  上述值所在文件路径
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 与 curl_cffi 常见可用指纹对齐（旧版库无 chrome142+）
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_VERSION = UA[len("Mozilla/") :] if UA.startswith("Mozilla/") else UA

SEC_CH_UA = '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_PLATFORM = '"Windows"'

_IMPERSONATE_CANDIDATES = (
    "chrome131",
    "chrome124",
    "chrome120",
    "chrome119",
    "chrome116",
    "chrome110",
    "chrome",
)

_CURL_CFFI = None
_CURL_OK: Optional[bool] = None
_ACTIVE_IMPERSONATE: Optional[str] = None


def _env_disabled() -> bool:
    v = (os.environ.get("DOUYIN_CURL_IMPERSONATE") or "").strip().lower()
    return v in ("0", "off", "false", "no", "requests")


def _wanted_impersonate() -> str:
    v = (os.environ.get("DOUYIN_CURL_IMPERSONATE") or "").strip()
    if not v or v.lower() in ("1", "on", "true", "yes", "chrome"):
        return "chrome131"
    return v


def _try_import_curl() -> bool:
    global _CURL_CFFI, _CURL_OK
    if _CURL_OK is not None:
        return _CURL_OK
    if _env_disabled():
        _CURL_OK = False
        return False
    try:
        from curl_cffi import requests as curl_requests  # type: ignore

        _CURL_CFFI = curl_requests
        _CURL_OK = True
    except Exception as e:
        logger.warning("curl_cffi 不可用，回退 requests: %s", e)
        _CURL_OK = False
    return bool(_CURL_OK)


def tls_backend_info() -> dict[str, Any]:
    if _env_disabled():
        return {"backend": "requests", "impersonate": None, "reason": "disabled"}
    if not _try_import_curl():
        return {"backend": "requests", "impersonate": None, "reason": "import_failed"}
    return {
        "backend": "curl_cffi",
        "impersonate": _ACTIVE_IMPERSONATE or _wanted_impersonate(),
        "reason": "ok",
    }


def chrome_request_headers(*, referer: str = "https://creator.douyin.com/") -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://creator.douyin.com",
        "Referer": referer,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def load_session_dtrait() -> str:
    raw = (os.environ.get("DOUYIN_SESSION_DTRAIT") or "").strip()
    if raw:
        return raw
    path = (os.environ.get("DOUYIN_SESSION_DTRAIT_FILE") or "").strip()
    if not path:
        cand = Path(__file__).resolve().parent / "session_dtrait.txt"
        path = str(cand) if cand.is_file() else ""
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def _probe_session(curl_requests: Any, name: str, headers: dict[str, str]) -> Any:
    """部分 curl_cffi 版本在 Session() 时不校验 impersonate，要到 request 才抛错。"""
    sess = curl_requests.Session(impersonate=name)
    sess.headers.update(headers)
    # 轻量探测：失败则抛 ImpersonateError / 其它异常
    sess.request("HEAD", "https://creator.douyin.com/", timeout=15)
    return sess


def create_session(
    cookie: str = "",
    *,
    referer: str = "https://creator.douyin.com/",
    impersonate: Optional[str] = None,
) -> Any:
    """创建带 Chrome TLS（若可用）的 Session。"""
    global _ACTIVE_IMPERSONATE
    headers = chrome_request_headers(referer=referer)
    if cookie:
        headers["Cookie"] = cookie.strip()

    if _try_import_curl() and _CURL_CFFI is not None:
        wanted = impersonate or _wanted_impersonate()
        candidates = [wanted] + [c for c in _IMPERSONATE_CANDIDATES if c != wanted]
        last_err: Optional[Exception] = None
        for name in candidates:
            try:
                sess = _probe_session(_CURL_CFFI, name, headers)
                _ACTIVE_IMPERSONATE = name
                logger.info("HTTP TLS: curl_cffi impersonate=%s", name)
                return sess
            except Exception as e:
                last_err = e
                try:
                    # 关掉半开 session
                    pass
                except Exception:
                    pass
                continue
        logger.warning(
            "curl_cffi impersonate 全部失败 (%s)，回退 requests: %s",
            candidates[:5],
            last_err,
        )

    import requests

    sess = requests.Session()
    sess.headers.update(headers)
    _ACTIVE_IMPERSONATE = None
    logger.info("HTTP TLS: requests（无 Chrome 指纹伪装）")
    return sess


def http_post(
    url: str,
    *,
    data: Any = None,
    headers: Optional[dict] = None,
    cookie: str = "",
    timeout: float = 20.0,
    params: Optional[dict] = None,
) -> Any:
    """一次性 POST（mssdk 换票等），优先 curl_cffi。"""
    sess = create_session(cookie, referer="https://creator.douyin.com/")
    try:
        return sess.post(
            url,
            params=params,
            data=data,
            headers=headers,
            timeout=timeout,
        )
    finally:
        try:
            sess.close()
        except Exception:
            pass
