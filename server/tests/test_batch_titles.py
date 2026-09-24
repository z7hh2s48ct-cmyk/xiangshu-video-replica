"""作品名称应独立于生成提示词，保留用户命名。"""

import pytest

from app.generation import batch_display_name


@pytest.mark.parametrize(
    ("name", "kind", "project", "expected"),
    [
        (None, "independent", None, "视频生成"),
        (None, "replica", "回乡建房", "回乡建房"),
        (None, "replica", "reference-video", "视频复刻"),
        (None, "replacement", None, "人物置换"),
        (None, "unknown", None, "视频作品"),
        ("我的英文标题 Home", "independent", None, "我的英文标题 Home"),
        ("  庭院讲解  ", "replica", "旧项目", "庭院讲解"),
    ],
)
def test_batch_title_uses_name_or_chinese_default(name, kind, project, expected):
    assert batch_display_name(name, kind, project) == expected
