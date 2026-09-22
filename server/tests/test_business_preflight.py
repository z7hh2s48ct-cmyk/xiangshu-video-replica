from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app import analysis_routes, rbac_routes


def test_voice_clone_rejects_over_20mb_before_consent_or_billing(monkeypatch):
    from app import oral

    monkeypatch.setattr(oral, "require_not_auditor", Mock())
    monkeypatch.setattr(oral, "_require_own_identity", Mock())
    monkeypatch.setattr(
        oral,
        "_require_biometric_source_asset",
        Mock(
            return_value={
                "size_bytes": 20 * 1024 * 1024 + 1,
                "sha256": "verified",
            }
        ),
    )
    monkeypatch.setattr(oral, "_require_audio_purpose", Mock())
    consent = Mock(
        side_effect=AssertionError("Must reject oversize input before consent or writes")
    )
    monkeypatch.setattr(oral, "_require_valid_consent", consent)
    with pytest.raises(oral.OralDomainError, match="20 MB"):
        oral.start_voice_clone(
            Mock(),
            actor=SimpleNamespace(id="user-1"),
            identity_id="person-1",
            title="test",
            source_asset_id="audio-1",
            consent_id="consent-1",
            idempotency_key="test-voice-size",
        )


class RequestDb:
    def __init__(self, conn, actor):
        self.conn, self.actor = conn, actor

    @contextmanager
    def write(self):
        yield self.conn, self.actor


def bare_http_request() -> object:
    """Minimal Starlette request so route handlers can read the X-Request-Id header."""
    from starlette.requests import Request as StarletteRequest

    return StarletteRequest({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_first_frame_can_read_existing_local_reference_after_cloud_switch(tmp_path, monkeypatch):
    from app import first_frames
    from app.storage import FakeStorageAdapter, LocalStorageAdapter

    local = LocalStorageAdapter(root=tmp_path)
    reference = local.put_object(
        "users/person/reference.png", b"existing-image", content_type="image/png"
    )
    cloud = FakeStorageAdapter(provider="cos", bucket="configured-bucket")
    monkeypatch.setattr(
        first_frames, "create_local_storage_from_environment", lambda: local, raising=False
    )
    image = first_frames.read_asset_image(
        cloud, {"id": "reference-1", "content_type": "image/png", "storage_uri": reference.uri}
    )
    assert image.content == b"existing-image"
    assert cloud._objects == {}


def test_first_frame_cloud_bucket_mismatch_cannot_fall_back_to_local(monkeypatch):
    from app import first_frames
    from app.storage import FakeStorageAdapter

    local = Mock(side_effect=AssertionError("Cloud bucket mismatch must not read local files"))
    monkeypatch.setattr(first_frames, "create_local_storage_from_environment", local, raising=False)
    with pytest.raises(HTTPException) as failure:
        first_frames.read_asset_image(
            FakeStorageAdapter(provider="cos", bucket="configured-bucket"),
            {
                "id": "reference-1",
                "content_type": "image/png",
                "storage_uri": "cos://other-bucket/image.png",
            },
        )
    assert failure.value.detail["code"] == "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE"
    local.assert_not_called()


def test_first_frame_legacy_local_read_remains_forbidden_in_customer_production(monkeypatch):
    from app import bootstrap, first_frames
    from app.storage import FakeStorageAdapter

    monkeypatch.setattr(bootstrap, "is_customer_production", lambda: True)
    local = Mock(side_effect=AssertionError("Production must not read legacy local assets"))
    monkeypatch.setattr(first_frames, "create_local_storage_from_environment", local)
    with pytest.raises(HTTPException) as failure:
        first_frames.read_asset_image(
            FakeStorageAdapter(provider="cos", bucket="configured-bucket"),
            {
                "id": "reference-1",
                "content_type": "image/png",
                "storage_uri": "local://assets/image.png",
            },
        )
    assert failure.value.detail["code"] == "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE"
    local.assert_not_called()


def test_cloud_upload_completion_failure_is_retryable_and_cannot_queue_analysis(monkeypatch):
    from app import media_routes
    from app.storage import StorageBackendUnavailable

    monkeypatch.setattr(media_routes, "prepare_upload_completion", Mock())
    monkeypatch.setattr(
        media_routes,
        "probe_upload_completion",
        Mock(side_effect=StorageBackendUnavailable("private provider failure")),
    )
    persist = Mock()
    monkeypatch.setattr(media_routes, "persist_upload_completion", persist)
    with pytest.raises(HTTPException) as failure:
        media_routes.complete_asset_upload(
            "asset-1",
            RequestDb(Mock(), SimpleNamespace(role="customer")),
            Mock(),
            Mock(),
            bare_http_request(),
        )
    assert failure.value.status_code == 503
    assert failure.value.detail["code"] == "STORAGE_PROVIDER_UNAVAILABLE"
    assert "重试" in failure.value.detail["message"]
    assert "private provider" not in str(failure.value.detail)
    persist.assert_not_called()


@pytest.mark.parametrize("requires_https, expected", [(True, False), (False, True)])
def test_upload_does_not_auto_queue_analysis_with_unreadable_local_input(
    monkeypatch, requires_https, expected
):
    from app.media import _automatic_analysis_input_ready

    provider = Mock(return_value=SimpleNamespace(requires_https_video_url=requires_https))
    monkeypatch.setattr(analysis_routes, "get_video_analysis_provider", provider)
    assert _automatic_analysis_input_ready(Mock(), "local://video.mp4") is expected
    provider.reset_mock()
    assert _automatic_analysis_input_ready(Mock(), "cos://video.mp4") is True
    provider.assert_not_called()


def test_download_grant_uses_request_session_when_other_sessions_overlap(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from app.customer_auth import CustomerSessionContext

    conn = Mock()
    conn.ctx = CustomerSessionContext("user-1", None, "device-1", "current-session", 3, "future")
    conn.execute.side_effect = AssertionError("Must not guess a session from the account")
    actor = SimpleNamespace(id="user-1", role="customer")
    monkeypatch.setattr(rbac_routes, "require_not_auditor", Mock())
    monkeypatch.setattr(
        rbac_routes,
        "require_asset_access",
        Mock(
            return_value={
                "project_id": "project-1",
                "size_bytes": 123,
                "sha256": "verified",
                "storage_uri": "local://test/video.mp4",
            }
        ),
    )
    monkeypatch.setattr(rbac_routes, "write_audit", Mock())
    monkeypatch.setattr(rbac_routes, "storage_for_asset", Mock())
    monkeypatch.setattr(rbac_routes, "settings_encryption_key", lambda: "test-only-signing-secret")
    result = rbac_routes.create_download_url("asset-1", RequestDb(conn, actor))
    assert parse_qs(urlsplit(result.url).query)["session_epoch"] == ["current-session:3"]


def test_audio_duration_probe_reads_a_closed_temporary_file():
    import io
    import wave

    from app.materials import probe_audio_duration

    content = io.BytesIO()
    with wave.open(content, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    assert probe_audio_duration(content.getvalue()) == pytest.approx(1.0)


def test_unfinished_upload_cannot_issue_download_url_or_success_audit(monkeypatch):
    monkeypatch.setattr(rbac_routes, "require_not_auditor", Mock())
    monkeypatch.setattr(
        rbac_routes,
        "require_asset_access",
        Mock(
            return_value={
                "project_id": "project-1",
                "size_bytes": 0,
                "sha256": "",
                "storage_uri": "local://test/empty.mp4",
            }
        ),
    )
    audit = Mock()
    monkeypatch.setattr(rbac_routes, "write_audit", audit)
    storage = Mock(side_effect=AssertionError("Unfinished upload must not reach storage"))
    monkeypatch.setattr(rbac_routes, "storage_for_asset", storage)
    with pytest.raises(HTTPException) as failure:
        rbac_routes.create_download_url("asset-1", RequestDb(Mock(), SimpleNamespace(id="user-1")))
    assert failure.value.status_code == 409
    assert failure.value.detail["code"] == "ASSET_UPLOAD_NOT_COMPLETE"
    audit.assert_not_called()


def test_real_analysis_rejects_local_asset_before_creating_task(monkeypatch):
    from contextlib import contextmanager

    class Db:
        @contextmanager
        def write(self):
            conn = Mock()
            conn.execute.return_value.fetchone.return_value = None
            yield conn, SimpleNamespace(id="user-1")

    monkeypatch.setattr(
        analysis_routes,
        "validate_analysis_enqueue",
        Mock(
            return_value=(
                {"storage_uri": "local://test/video.mp4", "sha256": "validated"},
                10,
            )
        ),
    )
    monkeypatch.setattr(
        analysis_routes,
        "get_video_analysis_provider",
        Mock(return_value=SimpleNamespace(requires_https_video_url=True)),
    )
    enqueue = Mock(side_effect=AssertionError("Known unavailable input must not reserve points"))
    monkeypatch.setattr(analysis_routes, "enqueue_analysis_task", enqueue)
    with pytest.raises(HTTPException) as failure:
        analysis_routes.create_project_analysis_task(
            "project-1",
            analysis_routes.CreateAnalysisRequest(asset_id="asset-1"),
            Db(),
            bare_http_request(),
        )
    assert failure.value.detail["code"] == "ANALYSIS_VIDEO_URL_UNAVAILABLE"
    enqueue.assert_not_called()


def test_analysis_duration_rejects_reference_video_over_limit() -> None:
    """参考视频实测时长超过 15 秒上限时，拆解入口必须明确报错而非静默失败。"""
    import json

    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "metadata_json": json.dumps({"duration_seconds": 81.0})
    }
    with pytest.raises(HTTPException) as failure:
        analysis_routes.analysis_duration_for_asset(
            conn, asset_id="asset-1", requested_duration=None
        )
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "ANALYSIS_DURATION_EXCEEDED"
    assert "15" in failure.value.detail["message"]
