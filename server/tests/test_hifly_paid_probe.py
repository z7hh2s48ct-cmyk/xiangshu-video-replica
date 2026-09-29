"""付费探针（数字人口播）—— `HiflyProviderTester.paid_test` 的真实最小计费链路。

定位：`paid_test` 只对 `provider="hifly"` 生效，且刻意把真实计费提交放在最后
一步——先只读一个已有声音与余额快照。因此：

- 提交前的任何失败（未配置 / 无声音 / 只读超时 / 认证失败）都可无歧义地
  断言「未创建收费任务」；
- 提交结果不确定（`HiflySubmissionUncertain`）时只能如实提示「可能已产生
  费用」，不得断言未计费；
- 其余服务保持 Noop 501 存根（`test_admin_provider_paid_probe.py` 的承诺
  不受影响）：真实客户端未接入就是未接入。

本文件为无数据库单元 lane：假客户端通过 `client_factory` 注入。
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException

from app.hifly import HiflyError, HiflySubmissionUncertain, HiflyTimeoutError
from app.settings import HiflyProviderTester, ProviderTestResult

_PROBE_CONFIG = {"api_key": "test-only"}


class RecordingHiflyProbe:
    """可编排的假客户端：记录调用顺序，允许逐步骤注入失败。"""

    def __init__(
        self,
        *,
        voices: list[dict[str, object]] | None = None,
        credit: int = 42,
        voices_error: Exception | None = None,
        credit_error: Exception | None = None,
        submit_error: Exception | None = None,
    ) -> None:
        self.voices = voices if voices is not None else [{"voice": "voice-1"}]
        self.credit = credit
        self.voices_error = voices_error
        self.credit_error = credit_error
        self.submit_error = submit_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_voices(self, *, page: int = 1, size: int = 10) -> list[dict[str, object]]:
        self.calls.append(("list_voices", {"page": page, "size": size}))
        if self.voices_error is not None:
            raise self.voices_error
        return self.voices

    def account_credit(self) -> int:
        self.calls.append(("account_credit", {}))
        if self.credit_error is not None:
            raise self.credit_error
        return self.credit

    def create_audio_by_tts(self, *, voice: str, text: str, title: str) -> str:
        self.calls.append(("create_audio_by_tts", {"voice": voice, "text": text, "title": title}))
        if self.submit_error is not None:
            raise self.submit_error
        return "audio-task-1"


def make_tester(probe: RecordingHiflyProbe) -> HiflyProviderTester:
    return HiflyProviderTester(client_factory=lambda _config: probe)


def _detail(excinfo: pytest.ExceptionInfo[HTTPException]) -> dict[str, object]:
    return cast("dict[str, object]", excinfo.value.detail)


def test_paid_probe_submits_the_minimal_tts_and_reports_the_credit_snapshot() -> None:
    probe = RecordingHiflyProbe(credit=42)
    result = make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert result == ProviderTestResult(
        status="ok",
        provider="hifly",
        test_kind="paid_probe",
        account_credit=42,
    )
    # 顺序证据：先读声音、再读余额，最后才提交计费任务。
    assert [name for name, _ in probe.calls] == [
        "list_voices",
        "account_credit",
        "create_audio_by_tts",
    ]
    submit = probe.calls[-1][1]
    assert submit["voice"] == "voice-1"
    # 最小成本：极短文本，且文本与标题同源。
    assert submit["text"] == "计费探针"
    assert submit["title"] == "计费探针"


def test_paid_probe_skips_rows_without_a_usable_voice() -> None:
    probe = RecordingHiflyProbe(voices=[{"voice": ""}, {}, {"voice": "voice-2"}])
    result = make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert result.status == "ok"
    assert probe.calls[-1][1]["voice"] == "voice-2"


def test_paid_probe_without_a_voice_asset_never_submits() -> None:
    probe = RecordingHiflyProbe(voices=[])
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 409
    detail = _detail(excinfo)
    assert detail["code"] == "HIFLY_PAID_PROBE_NO_VOICE"
    assert "未创建收费任务" in str(detail["message"])
    # 没有可用的声音就不得发生任何提交。
    assert [name for name, _ in probe.calls] == ["list_voices"]


def test_paid_probe_stays_a_501_stub_for_unwired_providers() -> None:
    """非 hifly 服务继续走 Noop 501：不得越权宣称有真实客户端。"""
    with pytest.raises(HTTPException) as excinfo:
        HiflyProviderTester().paid_test("metaso", {"api_key": "k"})

    assert excinfo.value.status_code == 501
    assert _detail(excinfo)["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"


def test_paid_probe_without_credentials_asks_to_save_settings_first() -> None:
    """hifly 未配置（无 API Key）时不转发 501，而是按配置不完整处理。"""
    with pytest.raises(HTTPException) as excinfo:
        HiflyProviderTester().paid_test("hifly", {})

    assert excinfo.value.status_code == 422
    assert _detail(excinfo)["code"] == "HIFLY_SETTINGS_INVALID"


def test_paid_probe_reports_an_uncertain_submission_as_possibly_billed() -> None:
    """提交不确定：请求可能已到达供应商——措辞必须提示可能已产生费用。"""
    probe = RecordingHiflyProbe(
        submit_error=HiflySubmissionUncertain("数字人服务请求超时，请稍后重试")
    )
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 502
    detail = _detail(excinfo)
    assert detail["code"] == "HIFLY_PAID_PROBE_UNCERTAIN"
    assert "可能已产生费用" in str(detail["message"])
    assert "未创建收费任务" not in str(detail["message"])


def test_paid_probe_reports_a_vendor_rejection_with_its_reason() -> None:
    """供应商明确拒绝（余额不足等）：透出中性原因并断言未创建收费任务。"""
    probe = RecordingHiflyProbe(
        submit_error=HiflyError("数字人服务余额不足，请联系管理员", vendor_code=1002)
    )
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 503
    detail = _detail(excinfo)
    assert detail["code"] == "HIFLY_PAID_PROBE_FAILED"
    assert "余额不足" in str(detail["message"])
    assert "未创建收费任务" in str(detail["message"])
    assert detail["failure_phase"] == "submit"


def test_paid_probe_maps_a_credential_failure_to_422() -> None:
    probe = RecordingHiflyProbe(
        voices_error=HiflyError("数字人服务未正确配置，请联系管理员", vendor_code=2003)
    )
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 422
    detail = _detail(excinfo)
    assert detail["code"] == "HIFLY_AUTH_FAILED"
    assert "未创建收费任务" in str(detail["message"])
    # 认证失败发生在只读阶段：提交必然没有发生。
    assert [name for name, _ in probe.calls] == ["list_voices"]


def test_paid_probe_read_timeout_before_submission_stays_unbilled() -> None:
    probe = RecordingHiflyProbe(credit_error=HiflyTimeoutError("数字人服务请求超时，请稍后重试"))
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("hifly", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 504
    detail = _detail(excinfo)
    assert detail["code"] == "HIFLY_PAID_PROBE_TIMEOUT"
    assert "未创建收费任务" in str(detail["message"])
    assert detail["failure_phase"] == "account_credit"
    assert [name for name, _ in probe.calls] == ["list_voices", "account_credit"]
