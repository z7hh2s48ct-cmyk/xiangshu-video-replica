"""下发给客户端的媒体地址必须是绝对地址（静态合同，无 PG 依赖）。

桌面端页面 origin 是 ``tauri://``，客户云版前端可与 API 分域名部署。站内相对
地址会打到客户端自身而不是后端，``img``/``video`` 拿不到字节，只剩一个深色空
预览框——这个症状在素材库、参考素材、成片详情上反复出现过，根因每次都是某条
新加的签发通道忘了拼地址（2026-09-18 一次排查中批量授权、成片播放、自有封面
三条通道同时中招）。

防线放在服务端而不是前端：真正的风险是「新增一个签发端点忘了绝对化」，而端点
都在这里。``rbac_routes`` 模块的所有 ``/api/...`` 字符串都是要交给浏览器加载
的地址，因此该模块内不允许出现裸的相对地址字面量——一律经 ``api_base_url()``
（与 ``viral_routes`` 的自有封面路由共用同一处配置 ``PUBLIC_BASE_URL``）。

注意 ``viral_media.CoverEnricher.stable_url`` 故意产出相对路径：封面地址要入库
（``update_viral_cover``），写库时还不知道对外地址，由 ``viral_routes`` 在序列化
响应时绝对化。「存相对、发绝对」是有意为之，不在本合同约束范围内。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# 形如 "/api/..." 或 f"/api/..." 的字符串字面量开头（绝对地址会是 f"{api_base_url()}/api/...）。
BARE_RELATIVE_API_URL = re.compile(r'f?"/api/')


def test_rbac_routes_never_hands_out_relative_media_urls() -> None:
    """签发通道一律下发绝对地址，客户端零拼接。"""
    source = (APP_ROOT / "rbac_routes.py").read_text(encoding="utf-8")
    offenders = [line.strip() for line in source.splitlines() if BARE_RELATIVE_API_URL.search(line)]
    assert offenders == [], (
        "签发地址必须经 api_base_url() 拼成绝对地址，否则桌面端只会显示空预览框：\n"
        + "\n".join(offenders)
    )


def test_thumbnail_objects_are_cacheable_until_the_grant_expires() -> None:
    """代理下发的缩略图必须可缓存，其余对象保持 no-store。

    素材库一页 24 张瓦片，``no-store`` 意味着每次翻页/重渲染都全量重拉、逐张
    穿透应用服务器。缩略图键由内容确定性派生（``<object_key>.thumb.jpg``），
    重新生成必然换键，因此可以 ``immutable``；缓存窗口与签名有效期一致，不会
    让任何一条授权活得比签名更久。
    """
    from app.material_thumbs import THUMBNAIL_SUFFIX, THUMBNAIL_URL_EXPIRES_IN
    from app.media_routes import _signed_object_cache_control

    thumb = _signed_object_cache_control(f"projects/a/clip.mp4{THUMBNAIL_SUFFIX}")
    assert thumb is not None
    assert "immutable" in thumb
    assert f"max-age={int(THUMBNAIL_URL_EXPIRES_IN.total_seconds())}" in thumb
    # 原视频/人物图等仍然不进缓存（授权可被会话吊销，缓存会架空吊销）。
    assert _signed_object_cache_control("projects/a/clip.mp4") is None
    assert _signed_object_cache_control("users/u1/identities/face.png") is None
