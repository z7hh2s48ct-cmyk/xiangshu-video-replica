"""付费探针（视频生成）—— `MetasoProviderTester.paid_test` 的最小计费链路。

定位：探针只对 `provider="metaso"` 生效，复用生产的提交 / 查询实现，提交一次规格
最小的文生视频任务（4 秒 · 768P · 纯文本），再只读回查一次任务状态。要钉死的是
「这次有没有花钱」的口径：

- 提交前的失败（未保存密钥）、供应商明确拒收（4xx / 200 带明确错误）→ 断言
  「未创建收费任务」；
- 超时、5xx、响应无法识别 → 只能说「可能已产生费用」，不得断言未计费；
- 已受理但回查失败 → 提示已受理并带回任务编号，同样不得说未计费；
- 其余服务保持 Noop 501 存根：真实客户端未接入就是未接入。

本文件为无数据库单元 lane：真实传输层不会被触碰，全部走注入的假传输 / 假客户端。
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from email.message import Message
from typing import cast
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

from app.generation import (
    METASO_BASE_URL,
    METASO_CREATE_PATH,
    METASO_QUERY_PATH,
    H3ProviderFailed,
)
from app.metaso_probe import (
    MetasoPaidProbeClient,
    MetasoProbeReadBackFailed,
    MetasoProbeRejected,
    MetasoProbeSettingsUnavailable,
    MetasoProbeUncertain,
)
from app.settings import (
    MetasoProviderTester,
    ProviderTestResult,
    get_provider_tester,
)

_PROBE_CONFIG = {"api_key": "test-only"}


def _detail(excinfo: pytest.ExceptionInfo[HTTPException]) -> dict[str, object]:
    return cast("dict[str, object]", excinfo.value.detail)


# ---------------------------------------------------------------------------
# 客户端：假传输，逐步骤注入响应 / 失败
# ---------------------------------------------------------------------------


def _http_failure(code: int) -> H3ProviderFailed:
    """还原真实传输层的异常形态：H3ProviderFailed，其 __cause__ 是底层 HTTPError。"""
    try:
        try:
            raise HTTPError("https://metaso.cn/x", code, "error", Message(), io.BytesIO(b"{}"))
        except HTTPError as exc:
            raise H3ProviderFailed(f"H3 provider returned HTTP {code}") from exc
    except H3ProviderFailed as failure:
        return failure


def _network_failure() -> H3ProviderFailed:
    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as exc:
            raise H3ProviderFailed("H3 provider request failed") from exc
    except H3ProviderFailed as failure:
        return failure


class ScriptedTransport:
    """按调用顺序返回预设的响应体或抛出预设的异常，并记录每次调用。"""

    def __init__(self, *script: bytes | Exception) -> None:
        self.script = list(script)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _json(payload: object) -> bytes:
    return json.dumps(payload).encode()


_CREATED = _json({"task_id": "vendor-task-1"})
_QUEUED = _json({"status": "queued", "id": "vendor-task-1"})


def _client(transport: ScriptedTransport) -> MetasoPaidProbeClient:
    return MetasoPaidProbeClient(api_key="test-only", transport=transport)


def test_client_submits_the_cheapest_text_only_video_then_reads_it_back() -> None:
    transport = ScriptedTransport(_CREATED, _QUEUED)
    client = _client(transport)

    task_id = client.submit_minimal_video()
    client.read_back(task_id)

    assert task_id == "vendor-task-1"
    create, query = transport.calls
    assert create["method"] == "POST"
    assert create["url"] == f"{METASO_BASE_URL}{METASO_CREATE_PATH}"
    assert cast("dict[str, str]", create["headers"])["Authorization"] == "Bearer test-only"
    request = json.loads(cast("bytes", create["body"]))
    # 最小成本：最短时长、最低分辨率、纯文本（没有任何素材元素，也就不需要公网素材）。
    assert request["duration"] == 4
    assert request["resolution"] == "768P"
    assert request["ratio"] == "16:9"
    assert [item["type"] for item in request["content"]] == ["text"]
    # 回查走生产同一个只读查询端点，且只读不写。
    assert query["method"] == "GET"
    assert query["url"] == f"{METASO_BASE_URL}{METASO_QUERY_PATH}/vendor-task-1"


def test_client_without_an_api_key_never_touches_the_transport() -> None:
    transport = ScriptedTransport()
    with pytest.raises(MetasoProbeSettingsUnavailable):
        MetasoPaidProbeClient(api_key="   ", transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_client_treats_a_4xx_as_a_definite_rejection(status: int) -> None:
    client = _client(ScriptedTransport(_http_failure(status)))
    with pytest.raises(MetasoProbeRejected) as excinfo:
        client.submit_minimal_video()
    assert excinfo.value.http_status == status
    assert excinfo.value.reason == f"HTTP {status}"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_client_treats_a_5xx_as_uncertain(status: int) -> None:
    """5xx 时请求可能已被受理再失败于响应阶段：不能断言未创建任务。"""
    client = _client(ScriptedTransport(_http_failure(status)))
    with pytest.raises(MetasoProbeUncertain):
        client.submit_minimal_video()


def test_client_treats_a_timeout_as_uncertain() -> None:
    client = _client(ScriptedTransport(_network_failure()))
    with pytest.raises(MetasoProbeUncertain):
        client.submit_minimal_video()


def test_client_reads_an_explicit_error_body_as_a_rejection() -> None:
    """200 但响应体明确报错（且没有 task_id）：供应商没有受理，可以断言未计费。"""
    body = _json({"error": {"code": "insufficient_balance", "message": "余额不足"}})
    client = _client(ScriptedTransport(body))
    with pytest.raises(MetasoProbeRejected) as excinfo:
        client.submit_minimal_video()
    assert excinfo.value.http_status is None
    assert excinfo.value.reason == "余额不足"


@pytest.mark.parametrize("body", [b"", b"not json", _json({"status": "ok"}), _json([1, 2])])
def test_client_treats_an_unrecognised_response_as_uncertain(body: bytes) -> None:
    """既没有 task_id 也没有明确错误：无法证明未受理，只能按不确定处理。"""
    client = _client(ScriptedTransport(body))
    with pytest.raises(MetasoProbeUncertain):
        client.submit_minimal_video()


def test_client_does_not_reuse_a_stale_error_body_after_a_timeout() -> None:
    """上一次调用的响应体不得被当作这一次的：超时后必须仍是「不确定」。"""
    error_body = _json({"error": {"code": "x", "message": "旧的报错"}})
    transport = ScriptedTransport(error_body, _network_failure())
    client = _client(transport)
    with pytest.raises(MetasoProbeRejected):
        client.submit_minimal_video()
    with pytest.raises(MetasoProbeUncertain):
        client.submit_minimal_video()


def test_client_reports_a_failed_read_back_with_the_task_id() -> None:
    client = _client(ScriptedTransport(_CREATED, _http_failure(500)))
    task_id = client.submit_minimal_video()
    with pytest.raises(MetasoProbeReadBackFailed) as excinfo:
        client.read_back(task_id)
    assert excinfo.value.task_id == "vendor-task-1"


# ---------------------------------------------------------------------------
# 测试器：假客户端，验证 HTTP 映射与措辞
# ---------------------------------------------------------------------------


class RecordingMetasoProbe:
    def __init__(
        self,
        *,
        task_id: str = "vendor-task-1",
        submit_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.task_id = task_id
        self.submit_error = submit_error
        self.read_error = read_error
        self.calls: list[str] = []

    def submit_minimal_video(self) -> str:
        self.calls.append("submit")
        if self.submit_error is not None:
            raise self.submit_error
        return self.task_id

    def read_back(self, task_id: str) -> None:
        self.calls.append(f"read_back:{task_id}")
        if self.read_error is not None:
            raise self.read_error


def make_tester(probe: RecordingMetasoProbe) -> MetasoProviderTester:
    return MetasoProviderTester(client_factory=lambda _config: probe)


def test_paid_probe_reports_ok_after_submit_and_read_back() -> None:
    probe = RecordingMetasoProbe()
    result = make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))

    assert result == ProviderTestResult(status="ok", provider="metaso", test_kind="paid_probe")
    # 顺序证据：先提交，再用返回的任务编号回查。
    assert probe.calls == ["submit", "read_back:vendor-task-1"]


def test_paid_probe_only_handles_the_metaso_provider() -> None:
    """非 metaso 服务继续走下一级：不得越权宣称有真实客户端。"""
    with pytest.raises(HTTPException) as excinfo:
        MetasoProviderTester().paid_test("deepseek", {"api_key": "k"})

    assert excinfo.value.status_code == 501
    assert _detail(excinfo)["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"


def test_paid_probe_without_a_saved_key_asks_to_save_settings_first() -> None:
    """默认工厂在缺密钥时直接报配置错误：请求根本没发出，也就没有费用。"""
    with pytest.raises(HTTPException) as excinfo:
        MetasoProviderTester().paid_test("metaso", {})

    assert excinfo.value.status_code == 422
    detail = _detail(excinfo)
    assert detail["code"] == "VIDEO_PAID_PROBE_SETTINGS_INVALID"
    assert detail["failure_phase"] == "configuration"
    assert "未创建收费任务" in str(detail["message"])


def test_paid_probe_maps_a_credential_failure_to_422_not_401() -> None:
    """401 会让设置页把管理员登出：凭据错是「设置有误」，必须映射成 422。"""
    probe = RecordingMetasoProbe(
        submit_error=MetasoProbeRejected(http_status=401, reason="HTTP 401")
    )
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 422
    detail = _detail(excinfo)
    assert detail["code"] == "VIDEO_PAID_PROBE_AUTH_FAILED"
    assert detail["failure_phase"] == "authenticate"
    assert "未创建收费任务" in str(detail["message"])


def test_paid_probe_reports_a_vendor_rejection_as_unbilled() -> None:
    probe = RecordingMetasoProbe(
        submit_error=MetasoProbeRejected(http_status=402, reason="HTTP 402")
    )
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 503
    detail = _detail(excinfo)
    assert detail["code"] == "VIDEO_PAID_PROBE_REJECTED"
    assert detail["failure_phase"] == "submit"
    assert "HTTP 402" in str(detail["message"])
    assert "未创建收费任务" in str(detail["message"])
    # 被拒收就不应再去回查。
    assert probe.calls == ["submit"]


def test_paid_probe_reports_an_uncertain_submission_as_possibly_billed() -> None:
    probe = RecordingMetasoProbe(submit_error=MetasoProbeUncertain("unknown"))
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 502
    detail = _detail(excinfo)
    assert detail["code"] == "VIDEO_PAID_PROBE_UNCERTAIN"
    assert "可能已产生费用" in str(detail["message"])
    assert "未创建收费任务" not in str(detail["message"])


def test_paid_probe_reports_a_failed_read_back_with_the_task_id() -> None:
    probe = RecordingMetasoProbe(read_error=MetasoProbeReadBackFailed("vendor-task-1"))
    with pytest.raises(HTTPException) as excinfo:
        make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))

    assert excinfo.value.status_code == 502
    detail = _detail(excinfo)
    assert detail["code"] == "VIDEO_PAID_PROBE_READBACK_FAILED"
    assert detail["failure_phase"] == "query"
    assert "vendor-task-1" in str(detail["message"])
    assert "可能已产生费用" in str(detail["message"])
    assert "未创建收费任务" not in str(detail["message"])


def test_error_codes_and_messages_never_name_the_vendor() -> None:
    """对外错误码与文案保持中性（AGENTS.md 编码红线）：不出现供应商名称。"""
    failures: list[Exception] = [
        MetasoProbeSettingsUnavailable("x"),
        MetasoProbeRejected(http_status=401, reason="HTTP 401"),
        MetasoProbeRejected(http_status=402, reason="HTTP 402"),
        MetasoProbeUncertain("x"),
        MetasoProbeReadBackFailed("vendor-task-1"),
    ]
    for failure in failures:
        probe = RecordingMetasoProbe(submit_error=failure)
        with pytest.raises(HTTPException) as excinfo:
            make_tester(probe).paid_test("metaso", dict(_PROBE_CONFIG))
        detail = _detail(excinfo)
        # 对外响应字段（错误码）与文案都不得出现供应商名称。
        for field in ("code", "message"):
            text = str(detail[field]).lower()
            assert "metaso" not in text
            assert "秘塔" not in text


# ---------------------------------------------------------------------------
# 装配：默认测试器链路里 metaso 已接入，其余服务不受影响
# ---------------------------------------------------------------------------


def test_default_tester_chain_routes_metaso_to_the_real_probe() -> None:
    """缺密钥时默认链路返回 422 配置错误（而不是 501 存根），证明 metaso 已接入。"""
    with pytest.raises(HTTPException) as excinfo:
        get_provider_tester().paid_test("metaso", {})

    assert excinfo.value.status_code == 422
    assert _detail(excinfo)["code"] == "VIDEO_PAID_PROBE_SETTINGS_INVALID"


@pytest.mark.parametrize("provider", ["apilio", "deepseek", "tikhub", "dashscope", "douyidou"])
def test_default_tester_chain_keeps_the_501_stub_for_unwired_providers(provider: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        get_provider_tester().paid_test(provider, {"api_key": "k"})

    assert excinfo.value.status_code == 501
    assert _detail(excinfo)["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"


def test_default_tester_chain_leaves_the_free_connection_test_alone() -> None:
    """metaso 没有免费只读端点：连接测试仍是「只核对参数已保存」，不发外部调用。"""
    result = get_provider_tester().connection_test("metaso", dict(_PROBE_CONFIG))

    assert result.status == "configured_only"
    assert result.test_kind == "connection"
