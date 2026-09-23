"""派生缩略图键必须能通过签名授权校验，而其它派生键一律不行。

生产事故背景：2026-09-20 线上 candidate-api-1 日志里
``GET /api/assets/signed-objects/<key>.thumb.jpg`` 出现 50 次，**403 五十次、
200 零次**；同一会话同一 session_epoch 下，非缩略图的 signed-objects 请求
116 次成功。原因是签发侧（``rbac_routes`` 的素材缩略图授权）签的是
``thumbnail_key_for(<原键>)``，而读取侧 ``validate_signed_asset_grant`` 拿
请求键去和资产自己的 ``storage_uri`` 键逐字节比对——两者永远不相等。

连带后果：``get_signed_object`` 里「历史素材首次读取时现场补齐缩略图」那段
代码在它前面的授权关卡就被挡掉了，从未真正执行过。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app import media_routes
from app.auth import CurrentUser
from app.material_thumbs import thumbnail_key_for

STORAGE_URI = "cos://bucket/verified-uploads/asset-1/abc123/original.mp4"
OBJECT_KEY = "verified-uploads/asset-1/abc123/original.mp4"


@pytest.fixture
def stubbed_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """只保留待测的键比对：身份与资产授权都已在上游通过。"""
    actor = CurrentUser(id="u-1", username="ops", display_name="Ops", role="admin")
    monkeypatch.setattr(media_routes, "authenticate_user", lambda conn, user_id: actor)
    monkeypatch.setattr(
        media_routes,
        "require_asset_access",
        lambda conn, **kwargs: {"storage_uri": STORAGE_URI},
    )


def _grant(expected_object_key: str) -> Any:
    # 内部角色 + epoch "0" 让函数在键比对之后立刻返回，不触碰会话状态表。
    return media_routes.validate_signed_asset_grant(
        None,  # type: ignore[arg-type]
        user_id="u-1",
        asset_id="a-1",
        session_epoch="0",
        expected_object_key=expected_object_key,
    )


@pytest.mark.usefixtures("stubbed_grant")
def test_original_object_key_is_accepted() -> None:
    """回归保护：原对象键本来就能通过，修缩略图不能把它弄坏。"""
    assert _grant(OBJECT_KEY)["storage_uri"] == STORAGE_URI


@pytest.mark.usefixtures("stubbed_grant")
def test_derived_thumbnail_key_is_accepted() -> None:
    """签发侧签的就是这个键，读取侧必须认。"""
    assert _grant(thumbnail_key_for(OBJECT_KEY))["storage_uri"] == STORAGE_URI


@pytest.mark.usefixtures("stubbed_grant")
def test_unrelated_key_is_still_rejected() -> None:
    """放行缩略图不等于放松校验——别的资产的键必须继续 403。"""
    with pytest.raises(HTTPException) as caught:
        _grant("verified-uploads/other-asset/deadbeef/original.mp4")
    assert caught.value.status_code == 403


@pytest.mark.usefixtures("stubbed_grant")
def test_thumbnail_suffix_on_an_unrelated_key_is_still_rejected() -> None:
    """不能退化成「以 .thumb.jpg 结尾就放行」。"""
    with pytest.raises(HTTPException) as caught:
        _grant(thumbnail_key_for("verified-uploads/other-asset/deadbeef/original.mp4"))
    assert caught.value.status_code == 403


@pytest.mark.usefixtures("stubbed_grant")
def test_prefix_of_the_object_key_is_still_rejected() -> None:
    """也不能退化成前缀匹配。"""
    with pytest.raises(HTTPException) as caught:
        _grant(OBJECT_KEY[:-4])
    assert caught.value.status_code == 403


@pytest.mark.usefixtures("stubbed_grant")
def test_double_thumbnail_suffix_is_rejected() -> None:
    """只认一层确定性派生，不认套娃。"""
    with pytest.raises(HTTPException) as caught:
        _grant(thumbnail_key_for(thumbnail_key_for(OBJECT_KEY)))
    assert caught.value.status_code == 403
