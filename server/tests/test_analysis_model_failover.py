"""视频拆解主模型被限流/下线时，自动切换备选模型重试，而不是整条任务直接失败。

背景：gemini-3.8-flash 是当前拆解主模型，线上频繁 429 限流；任务一旦失败
只能等用户手动重试。另据 2026-09-20 事故复盘，上游下线某个模型时表现为
HTTP 400 全量失败。这两类「换个模型就能活」的失败都不该烧掉一次用户任务。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from app import analysis_routes
from app.analysis import (
    APILIO_ANALYSIS_FALLBACK_MODELS,
    APILIO_ANALYSIS_MODEL,
    FAILOVER_RETRY_WAIT_SECONDS,
    HTTP_FAILURE_PHASE,
    REQUEST_FAILURE_PHASE,
    AnalysisProviderFailed,
    ApilioGemini,
    parse_analysis_fallback_models,
)

PRIMARY = "gemini-3.8-flash"
BACKUP = "gemini-3.1-pro-preview"


def _rate_limited() -> AnalysisProviderFailed:
    return AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 429）",
        http_status=429,
        failure_phase=HTTP_FAILURE_PHASE,
        retryable=True,
    )


def _model_unavailable(model: str) -> AnalysisProviderFailed:
    return AnalysisProviderFailed(
        f"视频拆解服务拒绝了请求（HTTP 400）：model {model} is not available",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
    )


def _success_body(content: str = '{"ok": true}') -> bytes:
    return json.dumps({"id": "resp-ok", "choices": [{"message": {"content": content}}]}).encode()


class _ScriptedTransport:
    """按脚本依次返回成功响应或抛出失败，并记录每次请求实际使用的模型名。"""

    def __init__(self, script: list[bytes | Exception]) -> None:
        self._pending = list(script)
        self.requested_models: list[str] = []

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        payload = json.loads(body.decode("utf-8"))
        assert isinstance(payload.get("model"), str) and payload["model"]
        self.requested_models.append(payload["model"])
        outcome = self._pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {}


def _provider(
    script: list[bytes | Exception],
    *,
    model: str = PRIMARY,
    fallback_models: tuple[str, ...] = (BACKUP,),
    pause: Any = None,
) -> tuple[ApilioGemini, _ScriptedTransport]:
    transport = _ScriptedTransport(script)
    provider = ApilioGemini(
        api_key="k",
        model=model,
        fallback_models=fallback_models,
        transport=transport,
        failover_pause=(lambda _seconds: None) if pause is None else pause,
    )
    return provider, transport


def test_rate_limited_primary_falls_over_to_the_backup_model() -> None:
    """429 限流不终结任务：备选模型接着上，结果与溯源都来自备选。"""
    provider, transport = _provider([_rate_limited(), _success_body()])

    response = provider.analyze(
        video_uri="https://example.com/reference.mp4", duration_seconds=15.0
    )

    assert transport.requested_models == [PRIMARY, BACKUP]
    assert response.text == '{"ok": true}'
    assert response.raw["model"] == BACKUP


def test_h3_context_path_falls_over_too() -> None:
    """生产任务走 analyze_with_context（H3 上下文注入），故障转移必须同样生效。"""
    provider, transport = _provider([_rate_limited(), _success_body()])

    response = provider.analyze_with_context(
        video_uri="https://example.com/reference.mp4",
        duration_seconds=15.0,
        context={"mode": None, "generation_assets": [], "issues": [], "media_info": {}},
        media=[],
    )

    assert transport.requested_models == [PRIMARY, BACKUP]
    assert response.raw["model"] == BACKUP


def test_retired_model_http_400_falls_over() -> None:
    """复刻 2026-09-20 事故：上游下线模型表现为 HTTP 400，换模型就能活。"""
    provider, transport = _provider([_model_unavailable(PRIMARY), _success_body()])

    provider.analyze(video_uri="https://example.com/reference.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY, BACKUP]


def test_all_models_failing_raises_the_last_failure_with_a_failover_note() -> None:
    """全部模型都失败时抛最后一次的原始失败，消息注明已尝试过哪些模型。"""
    provider, transport = _provider([_rate_limited(), _rate_limited()])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/reference.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY, BACKUP]
    assert PRIMARY in str(caught.value) and BACKUP in str(caught.value)
    assert caught.value.http_status == 429
    assert caught.value.retryable is True


def test_auth_failure_does_not_waste_backup_attempts() -> None:
    """401/403 是密钥问题，换模型救不了；不应再烧备选请求。"""
    auth_failed = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 401）",
        http_status=401,
        failure_phase=HTTP_FAILURE_PHASE,
    )
    provider, transport = _provider([auth_failed])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/reference.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    # 只尝试过一个模型时不得宣称「已依次尝试全部模型」。
    assert BACKUP not in str(caught.value)


def test_request_phase_failure_does_not_try_backups() -> None:
    """非 HTTPS 地址等请求阶段失败对任何模型都一样，不做无意义的备选调用。"""
    refused = AnalysisProviderFailed(
        "参考视频没有可用的 HTTPS 签名地址",
        failure_phase=REQUEST_FAILURE_PHASE,
    )
    provider, transport = _provider([refused])

    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="http://example.com/reference.mp4", duration_seconds=15.0)

    assert transport.requested_models == []


def test_pause_runs_before_each_backup_attempt() -> None:
    """切换备选前留出间隔，给限流窗口一点恢复时间；间隔可注入便于测试。"""
    waits: list[float] = []
    provider, _ = _provider(
        [_rate_limited(), _rate_limited(), _rate_limited()],
        fallback_models=(BACKUP, "gemini-3.5-flash"),
        pause=waits.append,
    )

    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="https://example.com/reference.mp4", duration_seconds=15.0)

    assert waits == [FAILOVER_RETRY_WAIT_SECONDS, FAILOVER_RETRY_WAIT_SECONDS]


def test_duplicate_and_blank_models_are_dropped_from_the_chain() -> None:
    """设置里的手误（重复、空串、与主模型同名）不得变成无效请求。"""
    transport = _ScriptedTransport([_success_body()])
    provider = ApilioGemini(
        api_key="k",
        model=PRIMARY,
        fallback_models=(PRIMARY, " ", BACKUP, BACKUP),
        transport=transport,
    )

    assert provider.fallback_models == (BACKUP,)


def test_default_construction_keeps_single_model_behavior() -> None:
    """不传备选链时保持原行为（提示词优化等复用方不受影响）。"""
    provider, transport = _provider([_rate_limited()], fallback_models=())

    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="https://example.com/reference.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]


# ---- 设置层接线：get_video_analysis_provider ----


def _provider_for(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> ApilioGemini:
    from unittest.mock import Mock

    repository = Mock()
    repository.load_provider_config.side_effect = lambda provider: (
        config if provider == "apilio" else {}
    )
    monkeypatch.setattr(analysis_routes, "SettingsRepository", lambda conn: repository)
    provider = analysis_routes.get_video_analysis_provider(Mock())
    assert isinstance(provider, ApilioGemini)
    return provider


def test_provider_ships_the_default_backup_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_for(monkeypatch, {"analysis_api_key": "k"})

    assert provider.model == APILIO_ANALYSIS_MODEL
    assert provider.fallback_models == APILIO_ANALYSIS_FALLBACK_MODELS


def test_configured_fallback_list_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_for(
        monkeypatch,
        {
            "analysis_api_key": "k",
            "analysis_model": "m-primary",
            "analysis_model_fallbacks": "m-a, m-b；m-a m-c",
        },
    )

    assert provider.model == "m-primary"
    assert provider.fallback_models == ("m-a", "m-b", "m-c")


def test_parse_fallback_models_blank_uses_shipped_default() -> None:
    assert parse_analysis_fallback_models(None) == APILIO_ANALYSIS_FALLBACK_MODELS
    assert parse_analysis_fallback_models("   ") == APILIO_ANALYSIS_FALLBACK_MODELS


def test_parse_fallback_models_supports_mixed_separators() -> None:
    assert parse_analysis_fallback_models("a, b；c，d e") == ("a", "b", "c", "d", "e")
