"""爆款视频搜索词配置测试."""

from app.viral_keywords import viral_keyword
from app.viral_tikhub import PLATFORM_DOUYIN, PLATFORM_WECHAT


def test_garden_category_uses_plain_rural_garden_keyword() -> None:
    assert viral_keyword("庭院案例", PLATFORM_DOUYIN) == "农村庭院设计"
    assert viral_keyword("庭院案例", PLATFORM_WECHAT) == "农村庭院设计"
