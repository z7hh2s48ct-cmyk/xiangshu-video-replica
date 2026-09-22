"""五视角合同收窄后的存量兼容契约：旧七资产人物照常可用，RIGHT_* 不再派生。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES, encode_json
from app.character_reference_matching import (
    SourceFrameFeatures as _Features,
)
from app.character_reference_matching import (
    recommended_body_view,
    validated_publication,
)
from app.simple_character import SIMPLE_CONTACT_SHEET_PROMPT, scene_contact_sheet_prompt
from app.simple_character_routes import read_simple_library


class _FakeRow(dict):
    def __getitem__(self, key):  # noqa: D105 - dict access
        return dict.__getitem__(self, key)


def _publication_snapshot(views: list[str]) -> tuple[dict, str]:
    snapshot = {
        "schema_version": "character-publication.v1",
        "character_version_id": "cv-1",
        "required_view_types": views,
        "assets_by_view": {view: {"asset_id": f"asset-{view.lower()}"} for view in views},
    }
    return snapshot, hashlib.sha256(encode_json(snapshot).encode()).hexdigest()


def _version_row(views: list[str]) -> _FakeRow:
    snapshot, publication_hash = _publication_snapshot(views)
    return _FakeRow(
        id="cv-1",
        publication_snapshot_json=encode_json(snapshot),
        publication_hash=publication_hash,
    )


def test_required_view_types_are_exactly_the_five_real_views() -> None:
    assert REQUIRED_CHARACTER_VIEW_TYPES == (
        "FRONT_FACE",
        "FRONT_HALF",
        "FRONT_FULL",
        "LEFT_45",
        "LEFT_SIDE",
    )


def test_five_view_prompts_include_physical_photography_anchors() -> None:
    """Verify new prompts emphasize physical camera gear and positive texture descriptors."""
    scene_prompt = scene_contact_sheet_prompt(
        scene_description="乡村工地",
        costume_description="蓝色施工马甲",
    )

    for prompt in (SIMPLE_CONTACT_SHEET_PROMPT, scene_prompt):
        # 提示词按行宽折行，断言只关心措辞本身，不应被折行位置左右。
        flat = " ".join(prompt.split())
        # 材质要点在提示词里是列表项、句首大写，这一类措辞用大小写不敏感比对。
        lowered = flat.lower()

        # Physical camera anchors (both prompts must have these)
        assert "Canon EOS R5" in flat, "Must specify professional camera model"
        assert "85mm" in flat, "Must specify portrait lens focal length"

        # Positive texture descriptors (at least one variant)
        assert "visible pores" in flat or "fine pores" in flat, "Must describe skin texture"
        assert "vellus hairs" in flat or "peach fuzz" in flat or "individual strands" in flat, (
            "Must describe hair detail"
        )
        assert "fabric weave" in lowered or "fabric texture" in lowered, (
            "Must describe clothing texture"
        )

        # Anti-plastic terms (updated wording)
        assert "plastic skin" in flat or "smooth plastic skin" in flat, (
            "Must reject plastic appearance"
        )
        assert "beauty filter" in flat or "beauty-filter" in flat, (
            "Must reject beauty filter effects"
        )


def test_base_prompt_includes_studio_lighting_anchor() -> None:
    """Base prompt should reference specific studio lighting equipment."""
    flat = " ".join(SIMPLE_CONTACT_SHEET_PROMPT.split())
    assert "Elinchrom" in flat or "softbox" in flat.lower(), (
        "Base prompt must specify studio lighting"
    )


def test_scene_prompt_includes_aperture_and_editorial_positioning() -> None:
    """Scene prompt should have aperture spec and editorial/commercial positioning."""
    scene_prompt = scene_contact_sheet_prompt(
        scene_description="乡村工地",
        costume_description="蓝色施工马甲",
    )
    flat = " ".join(scene_prompt.split())

    assert "f/4" in flat, "Scene prompt must specify aperture for depth of field"
    assert "editorial" in flat.lower() or "commercial portrait" in flat.lower(), (
        "Scene prompt must position output as professional editorial/commercial work"
    )


def test_old_negative_word_wall_removed() -> None:
    """Confirm we removed the old wall-of-negatives approach."""
    flat_base = " ".join(SIMPLE_CONTACT_SHEET_PROMPT.split())

    # This exact string was in the old prompt as a continuous negative list
    old_nasty_string = (
        "waxy or plastic skin, porcelain-doll smoothness, rubbery facial features, "
        "CGI sheen, excessive denoising, beauty-filter skin, artificial HDR, and uniformly "
        "airbrushed texture"
    )
    assert old_nasty_string not in flat_base, (
        "Old negative word wall should be removed and distributed into specific constraints"
    )


@pytest.mark.parametrize("legacy_views", [[], ["RIGHT_45", "RIGHT_SIDE"], ["IMPORTED_REFERENCE"]])
def test_library_response_accepts_legacy_assets_without_breaking_all_people(legacy_views) -> None:
    """Exercise the library mapper AND response validation with historical rows."""
    rows = [
        {
            "identity_id": "identity-1",
            "display_name": "历史人物",
            "owner_user_id": "customer-1",
            "identity_status": "ACTIVE",
            "persona_id": "persona-1",
            "occupation": "讲解员",
            "appearance_constraints_json": "{}",
            "version_id": "version-1",
            "version_number": 1,
            "published_at": "2026-09-01T00:00:00Z",
            "snapshot_json": "{}",
            "view_type": view,
            "asset_id": f"asset-{view}",
        }
        for view in [*REQUIRED_CHARACTER_VIEW_TYPES, *legacy_views]
    ]
    conn = MagicMock()
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: {"total": 1}),
        MagicMock(fetchall=lambda: [{"id": "identity-1", "created_at": "2026-09-01"}]),
        MagicMock(fetchall=lambda: rows),
    ]
    page = read_simple_library(
        conn=conn,
        actor=SimpleNamespace(id="customer-1", role="customer"),
        limit=12,
        cursor=None,
        query="",
    )
    assert page.total == 1
    assert page.items[0].display_name == "历史人物"
    assert {view.view_type for view in page.items[0].views} == set(REQUIRED_CHARACTER_VIEW_TYPES)


def test_legacy_seven_view_publication_still_validates() -> None:
    row = _version_row(
        [
            "FRONT_FACE",
            "FRONT_HALF",
            "FRONT_FULL",
            "LEFT_45",
            "RIGHT_45",
            "LEFT_SIDE",
            "RIGHT_SIDE",
        ]
    )
    result, publication_hash = validated_publication(row)
    assert publication_hash == row["publication_hash"]
    assert set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(set(result["assets_by_view"]))


def test_publication_missing_a_required_view_is_rejected() -> None:
    row = _version_row(["FRONT_FACE", "FRONT_FULL"])
    with pytest.raises(HTTPException) as exc:
        validated_publication(row)
    assert exc.value.detail["code"] == "CHARACTER_PUBLICATION_INVALID"


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        ("LEFT_45", "LEFT_45"),
        ("RIGHT_45", "LEFT_45"),
        ("LEFT_SIDE", "LEFT_SIDE"),
        ("RIGHT_SIDE", "LEFT_SIDE"),
    ],
)
def test_right_facing_source_frames_map_to_the_real_left_views(
    orientation: str, expected: str
) -> None:
    features = _Features(
        orientation=orientation,  # type: ignore[arg-type]
        shot_size="FULL_BODY",
        face_visible=True,
        body_completeness="FULL_BODY",
    )
    assert recommended_body_view(features) == expected


def test_contact_sheet_size_is_wide_enough_for_the_full_body_panels() -> None:
    """整图必须足够宽，否则左侧全身格会被右侧近景列挤到验收区间以下。

    实测（2026-09-22，同源图同提示词）：模型把约 815px 固定留给近景列，剩余宽度才
    分给三个全身格。2048x1152 时最窄全身格只有 386px，比不声明尺寸时的 405px 还窄；
    2560x1440 时全身格 548~573px，才落进计划的 500-700px 验收区间。
    """
    from app.simple_character import CONTACT_SHEET_SIZE

    width_text, height_text = CONTACT_SHEET_SIZE.split("x")
    width, height = int(width_text), int(height_text)

    assert width % 16 == 0, f"{CONTACT_SHEET_SIZE} 宽度 {width} 不能被 16 整除"
    assert height % 16 == 0, f"{CONTACT_SHEET_SIZE} 高度 {height} 不能被 16 整除"
    # 近景列实测约占 815px，三个全身格各需 ≥500px。
    assert width >= 3 * 500 + 815, (
        f"{CONTACT_SHEET_SIZE} 太窄：全身格会被近景列挤到 500px 验收下限以下"
    )


def test_contact_sheet_below_the_resolution_floor_is_rejected() -> None:
    """低于清晰度下限的五视图必须报错，而不是静默发布成客户人物形象。"""
    from fastapi import HTTPException

    from app.simple_character import (
        CONTACT_SHEET_MIN_SHEET_WIDTH,
        _require_contact_sheet_resolution,
        contact_sheet_placeholder_png,
    )

    narrow = contact_sheet_placeholder_png(b"narrow", width=CONTACT_SHEET_MIN_SHEET_WIDTH - 16)
    with pytest.raises(HTTPException) as excinfo:
        _require_contact_sheet_resolution(narrow, "image/png")
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail["code"] == "CONTACT_SHEET_RESOLUTION_TOO_LOW"

    wide = contact_sheet_placeholder_png(b"wide", width=CONTACT_SHEET_MIN_SHEET_WIDTH)
    _require_contact_sheet_resolution(wide, "image/png")
