"""DashScope Fun-ASR provider 单元合同（注入传输层，不出网）。

锁定的行为：短音频走 Flash 同步端点；超过阈值走异步 提交→轮询→下载；
401/403 产出凭据错误文案；异步任务失败/超时产出可读错误；transcription_url
结果解析出全文与时长。
"""

from __future__ import annotations

import base64
import json

import pytest

from app.asr import (
    AsrConfiguration,
    AsrProviderError,
    DashScopeFunAsr,
)

FLASH_THRESHOLD = 300.0


def make_config(**overrides: object) -> AsrConfiguration:
    values: dict[str, object] = {
        "base_url": "https://asr.example",
        "api_key": "test-key",
        "model": "fun-asr",
        "flash_model": "fun-asr-flash",
        "flash_threshold_sec": FLASH_THRESHOLD,
        "poll_interval_sec": 0.0,
        "poll_max_attempts": 5,
    }
    values.update(overrides)
    return AsrConfiguration(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("duration", [12.5, None, "NaN", -1])
@pytest.mark.parametrize("mode", ["flash", "async"])
def test_empty_transcript_retains_only_valid_supplier_duration(duration, mode):
    payload = (
        {"output": {"text": ""}, "usage": {"duration": duration}}
        if mode == "flash"
        else {
            "transcripts": [],
            "properties": {
                "original_duration_in_milliseconds": duration * 1000
                if isinstance(duration, (float, int))
                else duration
            },
        }
    )
    provider = DashScopeFunAsr(
        make_config(), transport=StubTransport([(200, json.dumps(payload).encode())])
    )
    with pytest.raises(AsrProviderError) as error:
        if mode == "flash":
            provider.transcribe("https://media.example/audio", duration_sec=20)
        else:
            provider._download_transcription(
                {
                    "results": [
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "https://media.example/transcript",
                        }
                    ]
                }
            )
    assert error.value.usage_seconds == (12.5 if duration == 12.5 else None)


class StubTransport:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url))
        if headers:
            assert headers.get("Authorization") == "Bearer test-key"
        return self.responses.pop(0)


def test_flash_sync_mode_for_short_audio() -> None:
    transport = StubTransport(
        [
            (
                200,
                json.dumps(
                    {
                        "output": {"text": "短音频转写结果"},
                        "usage": {"duration": 95},
                    }
                ).encode(),
            )
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe("https://media.example/a.m4a", duration_sec=95.0)

    assert result.text == "短音频转写结果"
    assert result.duration_sec == pytest.approx(95.0)
    method, url = transport.calls[0]
    assert (method, url) == (
        "POST",
        "https://asr.example/api/v1/services/aigc/multimodal-generation/generation",
    )


def test_flash_request_describes_the_extracted_m4a_audio() -> None:
    def transport(method, url, *, headers, body, timeout_seconds):
        payload = json.loads(body)
        assert payload["parameters"] == {"format": "m4a", "sample_rate": "16000"}
        assert headers["X-DashScope-SSE"] == "disable"
        return 200, b'{"output":{"text":"ok"},"usage":{"duration":294}}'

    provider = DashScopeFunAsr(make_config(), transport=transport)
    assert provider.transcribe("https://media.example/a.m4a", duration_sec=294).text == "ok"


@pytest.mark.parametrize("duration", [294, 300])
def test_local_pipeline_uploads_inline_audio_before_paid_submission(
    tmp_path, monkeypatch, duration
):
    from app import script_from_audio as pipeline
    from app.storage import LocalStorageAdapter

    audio = b"test extracted m4a bytes"
    storage = LocalStorageAdapter(root=tmp_path / "storage")
    storage.put_object("source.m4a", b"source", content_type="audio/mp4")
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda ffmpeg, source, target: target.write_bytes(audio)
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda *args: duration)
    events = []

    def transport(method, url, *, headers, body, timeout_seconds):
        events.append("POST")
        payload = json.loads(body)
        value = payload["input"]["messages"][0]["content"][0]["input_audio"]["data"]
        assert value.startswith("data:audio/mp4;base64,")
        assert base64.b64decode(value.split(",", 1)[1]) == audio
        assert b"local://" not in body
        assert payload["parameters"]["format"] == "m4a"
        return 200, b'{"output":{"text":"ok"},"usage":{"duration":294}}'

    work = pipeline.PreparedScriptFromAudio(
        "test",
        "project",
        "asset",
        "source.m4a",
        storage,
        DashScopeFunAsr(make_config(), transport=transport),
        "ffmpeg",
        "ffprobe",
        "temp.m4a",
    )
    result = pipeline.perform_script_from_audio_task(
        work, before_provider_call=lambda: events.append("billing")
    )
    assert result.text == "ok"
    assert events == ["billing", "POST"]
    assert storage.head_object("temp.m4a") is None
    assert storage.head_object("source.m4a") is not None
    assert work.audio_deleted


@pytest.mark.parametrize("duration", [300.01, None, float("nan"), 0])
def test_unsupported_local_audio_fails_before_billing_or_provider(tmp_path, monkeypatch, duration):
    from app import script_from_audio as pipeline
    from app.storage import LocalStorageAdapter

    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object("source", b"source", content_type="audio/mp4")
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda ffmpeg, source, target: target.write_bytes(b"audio")
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda *args: duration)
    transport = StubTransport([])
    work = pipeline.PreparedScriptFromAudio(
        "test",
        "project",
        "asset",
        "source",
        storage,
        DashScopeFunAsr(make_config(flash_threshold_sec=600), transport=transport),
        "ffmpeg",
        "ffprobe",
        "temp.m4a",
    )
    billing = []
    with pytest.raises(AsrProviderError, match="本地音频"):
        pipeline.perform_script_from_audio_task(
            work, before_provider_call=lambda: billing.append(True)
        )
    assert not billing
    assert not transport.calls
    assert work.audio_deleted


def test_inline_audio_limit_counts_base64_expansion(monkeypatch):
    import app.asr as asr

    monkeypatch.setattr(asr, "DASHSCOPE_INLINE_MAX_BYTES", 31)
    provider = DashScopeFunAsr(make_config(), transport=StubTransport([]))
    # Prefix is 22 bytes; 6 audio bytes encode to 8 bytes, 7 encode to 12.
    assert provider.prepare_audio_input(
        "local://media/a", audio_bytes=b"123456", duration_sec=30
    ).endswith("MTIzNDU2")
    with pytest.raises(AsrProviderError, match="过大"):
        provider.prepare_audio_input("local://media/a", audio_bytes=b"1234567", duration_sec=30)


def test_public_audio_keeps_signed_url_for_async():
    provider = DashScopeFunAsr(make_config(), transport=StubTransport([]))
    url = "https://media.example/audio.m4a?signature=test"
    assert provider.prepare_audio_input(url, audio_bytes=b"audio", duration_sec=600) == url


@pytest.mark.parametrize("duration", [301, 600])
def test_config_cannot_send_long_audio_to_flash(duration):
    transport = StubTransport([(200, b'{"output":{"task_id":"receipt"}}')])
    provider = DashScopeFunAsr(make_config(flash_threshold_sec=900), transport=transport)
    provider.resume = lambda *args, **kwargs: None
    provider.transcribe("https://media.example/audio.m4a", duration_sec=duration)
    assert transport.calls == [
        ("POST", "https://asr.example/api/v1/services/audio/asr/transcription")
    ]


def test_inline_submission_uncertainty_keeps_audio_without_retry(tmp_path, monkeypatch):
    from app import script_from_audio as pipeline
    from app.asr import AsrSubmissionUncertain
    from app.storage import LocalStorageAdapter

    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object("source", b"source", content_type="audio/mp4")
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda ffmpeg, source, target: target.write_bytes(b"audio")
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda *args: 294)
    calls = []

    def transport(*args, **kwargs):
        calls.append(True)
        raise AsrSubmissionUncertain("connection lost")

    work = pipeline.PreparedScriptFromAudio(
        "test",
        "project",
        "asset",
        "source",
        storage,
        DashScopeFunAsr(make_config(), transport=transport),
        "ffmpeg",
        "ffprobe",
        "temp.m4a",
    )
    with pytest.raises(AsrSubmissionUncertain):
        pipeline.perform_script_from_audio_task(work)
    assert len(calls) == 1
    assert storage.head_object("temp.m4a") is not None
    assert not work.audio_deleted


@pytest.mark.parametrize(
    "url",
    [
        "local://media/audio",
        "file:///tmp/audio",
        "http://127.0.0.1/audio",
        "http://localhost/audio",
    ],
)
def test_non_public_url_is_never_submitted(url):
    transport = StubTransport([])
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="音频"):
        provider.transcribe(url, duration_sec=30)
    assert not transport.calls


def test_http_400_is_actionable_and_does_not_echo_provider_payload(caplog):
    payload = {
        "code": "InvalidParameter",
        "message": "bad local://audio?secret=private",
        "request_id": "40e0734d-096f-9ae3-86c1-a8c013287561",
    }
    transport = StubTransport([(400, json.dumps(payload).encode())])
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="音频格式") as error:
        provider.transcribe("https://media.example/a.m4a", duration_sec=30)
    assert "HTTP 400" in str(error.value)
    assert "private" not in str(error.value) + caplog.text
    assert payload["request_id"] in caplog.text
    assert len(transport.calls) == 1


def test_async_mode_submits_polls_and_downloads() -> None:
    transcription_payload = json.dumps(
        {
            "transcripts": [{"text": "长音频完整转写文本"}],
            "properties": {"original_duration_in_milliseconds": 1_250_000},
        }
    ).encode()
    transport = StubTransport(
        [
            (200, json.dumps({"output": {"task_id": "task-1"}}).encode()),
            (200, json.dumps({"output": {"task_status": "RUNNING"}}).encode()),
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/r.json",
                                }
                            ],
                        }
                    }
                ).encode(),
            ),
            (200, transcription_payload),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe("https://media.example/long.mp4", duration_sec=1250.0)

    assert result.text == "长音频完整转写文本"
    assert result.duration_sec == pytest.approx(1250.0)
    assert [method for method, _ in transport.calls] == ["POST", "GET", "GET", "GET"]
    assert transport.calls[1][1].endswith("/api/v1/tasks/task-1")


def test_async_failure_raises_readable_error() -> None:
    transport = StubTransport(
        [
            (200, json.dumps({"output": {"task_id": "task-2"}}).encode()),
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "FAILED",
                            "results": [{"subtask_status": "FAILED", "message": "音频无法解析"}],
                        }
                    }
                ).encode(),
            ),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="音频无法解析"):
        provider.transcribe("https://media.example/bad.mp4", duration_sec=400.0)


def test_async_poll_timeout_raises() -> None:
    responses: list[tuple[int, bytes]] = [
        (200, json.dumps({"output": {"task_id": "task-3"}}).encode())
    ] + [(200, json.dumps({"output": {"task_status": "RUNNING"}}).encode())] * 6
    provider = DashScopeFunAsr(make_config(), transport=StubTransport(responses))
    with pytest.raises(AsrProviderError, match="轮询超时"):
        provider.transcribe("https://media.example/slow.mp4", duration_sec=None)


def test_invalid_credentials_surface_config_error() -> None:
    transport = StubTransport([(401, b'{"code":"Unauthorized"}')])
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="凭据无效"):
        provider.transcribe("https://media.example/a.m4a", duration_sec=10.0)


def test_missing_api_key_rejected() -> None:
    provider = DashScopeFunAsr(make_config(api_key=""), transport=StubTransport([]))
    with pytest.raises(AsrProviderError, match="未配置"):
        provider.transcribe("https://media.example/a.m4a", duration_sec=10.0)


def test_async_receipt_is_checkpointed_before_poll_and_can_resume() -> None:
    events = []
    result_output = {
        "results": [
            {"subtask_status": "SUCCEEDED", "transcription_url": "https://result.example/text"}
        ]
    }
    responses = [
        (200, json.dumps({"output": {"task_id": "durable-task"}}).encode()),
        (200, json.dumps({"output": {"task_status": "SUCCEEDED", **result_output}}).encode()),
        (200, json.dumps({"transcripts": [{"text": "已识别全文"}]}).encode()),
    ]
    transport = StubTransport(responses)
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe(
        "https://media.example/source.m4a",
        duration_sec=400,
        on_submitted=lambda task_id: events.append((task_id, len(transport.calls))),
        heartbeat=lambda: None,
    )
    assert result.text == "已识别全文"
    assert events == [("durable-task", 1)]
    resumed_transport = StubTransport(responses[1:])
    resumed = DashScopeFunAsr(make_config(), transport=resumed_transport)
    assert resumed.resume("durable-task", heartbeat=lambda: None).text == result.text
    assert [method for method, _ in resumed_transport.calls] == ["GET", "GET"]


@pytest.mark.parametrize("response", [(503, b"{}"), (200, b'{"output":{}}')])
def test_async_submit_without_reliable_receipt_is_uncertain(response) -> None:
    from app.asr import AsrSubmissionUncertain

    provider = DashScopeFunAsr(make_config(), transport=StubTransport([response]))
    with pytest.raises(AsrSubmissionUncertain):
        provider.transcribe("https://media.example/long.m4a", duration_sec=400)


def test_known_receipt_result_download_failure_can_resume() -> None:
    from app.asr import AsrTaskPending

    transport = StubTransport(
        [
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/text",
                                }
                            ],
                        }
                    }
                ).encode(),
            ),
            (503, b"{}"),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrTaskPending):
        provider.resume("existing")
    assert [method for method, _ in transport.calls] == ["GET", "GET"]
