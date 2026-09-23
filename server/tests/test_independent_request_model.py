"""PG-free unit tests for the independent-creation request contract.

These cover the *pure* layers of ``app.independent`` that need no database:

* the ``IndependentVideoRequest`` pydantic model — the unified mixed
  ``reference_asset_ids`` list (image/video/audio share one field) and its
  total-length cap (12 files);
* ``_validate_independent_mode_assets`` — the extracted mode/asset matrix that
  decides which inputs each generation mode may carry: R2V requires ≥1
  reference and forbids first/last frame; T2V/I2V may not carry any reference
  list (references are R2V-only); I2V requires a first frame; T2V forbids
  first/last frame;
* ``_validate_reference_kind_limits`` — the per-kind reference caps
  (image ≤ 8, video ≤ 3, audio ≤ 3) applied after the backend splits the mixed
  ``reference_asset_ids`` list by each asset's kind.

The database-backed ``create_independent_batch`` baseline (the kind split
itself, prompt-snapshot contents, wallet RESERVE, the PG worker protocol
payload) lives in the PG-gated ``test_independent_creation.py`` and only runs
on the Linux CI lane; this file exists so the request contract, the mode
matrix, and the per-kind caps stay verifiable without a PostgreSQL fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.independent import (
    IndependentVideoRequest,
    _validate_independent_mode_assets,
    _validate_reference_kind_limits,
)


def _request(**overrides: Any) -> IndependentVideoRequest:
    payload: dict[str, Any] = {
        "mode": "r2v",
        "prompt_text": "参考生成",
        "output_duration_seconds": 6,
        "quantity": 1,
        "idempotency_key": "key",
    }
    payload.update(overrides)
    return IndependentVideoRequest(**payload)


# ---------------------------------------------------------------------------
# Request model: unified mixed reference_asset_ids + total-length cap.
# ---------------------------------------------------------------------------


def test_request_defaults_reference_asset_ids_to_empty() -> None:
    request = _request()
    assert request.reference_asset_ids == []


def test_request_accepts_video_display_name() -> None:
    assert _request(display_name="乡墅庭院成片").display_name == "乡墅庭院成片"


def test_request_accepts_mixed_reference_asset_ids() -> None:
    request = _request(reference_asset_ids=["image-1", "video-1", "audio-1"])
    assert request.reference_asset_ids == ["image-1", "video-1", "audio-1"]


def test_request_rejects_reference_asset_ids_over_total_limit() -> None:
    # 多模态文件总数最多12；13项触发请求校验。
    with pytest.raises(ValidationError):
        _request(reference_asset_ids=[f"ref-{index}" for index in range(13)])


# ---------------------------------------------------------------------------
# Mode/asset matrix (pure): which inputs each mode may carry.
# ---------------------------------------------------------------------------


def test_matrix_r2v_accepts_mixed_references() -> None:
    _validate_independent_mode_assets(
        _request(reference_asset_ids=["image-1", "video-1", "audio-1"]),
    )


def test_matrix_r2v_requires_at_least_one_reference() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(_request())
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_REQUIRED"


def test_matrix_r2v_rejects_duplicate_references() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(
            _request(reference_asset_ids=["image-1", "image-1"]),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_DUPLICATE"


def test_matrix_r2v_rejects_first_frame() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(
            _request(reference_asset_ids=["image-1"], first_frame_asset_id="frame-1"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"


def test_matrix_rejects_reference_list_for_t2v() -> None:
    request = _request(mode="t2v", reference_asset_ids=["image-1"])
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(request)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"


def test_matrix_rejects_reference_list_for_i2v() -> None:
    request = _request(
        mode="i2v",
        first_frame_asset_id="frame-1",
        reference_asset_ids=["image-1"],
    )
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(request)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"


def test_matrix_i2v_requires_first_frame() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(_request(mode="i2v"))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_FIRST_FRAME_REQUIRED"


def test_matrix_t2v_rejects_first_frame() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(
            _request(mode="t2v", first_frame_asset_id="frame-1"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"


def test_matrix_r2v_with_references_is_always_accepted() -> None:
    """门禁移除后 R2V 恒开放：不再有 409 ``EXTENDED_MODE_PENDING_VERIFICATION``。

    取代原 ``test_matrix_extended_gate_blocks_r2v_when_disabled``：
    ``extended_enabled`` 形参连同该错误码已随迁移
    ``20260923T0000_open_h3_extended_modes`` 一并退休，带参考图的 R2V 提交
    必须直接通过本函数（无异常），与 ``test_independent_creation`` 里
    ``test_extended_modes_submit_without_any_flag`` 的新契约一致。
    """
    _validate_independent_mode_assets(_request(reference_asset_ids=["image-1"]))


def test_matrix_t2v_rejects_adaptive_ratio() -> None:
    # BUG-1（2026-09-19 真实付费核对）：纯文本 T2V 供应商要求 ratio 必填
    # 且不能为 adaptive（否则 400/err 2013），建批入口必须 422 拦下。
    with pytest.raises(HTTPException) as exc:
        _validate_independent_mode_assets(_request(mode="t2v", ratio="adaptive"))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_T2V_RATIO_REQUIRED"


def test_matrix_t2v_accepts_concrete_ratio() -> None:
    _validate_independent_mode_assets(_request(mode="t2v", ratio="16:9"))


def test_matrix_i2v_allows_adaptive_ratio() -> None:
    # I2V 的 ratio 由 build_h3_request 归一为 adaptive，入口不应拦截。
    _validate_independent_mode_assets(
        _request(mode="i2v", first_frame_asset_id="frame-1", ratio="adaptive"),
    )


# ---------------------------------------------------------------------------
# Per-kind reference caps (pure): image ≤ 8, video ≤ 3, audio ≤ 3.
# ---------------------------------------------------------------------------


def test_kind_limits_accept_within_caps() -> None:
    _validate_reference_kind_limits(image_count=8, video_count=3, audio_count=3)


def test_kind_limits_reject_images_over_cap() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_reference_kind_limits(image_count=9, video_count=0, audio_count=0)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED"


def test_kind_limits_reject_videos_over_cap() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_reference_kind_limits(image_count=0, video_count=4, audio_count=0)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED"


def test_kind_limits_reject_audios_over_cap() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_reference_kind_limits(image_count=0, video_count=0, audio_count=4)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED"
