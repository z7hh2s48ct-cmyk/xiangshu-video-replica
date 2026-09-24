"""
微信视频号助手 - 接口发布视频 / 图文

关键参数说明（非传统“加密”，而是接口协议参数）：
1. X-WECHAT-UIN  = helper_upload_params 返回的 data.uin（页面里叫 fakeUin）
2. Authorization = helper_upload_params 返回的 data.authKey（CDN 上传凭证）
3. X-Arguments   = apptype/filetype/weixinnum/filekey/filesize/taskid/scene 拼接
4. finger-print-device-id = 浏览器指纹（任意稳定 32 位 hex 即可）
5. _aid / _rid   = 会话 UUID / 时间戳十六进制+随机串
6. traceKey      = get-finder-post-trace-key 返回
7. clipKey/draftId = post_clip_video 返回，用作 videoClipTaskId（仅视频）

图文（finderNewLifeCreate）：
- mediaType=2，同一 /post/post_create（micro）
- 图片走 CDN pictureFileType，无需 clip
- objectDesc.finderNewlifeDesc 必填 richTextTitle / richTextJson
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import struct
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, unquote, urlencode, urlparse

import requests

from .china_city_centers import CITY_CENTER as _CITY_CENTER

logger = logging.getLogger("channels_publisher")


def setup_logging(level: int = logging.INFO) -> None:
    """配置控制台日志；根 logger 已有 handler 时不再重复 basicConfig。"""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root.setLevel(level)
    logger.setLevel(level)

# location:
#   None / False / {}  -> 不显示位置（默认）
#   True              -> 自动取附近第一条
#   "石家庄"           -> 按关键词搜索取第一条
#   {"query": "..."}  -> 同上
#   {"latitude","longitude","city","poiClassifyId"?} -> 手动指定
LocationConfig = Union[None, bool, str, Dict[str, Any]]

# schedule / effectiveTime（Unix 秒）:
#   None              -> 立即发表
#   1710000000        -> 定时时间戳（秒）
#   "2026-08-10 18:00" / ISO 字符串 / datetime
ScheduleConfig = Union[None, int, float, str, datetime]


BASE = "https://channels.weixin.qq.com"

# 作品可见范围（post_list.visibleType / post_update_visible）
VISIBLE_PUBLIC = 1  # 公开可见
VISIBLE_FOLLOW = 2  # 仅粉丝可见
VISIBLE_SELF = 3  # 仅自己可见

# post_list.userpageType（前端 M8）
USERPAGE_NORMAL = 0
USERPAGE_PHOTO = 10  # 图文
USERPAGE_VIDEO = 11  # 视频

MEDIA_TYPE_IMAGE = 2
MEDIA_TYPE_VIDEO = 4

CREATE_PAGE = f"{BASE}/platform/post/create"
NEWLIFE_PAGE = f"{BASE}/platform/post/finderNewLifeCreate"
NEWLIFE_MICRO_PAGE = f"{BASE}/micro/content/post/finderNewLifeCreate"

CGI = f"{BASE}/cgi-bin/mmfinderassistant-bin"
MICRO_CGI = f"{BASE}/micro/content/cgi-bin/mmfinderassistant-bin"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

CHUNK_SIZE = 8 * 1024 * 1024  # 视频分片 8MB，与前端一致
CDN_REPLACE_FROM = "http://wxapp.tc.qq.com"
CDN_REPLACE_TO = "https://finder.video.qq.com"


def generate_rid() -> str:
    ts_hex = hex(int(time.time()))[2:]
    rand = "".join(random.choice("0123456789abcdef") for _ in range(8))
    return f"{ts_hex}-{rand}"


def generate_aid() -> str:
    return str(uuid.uuid4())


def generate_fingerprint() -> str:
    return uuid.uuid4().hex


def ts_ms() -> str:
    return str(int(time.time() * 1000))


def fmt_elapsed(seconds: float) -> str:
    """格式化耗时：如 12.3s / 1m 05.2s。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {s:04.1f}s"


def parse_mp4_meta(path: str) -> Tuple[int, int, float]:
    """解析 mp4 宽高与时长，避免依赖 ffprobe；只读 moov，不整文件进内存。"""

    def walk(buf: bytes, offset: int = 0, end: Optional[int] = None):
        if end is None:
            end = len(buf)
        pos = offset
        while pos + 8 <= end:
            size = struct.unpack(">I", buf[pos : pos + 4])[0]
            typ = buf[pos + 4 : pos + 8]
            if size == 1:
                size = struct.unpack(">Q", buf[pos + 8 : pos + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - pos
                hdr = 8
            else:
                hdr = 8
            if size < hdr:
                break
            yield typ, pos, size, hdr
            if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                yield from walk(buf, pos + hdr, pos + size)
            pos += size

    moov = None
    with open(path, "rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:]
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                body_len = size - 16
            elif size == 0:
                body = f.read()
                if typ == b"moov":
                    moov = body
                break
            else:
                body_len = size - 8
            if typ == b"moov":
                moov = f.read(body_len)
                break
            f.seek(body_len, 1)

    if not moov:
        raise ValueError(f"无法找到 moov: {path}")

    width = height = None
    duration = timescale = None
    for typ, pos, size, hdr in walk(moov):
        if typ == b"tkhd":
            version = moov[pos + hdr]
            if version == 0:
                w = struct.unpack(">I", moov[pos + hdr + 76 : pos + hdr + 80])[0]
                h = struct.unpack(">I", moov[pos + hdr + 80 : pos + hdr + 84])[0]
            else:
                w = struct.unpack(">I", moov[pos + hdr + 88 : pos + hdr + 92])[0]
                h = struct.unpack(">I", moov[pos + hdr + 92 : pos + hdr + 96])[0]
            if (w >> 16) and (h >> 16):
                width, height = w >> 16, h >> 16
        if typ == b"mvhd":
            version = moov[pos + hdr]
            if version == 0:
                timescale = struct.unpack(">I", moov[pos + hdr + 12 : pos + hdr + 16])[0]
                duration = struct.unpack(">I", moov[pos + hdr + 16 : pos + hdr + 20])[0]
            else:
                timescale = struct.unpack(">I", moov[pos + hdr + 20 : pos + hdr + 24])[0]
                duration = struct.unpack(">Q", moov[pos + hdr + 24 : pos + hdr + 32])[0]

    if not width or not height or not timescale or duration is None:
        raise ValueError(f"无法解析视频元数据: {path}")
    return int(width), int(height), float(duration) / float(timescale)


def calc_target_size(width: int, height: int) -> Tuple[int, int]:
    long_side = max(width, height)
    short_side = min(width, height)
    max_long, max_short = 1920, 1080
    if long_side > max_long or short_side > max_short:
        scale = min(max_long / long_side, max_short / short_side)
        n = math.floor(long_side * scale)
        r = math.floor(short_side * scale)
        if width > height:
            return n, r
        return r, n
    return width, height


def parse_image_size(path: str) -> Tuple[int, int]:
    """读取图片宽高；优先 Pillow，失败则解析 JPEG/PNG 头。"""
    p = Path(path)
    try:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
            if w > 0 and h > 0:
                return int(w), int(h)
    except Exception:
        pass

    data = p.read_bytes()
    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    # JPEG SOF
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xD9:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return int(w), int(h)
            if marker == 0x00 or marker == 0x01 or (0xD0 <= marker <= 0xD9):
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg_len
    raise RuntimeError(f"无法解析图片尺寸: {path}")


def is_http_url(value: str) -> bool:
    """是否为 http(s) 远程地址。"""
    s = (value or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _guess_filename_from_url(url: str, default_name: str) -> str:
    path = unquote(urlparse(url).path or "")
    name = Path(path).name
    if name and "." in name and len(name) < 180:
        return name
    return default_name


def _ext_from_content_type(content_type: str, fallback: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "video/webm": ".webm",
    }
    return mapping.get(ct, fallback)


def download_url_to_temp(
    url: str,
    *,
    default_name: str = "media.bin",
    session: Optional[requests.Session] = None,
    timeout: int = 120,
) -> Tuple[str, str]:
    """
    下载远程文件到临时路径。
    Returns:
        (local_path, file_name) 调用方负责删除 local_path。
    """
    url = (url or "").strip()
    if not is_http_url(url):
        raise ValueError(f"不是有效的 http(s) URL: {url}")

    sess = session or requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": BASE + "/",
    }
    logger.info("下载远程文件: %s", url[:160])
    with sess.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        file_name = _guess_filename_from_url(url, default_name)
        suffix = Path(file_name).suffix.lower()
        if not suffix or suffix == ".bin":
            suffix = _ext_from_content_type(
                resp.headers.get("Content-Type", ""),
                Path(default_name).suffix or ".bin",
            )
            if not Path(file_name).suffix:
                file_name = f"{Path(default_name).stem}{suffix}"
        fd, tmp_path = tempfile.mkstemp(prefix="channels_", suffix=suffix)
        os.close(fd)
        written = 0
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    if written <= 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise RuntimeError(f"下载为空: {url}")
    logger.info("下载完成: %s bytes -> %s", written, tmp_path)
    return tmp_path, file_name


def resolve_local_media(
    source: str,
    *,
    default_name: str = "media.bin",
    session: Optional[requests.Session] = None,
) -> Tuple[str, Optional[str], str]:
    """
    将本地路径或 http(s) URL 解析为本地文件。

    Returns:
        (local_path, temp_path_to_cleanup_or_None, file_name)
    """
    src = str(source or "").strip()
    if not src:
        raise ValueError("媒体路径/URL 不能为空")
    if is_http_url(src):
        local_path, file_name = download_url_to_temp(
            src, default_name=default_name, session=session
        )
        return local_path, local_path, file_name
    path = Path(src)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {src}")
    return str(path.resolve()), None, path.name or default_name


def build_follow_post_info(music_id: Union[str, int]) -> Dict[str, Any]:
    """图文配乐：前端写入 objectDesc.followPostInfo，核心只要音乐 ID（docId）。

    docId 来自 BGM 的 listenId（优先）或 musicSid。
    """
    doc_id = str(music_id).strip()
    if not doc_id:
        raise ValueError("music_id 不能为空")
    return {
        "musicInfo": {
            "docId": doc_id,
            "albumThumbUrl": "",
            "name": "",
            "artist": "",
            "mediaStreamingUrl": "",
            "docType": 0,
        },
        "groupId": doc_id,
        "hasBgm": 1,
        "bgmSource": 0,
        "bgmQuery": "",
    }


def build_newlife_rich_text_json(title: str, description: str = "") -> str:
    """构造图文 richTextJson（Quill Delta，与前端 genTitleRichText + content 一致）。"""
    ops: List[Dict[str, Any]] = []
    title = (title or "").strip()
    desc = (description or "").rstrip()
    if title:
        ops.append({"insert": title})
        ops.append({"attributes": {"header": 1}, "insert": "\n"})
    if desc:
        ops.append({"insert": desc if desc.endswith("\n") else desc + "\n"})
    # 前端 merge 后保证以 \\n\\n 结尾
    if not ops:
        ops.append({"insert": "\n\n"})
    else:
        last = ops[-1].get("insert")
        if not (isinstance(last, str) and last.endswith("\n\n")):
            ops.append({"insert": "\n\n"})
    return json.dumps(ops, ensure_ascii=False)


def parse_schedule_time(schedule: ScheduleConfig) -> Optional[int]:
    """解析定时发表时间，返回 Unix 秒；None 表示立即发表。

    若定时时间 <= 当前时间 + 30 秒，视为立即发表（返回 None）。
    """
    if schedule is None or schedule is False:
        return None
    if isinstance(schedule, datetime):
        ts = int(schedule.timestamp())
    elif isinstance(schedule, (int, float)):
        ts = int(schedule)
        # 兼容误传毫秒
        if ts > 10_000_000_000:
            ts //= 1000
    elif isinstance(schedule, str):
        s = schedule.strip()
        if not s:
            return None
        if s.isdigit():
            ts = int(s)
            if ts > 10_000_000_000:
                ts //= 1000
        else:
            dt = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
            ):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                # 尝试 fromisoformat
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                except ValueError as e:
                    raise ValueError(f"无法解析定时时间: {schedule}") from e
            ts = int(dt.timestamp())
    else:
        raise TypeError(f"不支持的 schedule 类型: {type(schedule)}")

    # 距现在不足 30 秒（含已过期）→ 立即发表
    if ts <= int(time.time()) + 30:
        logger.info(
            "定时时间 %s 距现在不足 30s，改为立即发表",
            schedule,
        )
        return None
    return ts


def normalize_location_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """统一位置字段，供 objectDesc.location 使用（与前端一致）。"""
    if not item:
        return {}
    latitude = float(item.get("latitude") or 0)
    longitude = float(item.get("longitude") or 0)
    city = item.get("city") or item.get("cityName") or ""
    poi = (
        item.get("poiClassifyId")
        or item.get("uid")
        or item.get("poiId")
        or ""
    )
    # 前端提交: latitude/longitude/city/poiName/address/poiClassifyId
    poi_name = item.get("poiName") or item.get("name") or ""
    address = item.get("address") or item.get("fullAddress") or ""
    if not city and not latitude and not longitude and not poi_name:
        return {}
    return {
        "latitude": latitude,
        "longitude": longitude,
        "city": city,
        "poiName": poi_name,
        "address": address,
        "poiClassifyId": poi,
    }


def _norm_place_name(name: str) -> str:
    """去掉省/市/区等后缀，便于城市匹配。"""
    s = (name or "").strip()
    for suf in ("特别行政区", "自治州", "地区", "盟", "省", "市", "区", "县"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def _parse_place_query(query: str) -> Tuple[str, str, str]:
    """
    解析位置查询为 (city, district, bare)。
    例：
      石家庄市栾城区 -> (石家庄市, 栾城区, 石家庄栾城)
      栾城区         -> (, 栾城区, 栾城)
      北京市         -> (北京市, , 北京)
    """
    q = (query or "").strip()
    city = ""
    district = ""
    m = re.search(r"^(.+?市)(.+?(?:区|县|旗))$", q)
    if m:
        city, district = m.group(1), m.group(2)
    elif q.endswith(("区", "县", "旗")) and len(q) > 1:
        district = q
    elif q.endswith("市") and len(q) > 1:
        city = q
    bare = _norm_place_name(district) if district else _norm_place_name(city or q)
    if city and district:
        bare = f"{_norm_place_name(city)}{_norm_place_name(district)}"
    return city, district, bare


def _city_center(query: str) -> Optional[Tuple[float, float]]:
    """查城市中心坐标。数据见 china_city_centers.CITY_CENTER（全国省/地级市）。"""
    q = (query or "").strip()
    if not q:
        return None
    city, _district, bare = _parse_place_query(q)
    for key in (q, city, _norm_place_name(city) if city else "", bare, f"{bare}市"):
        if key and key in _CITY_CENTER:
            return _CITY_CENTER[key]
    return None


def _is_admin_place_query(query: str) -> bool:
    """是否像行政区查询（省/市/区县），否则视为 POI 关键词。"""
    q = (query or "").strip()
    if not q:
        return False
    city_q, district_q, _ = _parse_place_query(q)
    if city_q or district_q:
        return True
    return q in _CITY_CENTER or _norm_place_name(q) in _CITY_CENTER


def _poi_keywords(query: str) -> List[str]:
    """生成 POI 匹配词：原词 + 去掉业态后缀（厅/店/馆…）。如「咖啡厅」→「咖啡」。"""
    q = (query or "").strip()
    if not q:
        return []
    keys = [q]
    stem = q
    for suf in ("中心", "广场", "大厦", "厅", "店", "馆", "吧", "屋", "苑", "园", "场"):
        if stem.endswith(suf) and len(stem) > len(suf):
            stem = stem[: -len(suf)]
            break
    if stem and stem not in keys:
        keys.append(stem)
    return keys


def pick_best_location_item(query: str, items: List[Any]) -> Optional[Dict[str, Any]]:
    """
    从搜索结果中挑最合适的一条。
    - 行政区：按 city/region 同城匹配，避免跨城误匹配
    - POI 名（如「中国邮政集团」「咖啡厅」）：按名称/地址关键词匹配
    """
    # 接口偶发混入字符串，只保留 dict POI
    dict_items = [it for it in (items or []) if isinstance(it, dict)]
    if not dict_items:
        return None
    q = (query or "").strip()
    city_q, district_q, bare = _parse_place_query(q)
    if not bare and not city_q and not district_q:
        return dict_items[0]

    def city_of(it: Dict[str, Any]) -> str:
        return str(it.get("city") or it.get("cityName") or "").strip()

    def region_of(it: Dict[str, Any]) -> str:
        return str(it.get("region") or it.get("district") or "").strip()

    def name_of(it: Dict[str, Any]) -> str:
        return str(it.get("name") or it.get("poiName") or "").strip()

    def addr_of(it: Dict[str, Any]) -> str:
        return str(it.get("fullAddress") or it.get("address") or "").strip()

    # POI 关键词：不要求 city==query，按名称命中即可
    if not _is_admin_place_query(q):
        keys = _poi_keywords(q)
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for it in dict_items:
            n, a = name_of(it), addr_of(it)
            score = 0
            for kw in keys:
                if not kw:
                    continue
                if n == kw or n == q:
                    score = max(score, 100)
                elif n.startswith(kw) or kw.startswith(n):
                    score = max(score, 80)
                elif kw in n:
                    score = max(score, 60)
                elif kw in a:
                    score = max(score, 40)
            if score:
                scored.append((score, it))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[0][1]
        # 接口已按关键词召回时，取第一条（如「咖啡厅」->「瑞幸咖啡」）
        return dict_items[0]

    def same_city(it: Dict[str, Any]) -> bool:
        c = city_of(it)
        if not c:
            return False
        if city_q:
            return c == city_q or _norm_place_name(c) == _norm_place_name(city_q)
        # 仅区县查询：city 含关键词，或 region 精确命中
        if district_q:
            return (
                region_of(it) == district_q
                or district_q in c
                or district_q in addr_of(it)
                or _norm_place_name(district_q) in addr_of(it)
            )
        # 纯城市名
        return (
            c == q
            or _norm_place_name(c) == bare
            or c.startswith(bare)
            or bare in c
        )

    def same_district(it: Dict[str, Any]) -> bool:
        if not district_q:
            return False
        r = region_of(it)
        dn = _norm_place_name(district_q)
        return (
            r == district_q
            or _norm_place_name(r) == dn
            or district_q in name_of(it)
            or district_q in addr_of(it)
            or (dn and dn in name_of(it))
            or (dn and dn in addr_of(it))
        )

    candidates = [it for it in dict_items if same_city(it)]
    if not candidates:
        return None

    if district_q:
        in_district = [it for it in candidates if same_district(it)]
        if in_district:
            candidates = in_district

    # 名称就是查询/城市/区县本身的优先
    for it in candidates:
        n = name_of(it)
        if n in (q, city_q, district_q) or _norm_place_name(n) == bare:
            return it
        if district_q and n == region_of(it):
            return it
    return candidates[0]


def _as_location_item_list(raw: Any) -> List[Dict[str, Any]]:
    """把搜索接口返回整理成 POI dict 列表。

    注意：helper_search_location 可能返回
    - list: [poi, ...]
    - address: [poi, ...] 或单个 poi dict
    不能对空 list 再 `or address`，否则 dict 会被 list() 拆成 key 字符串。
    """
    if raw is None:
        return []

    candidates: List[Any] = []
    if isinstance(raw, dict):
        lst = raw.get("list")
        addr = raw.get("address")
        if isinstance(lst, list) and lst:
            candidates = lst
        elif isinstance(addr, list) and addr:
            candidates = addr
        elif isinstance(addr, dict) and addr:
            candidates = [addr]
        elif isinstance(lst, list):
            candidates = lst
        else:
            # 本身就是一条 POI
            if any(k in raw for k in ("latitude", "longitude", "city", "uid", "name")):
                candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw

    return [it for it in candidates if isinstance(it, dict)]


def _windows_short_path(path: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        GetShortPathNameW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(512)
        if GetShortPathNameW(path, buf, len(buf)):
            return buf.value
    except Exception:
        pass
    return path


def extract_cover_jpeg(video_path: str, out_path: str, at_sec: float = 0.0) -> str:
    """从视频取一帧作为封面（优先非黑帧，兼容 Windows 中文路径）。"""
    import cv2
    import numpy as np

    open_path = _windows_short_path(str(Path(video_path).resolve()))
    cap = cv2.VideoCapture(open_path)
    tmp_video = None
    if not cap.isOpened():
        tmp_video = Path(out_path).with_name("_tmp_upload_video.mp4")
        tmp_video.write_bytes(Path(video_path).read_bytes())
        cap = cv2.VideoCapture(str(tmp_video))
    try:
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        start_idx = int(max(0, at_sec) * fps)

        def is_black(frame) -> bool:
            # 接近网页 generateFirstNonBlackFrame：跳过近乎全黑帧
            return float(np.mean(frame)) < 8.0

        chosen = None
        # 从 start_idx 起向后扫少量帧，找第一帧非黑画面
        for idx in range(start_idx, min(start_idx + max(int(fps * 3), 30), max(total, start_idx + 1)) or (start_idx + 1)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                break
            chosen = frame
            if not is_black(frame):
                break

        if chosen is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, chosen = cap.read()
            if not ok:
                raise RuntimeError("无法从视频读取封面帧")

        ok, buf = cv2.imencode(".jpg", chosen, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        Path(out_path).write_bytes(buf.tobytes())
        return out_path
    finally:
        cap.release()
        if tmp_video is not None and tmp_video.exists():
            tmp_video.unlink()


class ChannelsPublisher:
    def __init__(
        self,
        cookie: str,
        fingerprint: Optional[str] = None,
        aid: Optional[str] = None,
    ):
        self.cookie = cookie.strip()
        self.fingerprint = fingerprint or generate_fingerprint()
        self.aid = aid or generate_aid()
        self.session = requests.Session()
        self.finder_id = ""
        self.uin = ""
        self.auth_key = ""
        self.upload_params: Dict[str, Any] = {}
        self.cdn_host = "finderassistancea.video.qq.com"
        self.cdn_hosts: List[str] = []
        self.location: Dict[str, Any] = {}

    def _common_body(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "timestamp": ts_ms(),
            "_log_finder_uin": "",
            "_log_finder_id": self.finder_id,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
        }
        if extra:
            body.update(extra)
        return body

    def _headers(self, referer: str, uin: Optional[str] = None) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": referer,
            "User-Agent": USER_AGENT,
            "X-WECHAT-UIN": uin if uin is not None else (self.uin or "0000000000"),
            "finger-print-device-id": self.fingerprint,
            "Cookie": self.cookie,
        }

    def _query(self, page_url: str) -> Dict[str, str]:
        return {
            "_aid": self.aid,
            "_rid": generate_rid(),
            "_pageUrl": page_url,
        }

    def _post_json(
        self,
        url: str,
        body: Dict[str, Any],
        referer: str,
        use_micro: bool = False,
        uin: Optional[str] = None,
        page_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        # micro 内容页接口的 _pageUrl 需与前端一致
        if page_url is None:
            page_url = (
                f"{BASE}/micro/content/post/create"
                if use_micro
                else referer
            )
        full = url if url.startswith("http") else ((MICRO_CGI if use_micro else CGI) + url)
        resp = self.session.post(
            full,
            params=self._query(page_url),
            headers=self._headers(referer, uin=uin),
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errCode", 0) != 0:
            raise RuntimeError(f"接口失败 {full}: {data}")
        return data

    def auth_data(self) -> Dict[str, Any]:
        data = self._post_json(
            "/auth/auth_data",
            self._common_body(),
            referer=f"{BASE}/platform/post/create",
            uin="0000000000",
        )["data"]
        finder = data.get("finderUser") or data.get("userAttr") or {}
        self.finder_id = (
            finder.get("finderUsername")
            or data.get("finderUsername")
            or self.finder_id
        )
        # 部分返回结构在 userAttr / finderList
        if not self.finder_id and data.get("finderList"):
            self.finder_id = data["finderList"][0].get("finderUsername", "")
        return data

    def helper_upload_params(self) -> Dict[str, Any]:
        data = self._post_json(
            "/helper/helper_upload_params",
            self._common_body(),
            referer=f"{BASE}/platform/post/create",
            uin="0000000000",
        )["data"]
        self.upload_params = data
        self.auth_key = data["authKey"]
        self.uin = str(data["uin"])  # 即 X-WECHAT-UIN / fakeUin
        return data

    def load_env_from_auth(self, auth: Dict[str, Any]) -> None:
        env = auth.get("envInfo") or {}
        if env.get("cdnHost"):
            self.cdn_host = env["cdnHost"]
        if env.get("cdnHostList"):
            self.cdn_hosts = list(env["cdnHostList"])

    def get_bgm_list(
        self,
        *,
        type_: int = 103,
        query: str = "",
        current_page: int = 1,
        page_size: int = 20,
        last_buffer: str = "",
        recommend_thumb_url_list: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """获取配乐列表。

        type_: 103=推荐，104=搜索（需 query），3=收藏，11=分类（query=categoryId）。
        返回 data.items[].listenItem，其中 playableInfo.listenId / musicSid 即 music_id。
        """
        body: Dict[str, Any] = {
            "type": int(type_),
            "query": query or "",
            "currentPage": int(current_page),
            "pageSize": int(page_size),
            "lastBuffer": last_buffer or "",
        }
        if type_ == 103:
            body["recommendThumbUrlList"] = recommend_thumb_url_list or []
        return self._post_json(
            "/post/get_bgm_list",
            self._common_body(body),
            referer=NEWLIFE_PAGE,
            use_micro=True,
            page_url=NEWLIFE_MICRO_PAGE,
        )["data"]

    def get_trace_key(
        self,
        *,
        referer: Optional[str] = None,
        page_url: Optional[str] = None,
    ) -> str:
        data = self._post_json(
            "/post/get-finder-post-trace-key",
            self._common_body({"objectId": ""}),
            referer=referer or CREATE_PAGE,
            use_micro=True,
            page_url=page_url,
        )["data"]
        return data["traceKey"]

    def post_list(
        self,
        page_size: int = 10,
        current_page: int = 1,
        userpage_type: int = 3,
    ) -> Dict[str, Any]:
        return self._post_json(
            "/post/post_list",
            self._common_body(
                {
                    "pageSize": page_size,
                    "currentPage": current_page,
                    "userpageType": int(userpage_type),
                }
            ),
            referer=f"{BASE}/platform/post/list",
            use_micro=True,
        )["data"]

    def get_object_short_link(
        self,
        export_id: str,
        nonce_id: str,
        scene: int = 40,
    ) -> str:
        """调用助手接口获取作品短链 https://weixin.qq.com/sph/..."""
        data = self._post_json(
            "/post/get_object_short_link",
            self._common_body(
                {
                    "exportId": export_id,
                    "nonceId": str(nonce_id),
                    "scene": scene,
                }
            ),
            referer=f"{BASE}/platform/post/list",
            use_micro=True,
        )["data"]
        short_url = data.get("shortUrl") or ""
        if not short_url:
            raise RuntimeError(f"未返回 shortUrl: {data}")
        return short_url

    def update_visible(
        self,
        export_id: str,
        visible_type: int = VISIBLE_PUBLIC,
    ) -> Dict[str, Any]:
        """修改作品可见范围：1公开 / 2仅粉丝 / 3仅自己。"""
        return self._post_json(
            "/post/post_update_visible",
            self._common_body(
                {
                    "objectId": export_id,
                    "visibleType": int(visible_type),
                }
            ),
            referer=f"{BASE}/platform/post/list",
            use_micro=True,
        )

    def find_published_post(
        self,
        *,
        description: str = "",
        title: str = "",
        media_md5: Optional[str] = None,
        since_ts: Optional[int] = None,
        userpage_type: int = 3,
        retries: int = 5,
        interval: float = 1.5,
    ) -> Optional[Dict[str, Any]]:
        """发布后从内容列表匹配刚发的作品。"""
        desc_key = (description or "").strip()
        title_key = (title or "").strip()
        for attempt in range(retries):
            items = (
                self.post_list(page_size=10, userpage_type=userpage_type).get("list")
                or []
            )
            for item in items:
                desc = item.get("desc") or {}
                media = (desc.get("media") or [{}])[0]
                item_desc = (desc.get("description") or "").strip()
                newlife = desc.get("finderNewlifeDesc") or {}
                item_title = (newlife.get("richTextTitle") or "").strip()
                item_md5 = media.get("md5sum") or ""
                create_time = int(item.get("createTime") or 0)
                if media_md5 and item_md5 and item_md5 == media_md5:
                    return item
                if title_key and title_key == item_title:
                    if since_ts is None or create_time >= since_ts - 30:
                        return item
                if desc_key and desc_key in item_desc:
                    if since_ts is None or create_time >= since_ts - 30:
                        return item
            if attempt + 1 < retries:
                time.sleep(interval)
        # 回退：取最近一条（仍可能是别人刚发的，优先上面精确匹配）
        items = (
            self.post_list(page_size=1, userpage_type=userpage_type).get("list") or []
        )
        return items[0] if items else None

    def resolve_publish_link(
        self,
        *,
        description: str = "",
        title: str = "",
        media_md5: Optional[str] = None,
        since_ts: Optional[int] = None,
        ensure_public: bool = True,
        userpage_type: int = 3,
    ) -> Dict[str, Any]:
        """发布成功后解析 exportId 与短链；默认确保作品为公开可见。"""
        item = self.find_published_post(
            description=description,
            title=title,
            media_md5=media_md5,
            since_ts=since_ts,
            userpage_type=userpage_type,
        )
        if not item:
            raise RuntimeError("发布成功但未在列表中找到作品")
        export_id = item.get("exportId") or item.get("objectId") or ""
        nonce_id = str(item.get("objectNonce") or "")
        if not export_id or not nonce_id:
            raise RuntimeError(f"作品缺少 exportId/objectNonce: {item.keys()}")

        visible_type = int(item.get("visibleType") or VISIBLE_PUBLIC)
        visible_name = {
            VISIBLE_PUBLIC: "公开可见",
            VISIBLE_FOLLOW: "仅粉丝可见",
            VISIBLE_SELF: "仅自己可见",
        }.get(visible_type, f"未知({visible_type})")
        logger.info("作品可见性=%s (%s)", visible_name, visible_type)

        if ensure_public and visible_type != VISIBLE_PUBLIC:
            logger.info("正在将作品设为公开可见 ...")
            self.update_visible(export_id, VISIBLE_PUBLIC)
            visible_type = VISIBLE_PUBLIC
            visible_name = "公开可见"

        short_url = self.get_object_short_link(export_id, nonce_id)
        return {
            "exportId": export_id,
            "objectNonce": nonce_id,
            "shortUrl": short_url,
            "feedUrl": (
                "https://channels.weixin.qq.com/mobile/extension/pages/feed.html"
                f"?feed_id={quote(export_id, safe='')}"
            ),
            "visibleType": visible_type,
            "visibleName": visible_name,
        }

    def search_location(
        self,
        query: str = "",
        longitude: float = 0,
        latitude: float = 0,
    ) -> Dict[str, Any]:
        data = self._post_json(
            "/helper/helper_search_location",
            self._common_body(
                {
                    "query": query or "",
                    "cookies": "",
                    "longitude": longitude,
                    "latitude": latitude,
                }
            ),
            referer=f"{BASE}/platform/post/create",
            use_micro=True,
        )["data"]
        self.location = data
        return data

    def _search_location_items(
        self,
        query: str,
        longitude: float = 0,
        latitude: float = 0,
    ) -> List[Dict[str, Any]]:
        data = self.search_location(query, longitude=longitude, latitude=latitude)
        return _as_location_item_list(data)

    def resolve_location(self, location: LocationConfig = None) -> Dict[str, Any]:
        """
        解析位置配置。
        默认不显示位置（返回 {}）；可传 True/关键词/坐标字典。
        """
        if location is None or location is False:
            return {}
        if location is True:
            items = self._search_location_items("")
            return normalize_location_item(items[0] if items else {})
        if isinstance(location, str):
            q = location.strip()
            if not q:
                return {}
            return self._resolve_location_query(q)
        if isinstance(location, dict):
            if location.get("query"):
                return self._resolve_location_query(str(location["query"]))
            # 已是坐标/城市
            if any(k in location for k in ("latitude", "longitude", "city", "poiClassifyId")):
                return normalize_location_item(location)
            return {}
        raise TypeError(f"不支持的 location 类型: {type(location)}")

    def _resolve_location_query(self, query: str) -> Dict[str, Any]:
        """按关键词搜索位置；城市名会校正到对应城市，避免 IP 偏置误匹配。

        说明：接口并不限制只能发 IP 所在地；但未传经纬度时结果会偏向当前定位，
        可能返回「名称含关键词、城市却是本地」的 POI。因此只接受同城结果。
        """
        q = query.strip()
        city_q, district_q, bare = _parse_place_query(q)
        if city_q:
            city_label = city_q
        elif district_q:
            city_label = district_q
        elif _is_admin_place_query(q):
            city_label = q if q.endswith(("市", "州", "盟", "县", "区")) else f"{bare}市"
        else:
            # POI 关键词：city 用结果里的城市，错误提示不再拼「xxx市」
            city_label = q

        # 依次尝试：原词 / 区县 / 城市；无命中再用城市中心坐标搜
        search_queries: List[str] = []
        for sq in (q, district_q, city_q, f"{bare}市" if bare else ""):
            if sq and sq not in search_queries:
                search_queries.append(sq)

        picked: Optional[Dict[str, Any]] = None
        last_items: List[Dict[str, Any]] = []
        for sq in search_queries:
            last_items = self._search_location_items(sq)
            picked = pick_best_location_item(q, last_items)
            if picked:
                break

        center = _city_center(q)
        if picked is None and center:
            lat, lng = center
            for sq in search_queries:
                last_items = self._search_location_items(
                    sq, longitude=lng, latitude=lat
                )
                picked = pick_best_location_item(q, last_items)
                if picked:
                    break
            if picked is None:
                # 有坐标即可发城市/区县级位置，不强制具体 POI
                logger.info("位置未命中具体 POI，使用城市中心: %s", city_label)
                return {
                    "latitude": lat,
                    "longitude": lng,
                    "city": city_label,
                    "poiName": district_q or "",
                    "address": "",
                    "poiClassifyId": "",
                }

        if picked is None and last_items:
            # 接口有结果但名称匹配过严未命中时，直接用第一条
            it0 = last_items[0]
            logger.info(
                "位置未精确命中，使用搜索第一条: %s/%s/%s",
                it0.get("city") or "",
                it0.get("region") or "",
                it0.get("name") or "",
            )
            picked = it0

        if picked is None:
            raise RuntimeError(
                f"未搜索到位置: {q}。"
                "请改用坐标字典，例如: "
                f'location={{"latitude": 38.04, "longitude": 114.51, "city": "石家庄市"}}'
            )
        return normalize_location_item(picked)

    def _x_arguments(self, file_name: str, file_size: int, task_id: str, file_type_key: str) -> str:
        filetype = self.upload_params[file_type_key]
        return urlencode(
            {
                "apptype": self.upload_params.get("appType", 251),
                "filetype": filetype,
                "weixinnum": self.uin,
                "filekey": file_name,
                "filesize": file_size,
                "taskid": task_id,
                "scene": self.upload_params.get("scene", 2),
            }
        )

    def _cdn_headers(self, file_name: str, file_size: int, task_id: str, file_type_key: str) -> Dict[str, str]:
        return {
            "Authorization": self.auth_key,
            "X-Arguments": self._x_arguments(file_name, file_size, task_id, file_type_key),
            "Content-MD5": "null",
            "User-Agent": USER_AGENT,
        }

    def _pick_cdn_host(self) -> str:
        hosts = self.cdn_hosts or [self.cdn_host]
        return random.choice(hosts)

    def upload_file(
        self,
        file_path: str,
        file_type_key: str = "videoFileType",
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = Path(file_path)
        file_size = path.stat().st_size
        file_name = file_name or path.name
        task_id = str(uuid.uuid4())
        host = self._pick_cdn_host()
        chunk_count = math.ceil(file_size / CHUNK_SIZE) or 1
        part_ends = [min(file_size, (i + 1) * CHUNK_SIZE) for i in range(chunk_count)]

        headers = self._cdn_headers(file_name, file_size, task_id, file_type_key)
        headers["Content-Type"] = "application/json"

        apply_url = f"https://{host}/applyuploaddfs"
        apply_resp = self.session.put(
            apply_url,
            headers=headers,
            json={"BlockSum": chunk_count, "BlockPartLength": part_ends},
            timeout=60,
        )
        apply_resp.raise_for_status()
        apply_json = apply_resp.json()
        upload_id = apply_json.get("UploadID") or apply_json.get("data", {}).get("UploadID")
        if not upload_id:
            raise RuntimeError(f"申请上传失败: {apply_json}")

        part_info = []
        with path.open("rb") as f:
            for part_num in range(1, chunk_count + 1):
                chunk = f.read(CHUNK_SIZE)
                upload_host = self._pick_cdn_host()
                put_url = (
                    f"https://{upload_host}/uploadpartdfs"
                    f"?PartNumber={part_num}&UploadID={quote(str(upload_id))}&QuickUpload=2"
                )
                put_headers = self._cdn_headers(file_name, file_size, task_id, file_type_key)
                put_headers["Content-Type"] = "application/octet-stream"
                put_resp = self.session.put(put_url, headers=put_headers, data=chunk, timeout=120)
                put_resp.raise_for_status()
                put_json = put_resp.json()
                etag = put_json.get("ETag") or put_json.get("data", {}).get("ETag")
                if not etag:
                    raise RuntimeError(f"分片上传失败 part={part_num}: {put_json}")
                part_info.append({"PartNumber": part_num, "ETag": etag})
                logger.info("分片 %s/%s ok", part_num, chunk_count)

        complete_url = f"https://{host}/completepartuploaddfs?UploadID={quote(str(upload_id))}"
        complete_headers = self._cdn_headers(file_name, file_size, task_id, file_type_key)
        complete_headers["Content-Type"] = "application/json"
        complete_resp = self.session.post(
            complete_url,
            headers=complete_headers,
            json={"TransFlag": "0_0", "PartInfo": part_info},
            timeout=60,
        )
        complete_resp.raise_for_status()
        complete_json = complete_resp.json()
        download_url = complete_json.get("DownloadURL") or complete_json.get("data", {}).get("DownloadURL")
        if not download_url:
            raise RuntimeError(f"完成上传失败: {complete_json}")
        https_url = download_url.replace(CDN_REPLACE_FROM, CDN_REPLACE_TO)
        if https_url.startswith("http://"):
            https_url = "https://" + https_url[len("http://") :]
        return {
            "url": https_url,
            "fileSize": file_size,
            "taskId": task_id,
            "md5sum": task_id,  # 前端 media.md5sum 使用上传任务 uuid
            "raw": complete_json,
        }

    def clip_video(
        self,
        video_url: str,
        width: int,
        height: int,
        duration: float,
        file_size: int,
        trace_key: str,
        upload_start: int,
        upload_end: int,
    ) -> Dict[str, Any]:
        target_w, target_h = calc_target_size(width, height)
        body = self._common_body(
            {
                "url": video_url,
                "timeStart": 0,
                "cropDuration": 0,
                "height": height,
                "width": width,
                "x": 0,
                "y": 0,
                "clipOriginVideoInfo": {
                    "width": width,
                    "height": height,
                    "duration": duration,
                    "fileSize": file_size,
                },
                "traceInfo": {
                    "traceKey": trace_key,
                    "uploadCdnStart": upload_start,
                    "uploadCdnEnd": upload_end,
                },
                "targetWidth": target_w,
                "targetHeight": target_h,
                "type": 4,
                "useAstraThumbCover": 1,
            }
        )
        return self._post_json(
            "/post/post_clip_video",
            body,
            referer=f"{BASE}/platform/post/create",
            use_micro=True,
        )["data"]

    def clip_video_result(self, clip_ticket: Dict[str, Any]) -> Dict[str, Any]:
        body = self._common_body(dict(clip_ticket))
        return self._post_json(
            "/post/post_clip_video_result",
            body,
            referer=f"{BASE}/platform/post/create",
            use_micro=True,
        )["data"]

    def wait_clip_result(self, clip_ticket: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
        """轮询裁剪结果。前端 flag==success 时 data 含 url；异步裁剪早期可能只有 flag。"""
        start = time.time()
        last: Dict[str, Any] = {}
        while time.time() - start < timeout:
            last = self.clip_video_result(clip_ticket)
            flag = last.get("flag")
            has_url = bool(last.get("url"))
            logger.info("clip flag=%s keys=%s", flag, list(last.keys()))
            if flag in (-1, 4, 5, "fail", "timeout", "Fail", "Timeout"):
                raise RuntimeError(f"裁剪失败: {last}")
            # 需要拿到转码后的 url（以及可选封面）后再继续
            if has_url and flag in (1, 2, 3, "success", "Success", None):
                return last
            time.sleep(5)
        if last.get("url"):
            return last
        raise TimeoutError(f"裁剪超时: {last}")

    def post_create(
        self,
        *,
        video_url: str,
        thumb_url: str,
        cover_url: str,
        width: int,
        height: int,
        duration: float,
        file_size: int,
        description: str,
        short_title: str,
        trace_key: str,
        upload_start: int,
        upload_end: int,
        clip_ticket: Dict[str, Any],
        topics: Optional[List[str]] = None,
        upload_cost_ms: int = 1000,
        md5sum: Optional[str] = None,
        location: Optional[Dict[str, Any]] = None,
        effective_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        topics = topics or []
        draft_id = clip_ticket.get("draftId") or clip_ticket.get("clipKey") or ""
        clip_key = clip_ticket.get("clipKey") or draft_id
        client_id = str(uuid.uuid4())
        md5sum = md5sum or str(uuid.uuid4())

        # 默认不显示位置：空对象；配置后才带坐标/城市
        loc_obj = location if location else {}

        desc = description
        for t in topics:
            tag = t if t.startswith("#") else f"#{t}"
            if tag not in desc:
                desc = f"{desc} {tag}".strip()

        # 与前端 postState.media 字段保持一致
        media = {
            "url": video_url,
            "fileSize": str(int(file_size)),
            "thumbUrl": thumb_url,
            "fullThumbUrl": thumb_url,
            "mediaType": 4,
            "videoPlayLen": int(round(duration)),
            "width": int(width),
            "height": int(height),
            "md5sum": md5sum,
            "coverUrl": cover_url or thumb_url,
            "fullCoverUrl": cover_url or thumb_url,
            "urlCdnTaskId": str(draft_id),
        }
        # 横版视频前端会补 cardShowStyle=Center(2)
        if int(width) >= int(height):
            media["cardShowStyle"] = 2
            media["shareCoverUrl"] = cover_url or thumb_url

        body = {
            "objectType": 0,
            "longitude": 0,
            "latitude": 0,
            "feedLongitude": 0,
            "feedLatitude": 0,
            "originalFlag": 0,
            "topics": topics,
            "isFullPost": 1,
            "handleFlag": 2,
            "videoClipTaskId": draft_id,
            "traceInfo": {
                "traceKey": trace_key,
                "uploadCdnStart": upload_start,
                "uploadCdnEnd": upload_end,
            },
            "objectDesc": {
                "mpTitle": "",
                "description": desc,
                "extReading": {"link": "", "title": ""},
                "mediaType": 4,
                "location": loc_obj,
                "topic": {"finderTopicInfo": ""},
                "event": {},
                "mentionedUser": [],
                "media": [media],
            },
            "report": {
                "clipKey": clip_key,
                "draftId": draft_id,
                "timestamp": ts_ms(),
                "_log_finder_uin": "",
                "_log_finder_id": self.finder_id,
                "rawKeyBuff": None,
                "pluginSessionId": None,
                "scene": 7,
                "reqScene": 7,
                "height": int(height),
                "width": int(width),
                "duration": float(duration),
                "fileSize": int(file_size),
                "uploadCost": upload_cost_ms,
            },
            "postFlag": 0,
            "mode": 1,
            "clientid": client_id,
            "timestamp": ts_ms(),
            "_log_finder_uin": "",
            "_log_finder_id": self.finder_id,
            "rawKeyBuff": None,
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
        }

        if short_title:
            body["objectDesc"]["shortTitle"] = [{"shortTitle": short_title}]

        # 定时发表：effectiveTime 为 Unix 秒；不传则立即发表
        if effective_time:
            body["effectiveTime"] = int(effective_time)

        return self._post_json(
            "/post/post_create",
            body,
            referer=f"{BASE}/platform/post/create",
            use_micro=True,
        )

    def post_create_image(
        self,
        *,
        media_list: List[Dict[str, Any]],
        title: str,
        description: str,
        trace_key: str,
        upload_start: int,
        upload_end: int,
        topics: Optional[List[str]] = None,
        location: Optional[Dict[str, Any]] = None,
        effective_time: Optional[int] = None,
        music_id: Optional[Union[str, int]] = None,
    ) -> Dict[str, Any]:
        """图文发布：mediaType=2 + finderNewlifeDesc。"""
        topics = topics or []
        if not media_list:
            raise ValueError("图文至少需要一张图片")
        title = (title or "").strip()
        if not title:
            raise ValueError("图文标题不能为空（richTextTitle）")

        desc = description or ""
        for t in topics:
            tag = t if t.startswith("#") else f"#{t}"
            if tag not in desc:
                desc = f"{desc} {tag}".strip()

        rich_json = build_newlife_rich_text_json(title, desc)
        client_id = str(uuid.uuid4())
        loc_obj = location if location else {}

        object_desc: Dict[str, Any] = {
            "mpTitle": "",
            "description": desc,
            "extReading": {},
            "mediaType": MEDIA_TYPE_IMAGE,
            "location": loc_obj,
            "topic": {"finderTopicInfo": ""},
            "event": {},
            "mentionedUser": [],
            "media": media_list,
            "finderNewlifeDesc": {
                "richTextTitle": title,
                "richTextJson": rich_json,
                "fromRichPublisher": 1,
            },
            "member": {},
        }
        if music_id is not None and str(music_id).strip():
            object_desc["followPostInfo"] = build_follow_post_info(music_id)

        body = {
            "objectType": 0,
            "longitude": 0,
            "latitude": 0,
            "feedLongitude": 0,
            "feedLatitude": 0,
            "originalFlag": 0,
            "topics": topics,
            "isFullPost": 1,
            "handleFlag": 2,
            "videoClipTaskId": "",
            "traceInfo": {
                "traceKey": trace_key,
                "uploadCdnStart": upload_start,
                "uploadCdnEnd": upload_end,
            },
            "objectDesc": object_desc,
            "postFlag": 0,
            "mode": 1,
            "clientid": client_id,
            "timestamp": ts_ms(),
            "_log_finder_uin": "",
            "_log_finder_id": self.finder_id,
            "rawKeyBuff": None,
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
        }
        if effective_time:
            body["effectiveTime"] = int(effective_time)

        return self._post_json(
            "/post/post_create",
            body,
            referer=NEWLIFE_PAGE,
            use_micro=True,
            page_url=NEWLIFE_MICRO_PAGE,
        )

    def publish_images(
        self,
        image_paths: Union[str, List[str]],
        title: str,
        description: str = "",
        topics: Optional[List[str]] = None,
        location: LocationConfig = None,
        schedule: ScheduleConfig = None,
        music_id: Optional[Union[str, int]] = None,
    ) -> Dict[str, Any]:
        """
        发布图文（finderNewLifeCreate）。

        Args:
            image_paths: 一张或多张本地路径 / http(s) URL。
            title: 图文标题（必填，对应 richTextTitle）。
            description: 正文描述，可含话题。
            location / schedule: 同 publish()。
            music_id: 配乐 ID（listenId 优先，或 musicSid）。仅传 ID 即可。
        """
        if isinstance(image_paths, (str, Path)):
            sources = [str(image_paths)]
        else:
            sources = [str(p) for p in image_paths]
        if not sources:
            raise ValueError("请至少传入一张图片")

        temp_files: List[str] = []
        task_t0 = time.time()
        step_t0 = task_t0

        def _step_done() -> None:
            nonlocal step_t0
            now = time.time()
            logger.info(
                "耗时 %s | 累计 %s",
                fmt_elapsed(now - step_t0),
                fmt_elapsed(now - task_t0),
            )
            step_t0 = now

        try:
            logger.info("[1/5] auth_data ...")
            auth = self.auth_data()
            self.load_env_from_auth(auth)
            if not self.finder_id:
                raise RuntimeError("未获取到 finderUsername，请检查 cookie 是否有效")
            logger.info("finder_id=%s", self.finder_id)
            _step_done()

            logger.info("[2/5] helper_upload_params ...")
            self.helper_upload_params()
            logger.info("uin(X-WECHAT-UIN)=%s", self.uin)
            _step_done()

            logger.info("[3/5] get_trace_key + 位置/定时 ...")
            loc_obj = self.resolve_location(location)
            if loc_obj:
                logger.info(
                    "location=%s / %s",
                    loc_obj.get("city") or "",
                    loc_obj.get("poiName") or "",
                )
            else:
                logger.info("location=不显示位置")
            effective_time = parse_schedule_time(schedule)
            if effective_time:
                logger.info(
                    "schedule=%s (%s)",
                    datetime.fromtimestamp(effective_time),
                    effective_time,
                )
            else:
                logger.info("schedule=立即发表")
            trace_key = self.get_trace_key(
                referer=NEWLIFE_PAGE,
                page_url=NEWLIFE_MICRO_PAGE,
            )
            logger.info("traceKey=%s", trace_key)
            _step_done()

            logger.info("[4/5] 上传图片 x%s ...", len(sources))
            upload_start = int(time.time())
            media_list: List[Dict[str, Any]] = []
            first_md5: Optional[str] = None
            for idx, src in enumerate(sources, 1):
                local_path, tmp_path, file_name = resolve_local_media(
                    src,
                    default_name=f"image_{idx}.jpg",
                    session=self.session,
                )
                if tmp_path:
                    temp_files.append(tmp_path)
                w, h = parse_image_size(local_path)
                uploaded = self.upload_file(
                    local_path,
                    "pictureFileType",
                    file_name=file_name or f"image_{idx}.jpg",
                )
                url = uploaded["url"]
                md5sum = uploaded["md5sum"]
                if first_md5 is None:
                    first_md5 = md5sum
                media_list.append(
                    {
                        "url": url,
                        "fileSize": int(uploaded["fileSize"]),
                        "thumbUrl": url,
                        "fullThumbUrl": url,
                        "mediaType": MEDIA_TYPE_IMAGE,
                        "videoPlayLen": 0,
                        "width": int(w),
                        "height": int(h),
                        "md5sum": md5sum,
                    }
                )
                logger.info("[%s/%s] %sx%s %s", idx, len(sources), w, h, url[:80])
            upload_end = int(time.time())
            _step_done()

            logger.info("[5/5] post_create (图文) ...")
            if music_id is not None and str(music_id).strip():
                logger.info("music_id=%s", music_id)
            create_ts = int(time.time())
            resp = self.post_create_image(
                media_list=media_list,
                title=title,
                description=description,
                trace_key=trace_key,
                upload_start=upload_start,
                upload_end=upload_end,
                topics=topics,
                location=loc_obj,
                effective_time=effective_time,
                music_id=music_id,
            )
            _step_done()
            logger.info("发布结果: %s", json.dumps(resp, ensure_ascii=False)[:1000])

            desc_for_match = description or ""
            for t in topics or []:
                tag = t if t.startswith("#") else f"#{t}"
                if tag not in desc_for_match:
                    desc_for_match = f"{desc_for_match} {tag}".strip()

            logger.info("获取发布链接 ...")
            try:
                link_info = self.resolve_publish_link(
                    description=desc_for_match,
                    title=title,
                    media_md5=first_md5,
                    since_ts=create_ts,
                    userpage_type=USERPAGE_PHOTO,
                )
                resp = dict(resp)
                resp["exportId"] = link_info["exportId"]
                resp["objectNonce"] = link_info["objectNonce"]
                resp["shortUrl"] = link_info["shortUrl"]
                resp["feedUrl"] = link_info["feedUrl"]
                resp["visibleType"] = link_info.get("visibleType")
                resp["visibleName"] = link_info.get("visibleName")
                logger.info("exportId=%s", link_info["exportId"])
                logger.info("可见性=%s", link_info.get("visibleName"))
                logger.info("发布链接=%s", link_info["shortUrl"])
            except Exception as e:
                logger.warning("获取发布链接失败: %s", e)
            _step_done()

            logger.info("任务总耗时: %s", fmt_elapsed(time.time() - task_t0))
            return resp
        finally:
            for tmp in temp_files:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def publish(
        self,
        video_path: str,
        description: str,
        short_title: str = "",
        topics: Optional[List[str]] = None,
        wait_clip: bool = True,
        location: LocationConfig = None,
        schedule: ScheduleConfig = None,
        cover_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Args:
            video_path: 本地视频路径或 http(s) URL。
            location: 位置配置，默认 None=不显示位置。
                True=自动附近第一条；"关键词"=搜索；dict=手动坐标/城市。
            schedule: 定时发表，默认 None=立即发表。
                支持 Unix 秒 / "YYYY-MM-DD HH:MM" / datetime。
            cover_path: 可选封面（本地路径或 http(s) URL）。
                传入则用该图；不传则从视频中取一帧上传。
        """
        temp_files: List[str] = []
        task_t0 = time.time()
        step_t0 = task_t0

        def _step_done() -> None:
            nonlocal step_t0
            now = time.time()
            logger.info(
                "耗时 %s | 累计 %s",
                fmt_elapsed(now - step_t0),
                fmt_elapsed(now - task_t0),
            )
            step_t0 = now

        try:
            local_video, tmp_video, video_name = resolve_local_media(
                video_path,
                default_name="video.mp4",
                session=self.session,
            )
            if tmp_video:
                temp_files.append(tmp_video)

            logger.info("[1/8] auth_data ...")
            auth = self.auth_data()
            self.load_env_from_auth(auth)
            if not self.finder_id:
                logger.error("auth keys: %s", list(auth.keys())[:30])
                raise RuntimeError("未获取到 finderUsername，请检查 cookie 是否有效")

            logger.info("finder_id=%s", self.finder_id)
            logger.info("cdnHost=%s", self.cdn_host)
            _step_done()

            logger.info("[2/8] helper_upload_params ...")
            self.helper_upload_params()
            logger.info("uin(X-WECHAT-UIN)=%s", self.uin)
            logger.info("authKey=%s...", self.auth_key[:32])
            _step_done()

            logger.info("[3/8] get_trace_key + 位置/定时 ...")
            loc_obj = self.resolve_location(location)
            if loc_obj:
                loc_query = location if isinstance(location, str) else (
                    (location or {}).get("query") if isinstance(location, dict) else location
                )
                logger.info(
                    "location=查询[%s] -> %s / %s",
                    loc_query,
                    loc_obj.get("city") or "",
                    loc_obj.get("poiName") or "",
                )
            else:
                logger.info("location=不显示位置")

            effective_time = parse_schedule_time(schedule)
            if effective_time:
                logger.info(
                    "schedule=%s (%s)",
                    datetime.fromtimestamp(effective_time),
                    effective_time,
                )
            else:
                logger.info("schedule=立即发表")

            trace_key = self.get_trace_key()
            logger.info("traceKey=%s", trace_key)
            _step_done()

            width, height, duration = parse_mp4_meta(local_video)
            logger.info("[4/8] 上传视频 %sx%s %.1fs ...", width, height, duration)
            upload_start = int(time.time())
            t0 = time.time()
            uploaded = self.upload_file(
                local_video, "videoFileType", file_name=video_name
            )
            upload_end = int(time.time())
            upload_cost = int((time.time() - t0) * 1000)
            video_url = uploaded["url"]
            file_size = uploaded["fileSize"]
            logger.info("video_url=%s", video_url)
            _step_done()

            logger.info("[5/8] 处理封面 ...")
            if cover_path:
                upload_cover, tmp_cover, cover_name = resolve_local_media(
                    cover_path,
                    default_name="cover.jpg",
                    session=self.session,
                )
                if tmp_cover:
                    temp_files.append(tmp_cover)
                logger.info("使用传入封面: %s", cover_path)
            else:
                cover_file = Path(__file__).resolve().parent / "finder_video_img.jpeg"
                extract_cover_jpeg(local_video, str(cover_file), at_sec=0.0)
                logger.info(
                    "未传 cover_path，已从视频取一帧: %s size=%s",
                    cover_file,
                    cover_file.stat().st_size,
                )
                upload_cover = str(cover_file)
                cover_name = "finder_video_img.jpeg"

            cover_uploaded = self.upload_file(
                upload_cover,
                "pictureFileType",
                file_name=cover_name or "finder_video_img.jpeg",
            )
            thumb_url = cover_uploaded["url"]
            cover_url = cover_uploaded["url"]
            logger.info("cover_url=%s", cover_url)
            _step_done()

            logger.info("[6/8] post_clip_video ...")
            clip_ticket = self.clip_video(
                video_url=video_url,
                width=width,
                height=height,
                duration=duration,
                file_size=file_size,
                trace_key=trace_key,
                upload_start=upload_start,
                upload_end=upload_end,
            )
            logger.info("clipTicket=%s", clip_ticket)
            _step_done()

            final_url = video_url
            final_w, final_h, final_dur, final_size = width, height, duration, file_size
            # 前端 md5sum = 视频上传任务 uuid，不是文件内容 md5
            md5sum = uploaded["md5sum"]
            clip_media_md5: Optional[str] = None

            if wait_clip:
                logger.info("[7/8] 等待裁剪结果 ...")
                try:
                    result = self.wait_clip_result(clip_ticket)
                    logger.info("clip_result keys=%s", list(result.keys()))
                    final_url = result.get("url") or final_url
                    clip_media_md5 = result.get("md5") or None
                    # 已有本地/指定封面时，不被 clip 空字段覆盖
                    clip_thumb = (
                        result.get("thumbUrl")
                        or result.get("coverUrl")
                        or result.get("fullThumbUrl")
                    )
                    clip_cover = (
                        result.get("coverUrl")
                        or result.get("fullCoverUrl")
                        or result.get("thumbUrl")
                    )
                    if clip_thumb:
                        thumb_url = clip_thumb
                    if clip_cover:
                        cover_url = clip_cover
                    final_w = int(result.get("width") or final_w)
                    final_h = int(result.get("height") or final_h)
                    if result.get("duration"):
                        final_dur = float(result["duration"])
                    if result.get("fileSize"):
                        final_size = int(result["fileSize"])
                except Exception as e:
                    logger.warning("等待裁剪结果失败，尝试异步发布: %s", e)
                _step_done()
            else:
                logger.info("[7/8] 跳过等待裁剪（asyncClip）")

            logger.info("[8/8] post_create ...")
            if final_w >= final_h:
                logger.info(
                    "横版 %sx%s -> cardShowStyle=2 + shareCoverUrl", final_w, final_h
                )
            create_ts = int(time.time())
            resp = self.post_create(
                video_url=final_url,
                thumb_url=thumb_url,
                cover_url=cover_url,
                width=final_w,
                height=final_h,
                duration=final_dur,
                file_size=final_size,
                description=description,
                short_title=short_title,
                trace_key=trace_key,
                upload_start=upload_start,
                upload_end=upload_end,
                clip_ticket=clip_ticket,
                topics=topics,
                upload_cost_ms=upload_cost,
                md5sum=md5sum,
                location=loc_obj,
                effective_time=effective_time,
            )
            _step_done()
            logger.info("发布结果: %s", json.dumps(resp, ensure_ascii=False)[:1000])

            # post_create 不直接返回链接；从内容列表取 exportId 再换短链
            desc_for_match = description
            for t in topics or []:
                tag = t if t.startswith("#") else f"#{t}"
                if tag not in desc_for_match:
                    desc_for_match = f"{desc_for_match} {tag}".strip()

            logger.info("获取发布链接 ...")
            try:
                link_info = self.resolve_publish_link(
                    description=desc_for_match,
                    media_md5=clip_media_md5,
                    since_ts=create_ts,
                )
                resp = dict(resp)
                resp["exportId"] = link_info["exportId"]
                resp["objectNonce"] = link_info["objectNonce"]
                resp["shortUrl"] = link_info["shortUrl"]
                resp["feedUrl"] = link_info["feedUrl"]
                resp["visibleType"] = link_info.get("visibleType")
                resp["visibleName"] = link_info.get("visibleName")
                logger.info("exportId=%s", link_info["exportId"])
                logger.info("可见性=%s", link_info.get("visibleName"))
                logger.info("发布链接=%s", link_info["shortUrl"])
            except Exception as e:
                logger.warning("获取发布链接失败: %s", e)
            _step_done()

            logger.info("任务总耗时: %s", fmt_elapsed(time.time() - task_t0))
            return resp
        finally:
            for tmp in temp_files:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
