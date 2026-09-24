"""发布作品可配置参数（对应 create_v2 请求体 + 创作者中心发布页）。"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 创作者中心「作品标题」上限（字符数，按 Unicode 码点计）
VIDEO_TITLE_MAX_LEN = 30
IMAGE_TITLE_MAX_LEN = 20
# 兼容旧名
TITLE_MAX_LEN = VIDEO_TITLE_MAX_LEN

# 简介/参数里常见的话题写法：#AI推广 / # AI推广 / a,b / #a #b
_HASHTAG_TOKEN_RE = re.compile(r"#\s*([^\s#]+)")
_TRAILING_HASHTAG_CLUSTER_RE = re.compile(
    r"(?:[\s,，、]*)(?:#\s*[^\s#]+(?:[\s,，、]*#\s*[^\s#]+)*)[\s,，、.。]*$"
)


def parse_hashtag_names(*parts: Any) -> list[str]:
    """把各种脏话题写法拆成干净话题名（不含 #，去重保序）。

    支持:
      - ``旅行,美食``
      - ``#旅行 #美食`` / ``# 旅行 # 美食``
      - 单个脏串 ``\"# ai行业 # geo优化\"``（会被继续拆开）
      - dict: ``{hashtag_name|name|cid|hashtag_id}``（仅取名字；id 在上层处理）
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = (name or "").lstrip("#").strip()
        if not n or n in seen:
            return
        # 仍含 # 说明是未拆开的脏串
        if "#" in n:
            for m in _HASHTAG_TOKEN_RE.findall("#" + n if not n.startswith("#") else n):
                _add(m)
            # 也尝试按空白/逗号再拆一次无 # 的片段
            for frag in re.split(r"[\s,，、]+", n):
                frag = frag.lstrip("#").strip()
                if frag and "#" not in frag:
                    _add(frag)
            return
        seen.add(n)
        names.append(n)

    for part in parts:
        if part is None:
            continue
        if isinstance(part, dict):
            _add(str(part.get("hashtag_name") or part.get("name") or ""))
            continue
        if isinstance(part, (list, tuple)):
            for x in part:
                for n in parse_hashtag_names(x):
                    _add(n)
            continue
        s = str(part).strip()
        if not s:
            continue
        if "#" in s:
            for m in _HASHTAG_TOKEN_RE.findall(s):
                _add(m)
            continue
        if "," in s or "，" in s or "、" in s:
            for frag in re.split(r"[,，、]+", s):
                _add(frag)
            continue
        _add(s)
    return names


def extract_hashtags_from_caption(caption: str) -> tuple[str, list[str]]:
    """从简介中抽出话题，返回 (干净简介, 话题名列表)。

    手机端对 ``# 话题``（# 后有空格）兼容很差，常导致简介被截断/话题渲染异常；
    统一抽到 challenges，正文只保留纯文案。
    """
    text = (caption or "").strip()
    if not text or "#" not in text:
        return text, []

    found = parse_hashtag_names(text)
    cleaned = _TRAILING_HASHTAG_CLUSTER_RE.sub("", text).strip()
    # 若话题嵌在句中而非末尾，再清一遍残留的 #xxx
    if "#" in cleaned:
        cleaned2 = _HASHTAG_TOKEN_RE.sub(" ", cleaned)
        cleaned2 = re.sub(r"\s{2,}", " ", cleaned2).strip(" ,，、")
        cleaned = cleaned2
    return cleaned.strip(), found


@dataclass
class PublishOptions:
    """
    与网页「发布视频」页面对齐的可配置项。

    可见性 visibility_type:
      0 = 公开
      1 = 好友可见
      2 = 仅自己可见

    download:
      1 = 允许他人保存
      0 = 不允许

    timing:
      0 = 立即发布
      >0 = 定时发布时间戳（秒）
    """

    # ----- 基础信息 -----
    title: str = ""  # 可为空；超长会截断，超出部分拼到 caption 前
    caption: str = ""  # 作品简介
    hashtags: list[str] = field(default_factory=list)  # 话题，不含 #
    mentions: list[str] = field(default_factory=list)  # @好友 sec_uid / 昵称占位
    activity: list[Any] = field(default_factory=list)  # 官方活动
    hot_sentence: str = ""  # 关联热点词

    # ----- 发布设置 -----
    visibility_type: int = 0  # 谁可以看
    download: int = 1  # 保存权限
    timing: int = 0  # 发布时间

    # ----- 音乐 -----
    music_id: Optional[str] = None
    music_source: int = 0
    music_end_time: Optional[int] = None  # 图文：截取结束时间（毫秒，通常=duration*1000）

    # ----- 封面 -----
    poster_delay: int = 0  # 封面取帧时间（秒，网页侧语义）

    # ----- 合集 -----
    mix_id: str = ""

    # ----- 同步 -----
    should_sync: bool = False
    sync_to_toutiao: int = 0

    # ----- 章节（可选 JSON 结构）-----
    chapter_abstract: str = ""
    chapter_details: list[dict[str, Any]] = field(default_factory=list)
    chapter_type: int = 0

    # ----- 地理位置 / 锚点 -----
    # 创作者中心 beforePost：anchorType=-11 时写入 item.anchor，而非 common
    poi_id: str = ""  # 发布地点 POI ID（有值时写入 item.anchor）
    poi_name: str = ""  # 发布地点名称
    # 对应网页选点后的 anchorExtra；缺省用 {"loc_from":"without"}
    poi_anchor_content: str = ""
    anchor: dict[str, Any] = field(default_factory=dict)  # 其它锚点透传（可与 poi 合并）

    # ----- 助手 -----
    is_preview: int = 0
    is_post_assistant: int = 1  # 与网页抓包一致

    # ----- 扩展透传（高级，直接合并进 common）-----
    extra_common: dict[str, Any] = field(default_factory=dict)

    def _build_anchor(self) -> dict[str, Any]:
        """组装 item.anchor。

        网页逻辑（async chunk）：选地点时 anchorType=-11，beforePost 产出
        ``{poi_id, poi_name, anchor_content?}``，挂在 item.anchor 上。
        写到 common 会被服务端忽略，作品不会显示位置。
        """
        out: dict[str, Any] = dict(self.anchor or {})
        pid = str(self.poi_id or "").strip()
        if not pid:
            return out
        out["poi_id"] = pid
        out["poi_name"] = str(self.poi_name or "").strip()
        content = str(self.poi_anchor_content or "").strip()
        if not content:
            content = str(out.get("anchor_content") or "").strip()
        if not content:
            content = json.dumps({"loc_from": "without"}, ensure_ascii=False, separators=(",", ":"))
        out["anchor_content"] = content
        return out

    def normalize_title_caption(self, max_len: int = VIDEO_TITLE_MAX_LEN) -> None:
        """标题可为空；超过 max_len 时截断，超出部分拼到简介前。

        同时把简介里的 ``#话题`` / ``# 话题`` 抽到 hashtags，避免手机端截断显示。
        max_len: 视频默认 30，图文传 IMAGE_TITLE_MAX_LEN(=20)。
        """
        limit = max(0, int(max_len))
        title = (self.title or "").strip()
        caption = (self.caption or "").strip()
        if len(title) > limit:
            overflow = title[limit:].strip()
            kept = title[:limit]
            if overflow:
                caption = f"{overflow} {caption}".strip() if caption else overflow
            logger.info(
                "标题超长（%d>%d），截断为 %r，超出部分已拼到简介前",
                len(title),
                limit,
                kept,
            )
            title = kept

        caption, from_caption = extract_hashtags_from_caption(caption)
        merged = parse_hashtag_names(self.hashtags, from_caption)
        if from_caption:
            logger.info(
                "简介中的话题已抽出并规范化: %s；干净简介字数=%d",
                from_caption,
                len(caption),
            )
        self.title = title
        self.caption = caption
        self.hashtags = merged

    def _build_text_and_extras(
        self, *, title_max_len: int = VIDEO_TITLE_MAX_LEN
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
        """返回 (text_body, challenges, text_extra_hashtags, mentions_payload)。

        text_extra 的 start/end 按「末尾追加的话题」计算，避免 title/caption
        里已有相同 ``#话题`` 时 find 误命中正文。
        未传 hashtag_id / cid 时默认 ``\"0\"``。
        """
        # 保留 dict 形式话题上的 hashtag_id / cid（normalize 后 hashtags 会变成纯名字）
        id_by_name: dict[str, str] = {}
        for tag in self.hashtags:
            if not isinstance(tag, dict):
                continue
            name = str(tag.get("hashtag_name") or tag.get("name") or "").lstrip("#").strip()
            if not name:
                continue
            hid = tag.get("hashtag_id")
            if hid is None or hid == "":
                hid = tag.get("cid")
            if hid is not None and hid != "":
                id_by_name[name] = str(hid)

        self.normalize_title_caption(title_max_len)
        title = self.title
        caption_text = self.caption
        names = parse_hashtag_names(self.hashtags)

        challenges: list[dict[str, Any]] = []
        tag_parts: list[str] = []
        for name in names:
            hid_s = id_by_name.get(name, "0")
            token = f"#{name}"
            tag_parts.append(token)
            challenges.append({"hashtag_name": name, "hashtag_id": hid_s})

        mentions_payload: list[Any] = []
        for m in self.mentions:
            if isinstance(m, dict):
                mentions_payload.append(m)
            elif m:
                mentions_payload.append({"nickname": str(m)})

        if caption_text and caption_text != title:
            base = f"{title} {caption_text}".strip()
        else:
            base = title

        text_extra: list[dict[str, Any]] = []
        if not tag_parts:
            return base, challenges, text_extra, mentions_payload

        # 话题固定拼在正文末尾；偏移按追加位置写，不靠 find
        if base:
            text_body = f"{base} {' '.join(tag_parts)}"
            cursor = len(base) + 1  # base 后的空格
        else:
            text_body = " ".join(tag_parts)
            cursor = 0
        for i, (ch, token) in enumerate(zip(challenges, tag_parts)):
            text_extra.append(
                {
                    "start": cursor,
                    "end": cursor + len(token),
                    "type": 1,
                    "hashtag_name": ch["hashtag_name"],
                    "hashtag_id": ch["hashtag_id"],
                    "user_id": "",
                }
            )
            cursor += len(token)
            if i + 1 < len(tag_parts):
                cursor += 1  # 话题之间的空格

        return text_body, challenges, text_extra, mentions_payload

    def to_create_item(
        self,
        video_id: str,
        poster: str,
        creation_id: str,
        cover_width: int = 0,
        cover_height: int = 0,
    ) -> dict[str, Any]:
        """视频 create_v2（2026-07-23 创作者中心浏览器实发抓包对齐）。

        要点:
          - text = 标题 + 空格 + 简介（无强制末尾空格）
          - caption = 简介原文（不是空字符串）
          - 未选配乐时 music_id 必须为 null（勿传无关推荐歌 id）
          - cover 传 poster；自定义封面时带 width/height
          - item_title 可为空；超 VIDEO_TITLE_MAX_LEN(30) 截断，超出拼进 caption
        """
        # 话题拼进 text + challenges/text_extra；无 id 时默认 hashtag_id=0
        text_body, challenges, text_extra, mentions_payload = self._build_text_and_extras(
            title_max_len=VIDEO_TITLE_MAX_LEN
        )
        title = self.title
        caption_text = self.caption

        common: dict[str, Any] = {
            "text": text_body,
            "caption": caption_text,
            "item_title": title,
            "activity": json.dumps(self.activity, ensure_ascii=False, separators=(",", ":")),
            "text_extra": json.dumps(text_extra, ensure_ascii=False, separators=(",", ":")),
            "challenges": json.dumps(challenges, ensure_ascii=False, separators=(",", ":")),
            "mentions": json.dumps(mentions_payload, ensure_ascii=False, separators=(",", ":")),
            "hashtag_source": "",
            "hot_sentence": self.hot_sentence or "",
            "interaction_stickers": "[]",
            "visibility_type": int(self.visibility_type),
            "download": int(self.download),
            "timing": int(self.timing),
            "creation_id": creation_id,
            "media_type": 4,
            "video_id": video_id,
            "music_source": int(self.music_source),
            # 网页未选配乐时为 null；硬编码无关 music_id 易触发风控
            "music_id": str(self.music_id) if self.music_id else None,
        }
        if self.music_end_time is not None:
            common["music_end_time"] = int(self.music_end_time)
        if self.extra_common:
            common.update(self.extra_common)

        chapter_obj = {
            "chapter_abstract": self.chapter_abstract or "",
            "chapter_details": self.chapter_details or [],
            "chapter_type": int(self.chapter_type),
            "chapter_tools_info": {
                "chapter_recommend_detail": [],
                "chapter_recommend_abstract": "",
                "chapter_source": 2,
                "chapter_recommend_type": -2,
                "create_date": int(time.time()),
                "is_pc": "1",
                "is_pre_generated": "0",
                "is_syn": "1",
            },
        }

        mix: dict[str, Any] = {}
        if self.mix_id:
            mix = {"mix_id": self.mix_id}

        cover: dict[str, Any] = {
            "poster": poster,
            "poster_delay": int(self.poster_delay),
            "cover_tools_extend_info": "{}",
            "cover_tools_info": "{}",
        }
        if cover_width > 0 and cover_height > 0:
            cover["custom_cover_image_width"] = int(cover_width)
            cover["custom_cover_image_height"] = int(cover_height)

        return {
            "item": {
                "common": common,
                "cover": cover,
                "mix": mix,
                "selected_member": {"is_selected_member_video": False},
                "chapter": {
                    "chapter": json.dumps(chapter_obj, ensure_ascii=False, separators=(",", ":"))
                },
                "anchor": self._build_anchor(),
                "sync": {
                    "should_sync": bool(self.should_sync),
                    "sync_to_toutiao": int(self.sync_to_toutiao),
                },
                "open_platform": {},
                "assistant": {
                    "is_preview": int(self.is_preview),
                    "is_post_assistant": int(self.is_post_assistant),
                },
            }
        }

    def to_create_image_item(
        self,
        images: list[dict[str, Any]],
        poster: str,
        creation_id: str,
    ) -> dict[str, Any]:
        """图文 create_v2（2026-08-12 创作者中心浏览器实发抓包对齐）。

        关键点:
          - media_type = 2
          - common.images = [{uri, width, height}, ...]
          - 立即发布 timing = -1
          - text = ``标题。简介 #话题``（标题后必须紧跟「。」，不是空格）
          - text_extra:
              type7 = 标题区间 [0, len(title))
              type8 = 标题后句号 [len(title), len(title)+1)
              type1 = 末尾话题
            手机端把 type8 之后当作可展开正文；句号放文末会导致简介不可见。
          - 浏览器图文请求通常不带 caption / item_title
        """
        # 先 normalize（截断标题、抽出简介里的话题），但不要用视频那套「空格拼接」
        id_by_name: dict[str, str] = {}
        for tag in self.hashtags:
            if not isinstance(tag, dict):
                continue
            name = str(tag.get("hashtag_name") or tag.get("name") or "").lstrip("#").strip()
            if not name:
                continue
            hid = tag.get("hashtag_id")
            if hid is None or hid == "":
                hid = tag.get("cid")
            if hid is not None and hid != "":
                id_by_name[name] = str(hid)

        self.normalize_title_caption(IMAGE_TITLE_MAX_LEN)
        title = self.title
        caption_text = self.caption
        names = parse_hashtag_names(self.hashtags)

        challenges: list[dict[str, Any]] = []
        tag_parts: list[str] = []
        for name in names:
            hid_s = id_by_name.get(name, "0")
            tag_parts.append(f"#{name}")
            challenges.append({"hashtag_name": name, "hashtag_id": hid_s})

        mentions_payload: list[Any] = []
        for m in self.mentions:
            if isinstance(m, dict):
                mentions_payload.append(m)
            elif m:
                mentions_payload.append({"nickname": str(m)})

        # 浏览器: text = 标题。 + 简介 + (可选空格+话题)
        # 仅有标题时: 标题。
        body_after_period = caption_text or ""
        if tag_parts:
            tags_str = " ".join(tag_parts)
            body_after_period = (
                f"{body_after_period} {tags_str}".strip()
                if body_after_period
                else tags_str
            )
        text_body = f"{title}。{body_after_period}" if title else f"。{body_after_period}"
        if not title and not body_after_period:
            text_body = "。"

        text_extra: list[dict[str, Any]] = [
            {
                "start": 0,
                "end": len(title),
                "hashtag_id": 0,
                "hashtag_name": "",
                "type": 7,
            },
            {
                "start": len(title),
                "end": len(title) + 1,
                "hashtag_id": 0,
                "hashtag_name": "",
                "type": 8,
            },
        ]
        if tag_parts:
            # 话题从正文末尾往前定位，避免 find 误命中简介里的同名片段
            cursor = len(text_body) - len(" ".join(tag_parts))
            for i, (ch, token) in enumerate(zip(challenges, tag_parts)):
                text_extra.append(
                    {
                        "start": cursor,
                        "end": cursor + len(token),
                        "type": 1,
                        "hashtag_name": ch["hashtag_name"],
                        "hashtag_id": ch["hashtag_id"],
                        "user_id": "",
                    }
                )
                cursor += len(token)
                if i + 1 < len(tag_parts):
                    cursor += 1

        timing = int(self.timing)
        if timing == 0:
            timing = -1

        common: dict[str, Any] = {
            "text": text_body,
            "text_extra": json.dumps(text_extra, ensure_ascii=False, separators=(",", ":")),
            "activity": json.dumps(self.activity, ensure_ascii=False, separators=(",", ":")),
            "challenges": json.dumps(challenges, ensure_ascii=False, separators=(",", ":")),
            "hashtag_source": "",
            "mentions": json.dumps(mentions_payload, ensure_ascii=False, separators=(",", ":")),
            "visibility_type": int(self.visibility_type),
            "download": int(self.download),
            "timing": timing,
            "media_type": 2,
            "images": images,
            "creation_id": creation_id,
        }
        if self.hot_sentence:
            common["hot_sentence"] = self.hot_sentence
        if self.music_id:
            common["music_id"] = str(self.music_id)
            if self.music_end_time is not None:
                common["music_end_time"] = int(self.music_end_time)
        if self.extra_common:
            common.update(self.extra_common)

        item: dict[str, Any] = {
            "common": common,
            "cover": {"poster": poster},
            "anchor": self._build_anchor(),
        }
        if self.mix_id:
            item["mix"] = {"mix_id": self.mix_id}
        return {"item": item}

    @classmethod
    def from_mapping(cls, data: Optional[dict[str, Any]]) -> "PublishOptions":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                kwargs[k] = v
        # 兼容别名
        if "desc" in data and "caption" not in kwargs:
            kwargs["caption"] = data["desc"]
        if "description" in data and "caption" not in kwargs:
            kwargs["caption"] = data["description"]
        if "tags" in data and "hashtags" not in kwargs:
            kwargs["hashtags"] = data["tags"]
        if "private" in data and data["private"] and "visibility_type" not in kwargs:
            kwargs["visibility_type"] = 2
        return cls(**kwargs)

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


# 文档用字段说明（原 Flask GET /api/options；接口层已废弃，可随 app.py 去掉）
OPTIONS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "title",
        "type": "string",
        "default": "",
        "desc": (
            f"作品标题（可空；视频超 {VIDEO_TITLE_MAX_LEN} 字 / "
            f"图文超 {IMAGE_TITLE_MAX_LEN} 字截断，超出拼到 caption 前）"
        ),
    },
    {"name": "caption", "type": "string", "default": "", "desc": "作品简介（拼进 text，同时写入 caption 字段）"},
    {"name": "hashtags", "type": "string[]", "default": [], "desc": "话题名列表，拼进 text；无 id 默认 0，也可传 {hashtag_name,hashtag_id}"},
    {"name": "mentions", "type": "string[]|object[]", "default": [], "desc": "@好友"},
    {"name": "activity", "type": "array", "default": [], "desc": "官方活动配置"},
    {"name": "hot_sentence", "type": "string", "default": "", "desc": "关联热点词"},
    {
        "name": "visibility_type",
        "type": "int",
        "default": 0,
        "desc": "谁可以看：0公开 / 1好友可见 / 2仅自己可见",
    },
    {"name": "download", "type": "int", "default": 1, "desc": "保存权限：1允许 / 0不允许"},
    {"name": "timing", "type": "int", "default": 0, "desc": "0立即发布，否则为定时 Unix 秒时间戳"},
    {"name": "music_id", "type": "string|null", "default": None, "desc": "音乐 ID（来自 music/list）"},
    {"name": "music_source", "type": "int", "default": 0, "desc": "音乐来源（视频用；图文抓包未传）"},
    {"name": "music_end_time", "type": "int|null", "default": None, "desc": "图文配乐结束毫秒，通常 duration*1000"},
    {"name": "poster_delay", "type": "int", "default": 0, "desc": "封面相关延时/取帧参数"},
    {"name": "mix_id", "type": "string", "default": "", "desc": "合集 ID"},
    {"name": "should_sync", "type": "bool", "default": False, "desc": "是否同步其他平台"},
    {"name": "sync_to_toutiao", "type": "int", "default": 0, "desc": "是否同步头条"},
    {"name": "chapter_abstract", "type": "string", "default": "", "desc": "章节摘要"},
    {"name": "chapter_details", "type": "array", "default": [], "desc": "章节明细"},
    {"name": "chapter_type", "type": "int", "default": 0, "desc": "章节类型"},
    {"name": "poi_id", "type": "string", "default": "", "desc": "发布地点 POI ID（有值时写入 item.anchor）"},
    {"name": "poi_name", "type": "string", "default": "", "desc": "发布地点名称（配合 poi_id）"},
    {"name": "poi_anchor_content", "type": "string", "default": "", "desc": "地点 anchor_content（空则默认 {\"loc_from\":\"without\"}）"},
    {"name": "anchor", "type": "object", "default": {}, "desc": "其它锚点透传（与 poi 合并进 item.anchor）"},
    {"name": "is_preview", "type": "int", "default": 0, "desc": "是否预览发布"},
    {"name": "is_post_assistant", "type": "int", "default": 0, "desc": "发文助手标记"},
    {"name": "extra_common", "type": "object", "default": {}, "desc": "合并进 common 的高级字段"},
]

# 图文模式补充说明（见 PublishOptions.to_create_image_item）
IMAGE_NOTES = (
    "图文: media_type=2; images=[{uri,width,height}]; "
    "立即发布 timing=-1; 封面默认第一张图 uri"
)
