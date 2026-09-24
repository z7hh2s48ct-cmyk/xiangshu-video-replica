"""HiFly's documented V2 wire contract, including flat success responses."""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from app.hifly import HiflyClient, HiflyError, HiflyHttpTransport


class WireTransport(HiflyHttpTransport):
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.payload: dict[str, Any] = {}

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None = None
    ) -> bytes:
        self.payload = json.loads(body) if body else {}
        return json.dumps(self.response).encode()


def client(response: dict[str, Any]) -> tuple[HiflyClient, WireTransport]:
    transport = WireTransport(response)
    return HiflyClient(api_key="test-only", transport=transport), transport


@pytest.mark.parametrize(
    "response",
    [{"code": 0, "left": 0}, {"code": 0, "left": 42}, {"code": 0, "data": {"credit": 42}}],
)
def test_credit_preserves_vendor_integer_units(response: dict[str, Any]) -> None:
    vendor, _ = client(response)
    assert vendor.account_credit() == (0 if response.get("left") == 0 else 42)


@pytest.mark.parametrize("value", [True, -1, 1.5, "42", None])
def test_malformed_credit_is_not_silently_zero(value: Any) -> None:
    vendor, _ = client({"code": 0, "left": value})
    with pytest.raises(HiflyError):
        vendor.account_credit()


def test_image_clone_uses_image_url_and_flat_task_receipt() -> None:
    vendor, transport = client({"code": 0, "message": "", "task_id": "image-task"})
    assert (
        vendor.create_avatar_by_image(
            title="test", image_url="https://media.example/portrait.png", aigc_flag=True
        )
        == "image-task"
    )
    assert transport.payload["image_url"] == "https://media.example/portrait.png"
    assert "video_url" not in transport.payload
    assert transport.payload["aigc_flag"] == 1
    assert type(transport.payload["aigc_flag"]) is int


def test_flat_avatar_completion_returns_avatar_identifier() -> None:
    vendor, _ = client({"code": 0, "status": 3, "avatar": "avatar-test"})
    result = vendor.avatar_task("image-task")
    assert result.status == "DONE"
    assert result.avatar_id == "avatar-test"


@pytest.mark.parametrize(
    "method, row",
    [("list_avatars", {"avatar": "avatar-test"}), ("list_voices", {"voice": "voice-test"})],
)
def test_catalog_arrays_are_not_discarded(method: str, row: dict[str, str]) -> None:
    vendor, _ = client({"code": 0, "data": [row]})
    assert getattr(vendor, method)() == [row]


def test_upload_target_can_omit_code_as_documented() -> None:
    vendor, _ = client(
        {
            "upload_url": "https://upload.hifly.cc/test.mp3?test=1",
            "content_type": "audio/mpeg",
            "file_id": "file-test",
        }
    )
    assert vendor.create_upload_url("mp3").file_id == "file-test"
    with pytest.raises(HiflyError, match="状态码"):
        vendor.account_credit()


def test_flat_voice_and_video_completion_preserve_media_query() -> None:
    vendor, _ = client(
        {
            "code": 0,
            "status": 3,
            "voice": "voice-test",
            "demo_url": "https://media.example/demo.mp3?test=1",
        }
    )
    assert vendor.voice_task("voice-task").voice == "voice-test"
    vendor, _ = client(
        {
            "code": 0,
            "status": 3,
            "video_Url": "https://media.example/result.mp4?test=1",
            "duration": 12,
        }
    )
    result = vendor.video_task("video-task")
    assert result.video_url == "https://media.example/result.mp4?test=1"
    assert result.duration == 12


def test_flat_error_does_not_accept_success_fields() -> None:
    vendor, _ = client({"code": 2016, "message": "image unavailable", "task_id": "not-valid"})
    with pytest.raises(HiflyError, match="image unavailable"):
        vendor.create_voice(title="test", file_id="file-test")


def test_text_video_creation_preserves_subtitles_and_integer_watermark_flag() -> None:
    vendor, transport = client({"code": 0, "task_id": "tts-task"})
    result = vendor.create_video_by_tts(
        voice="voice-test",
        text="测试口播内容",
        avatar="avatar-test",
        title="test",
        aigc_flag=True,
        subtitle={"st_show": 1, "st_font_size": 72},
    )
    assert result == "tts-task"
    assert transport.payload["st_show"] == 1
    assert transport.payload["st_font_size"] == 72
    assert type(transport.payload["aigc_flag"]) is int


@pytest.mark.parametrize(
    "subtitle",
    [
        {"avatar": "unvalidated-avatar"},
        {"voice": "unvalidated-voice"},
        {"text": "changed script"},
        {"title": "changed title"},
        {"aigc_flag": 0},
        {"callback_url": "https://example.invalid/callback"},
        {"st_unknown": 1},
        {"st_show": "true"},
        {"st_font_size": True},
        {"st_font_size": 101},
        {"st_primary_color": {"value": "0xFFFFFF"}},
    ],
)
def test_subtitle_invalid_fields_never_reach_provider(subtitle: dict[str, Any]) -> None:
    vendor, transport = client({"code": 0, "task_id": "tts-task"})
    with pytest.raises(HiflyError, match="字幕"):
        vendor.create_video_by_tts(
            voice="voice-test",
            text="approved script",
            avatar="avatar-test",
            title="test",
            aigc_flag=True,
            subtitle=subtitle,
        )
    assert transport.payload == {}


def test_documented_subtitle_fields_preserve_client_values() -> None:
    vendor, transport = client({"code": 0, "task_id": "tts-task"})
    subtitle = {
        "st_show": True,
        "st_font_name": "Arial",
        "st_font_size": 30,
        "st_primary_color": "0xFFFFFF",
        "st_outline_color": "0x000000FF",
        "st_width": 1080,
        "st_height": 200,
        "st_x": 0,
        "st_y": 800,
    }
    vendor.create_video_by_tts(
        voice="voice-test",
        text="approved script",
        avatar="avatar-test",
        title="test",
        aigc_flag=True,
        subtitle=subtitle,
    )
    assert {key: transport.payload[key] for key in subtitle} == subtitle
    assert transport.payload["avatar"] == "avatar-test"
    assert transport.payload["text"] == "approved script"
    assert transport.payload["aigc_flag"] == 1


@pytest.mark.parametrize("mode", ["TTS", "AUDIO"])
def test_invalid_subtitle_is_rejected_before_domain_database_access(mode: str) -> None:
    from app.auth import CurrentUser
    from app.oral import OralDomainError, create_oral_task

    class NoDatabaseAccess:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"unexpected database/actor access: {name}")

    with pytest.raises(OralDomainError, match="字幕"):
        create_oral_task(
            conn=NoDatabaseAccess(),  # type: ignore[arg-type]
            actor=CurrentUser("user-test", "tester", "Tester", "customer"),
            identity_id="identity-test",
            avatar_id="avatar-test",
            voice_id="voice-test",
            mode=mode,
            title="test",
            script_text="approved script",
            audio_asset_id=None,
            subtitle={"avatar": "unvalidated-avatar"},
            idempotency_key="local-test-request",
        )


@pytest.mark.parametrize("subtitle", [{"avatar": "unvalidated-avatar"}, ["avatar"]])
def test_legacy_invalid_subtitle_fails_worker_without_submission(subtitle: Any) -> None:
    from app.oral_worker import OralWorkLease, perform_oral_work

    vendor, transport = client({"code": 0, "task_id": "tts-task"})
    fixture_lease = "local-test-lease"
    lease = OralWorkLease(
        kind="task_submit",
        record_id="task-test",
        worker_id="worker-test",
        lease_token=fixture_lease,
        attempt_count=1,
        row={
            "mode": "TTS",
            "vendor_voice_id": "voice-test",
            "vendor_avatar_id": "avatar-test",
            "script_text": "approved script",
            "title": "test",
            "subtitle_json": json.dumps(subtitle),
        },
    )
    result = perform_oral_work(lease, vendor=vendor, storage=None)  # type: ignore[arg-type]
    assert result.outcome == "failed"
    assert "字幕" in (result.message or "")
    assert transport.payload == {}
