"""CW-028 共享设置工具合同测试。

共享的 Provider 配置/测试工具必须只有一份中性实现（app.settings），
control 与 admin/settings 两条管理通道以及任何后续消费者都从中导入。
迁移前从 app.settings 导入即为失败（RED），迁移后逐项合同相等（GREEN）。
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException

import app.settings_routes as settings_routes
from app.settings import (
    HiflyProviderTester,
    NoopProviderTester,
    ProviderTester,
    ProviderTestResult,
    StorageProviderTester,
    get_provider_tester,
    merge_provider_config,
    remove_cos_lifecycle_rules,
    require_supported_provider,
)
from app.storage import (
    CloudStorageAdapter,
    CloudStorageConfig,
    FakeStorageAdapter,
    StorageBackendUnavailable,
)

_VALID_COS_CONFIG = {
    "access_key_id": "cos-id",
    "secret_access_key": "cos-secret",
    "bucket": "contract-bucket",
    "region": "ap-shanghai",
}


def test_control_routes_no_longer_import_shared_tools_from_settings_routes() -> None:
    import inspect

    from app import control_routes

    source = inspect.getsource(control_routes)
    assert "settings_routes" not in source


@pytest.mark.parametrize(
    ("symbol",),
    [
        ("ProviderTester",),
        ("ProviderTestResult",),
        ("get_provider_tester",),
        ("merge_provider_config",),
        ("remove_cos_lifecycle_rules",),
        ("require_supported_provider",),
    ],
)
def test_settings_routes_reexports_neutral_implementation(symbol: str) -> None:
    neutral = getattr(__import__("app.settings", fromlist=[symbol]), symbol)
    reexported = getattr(settings_routes, symbol)
    assert reexported is neutral


def test_settings_routes_no_longer_reexports_unused_tester_classes() -> None:
    # 测试器实现类只在中性模块暴露；路由模块仅导入自身消费的符号，
    # 避免留下第二层"看似属于路由层"的实现入口。
    assert not hasattr(settings_routes, "NoopProviderTester")
    assert not hasattr(settings_routes, "HiflyProviderTester")
    assert not hasattr(settings_routes, "StorageProviderTester")


def test_merge_provider_config_overwrites_plain_fields() -> None:
    merged = merge_provider_config(
        {"base_url": "https://old.example", "model": "m1"},
        {"model": "m2"},
    )
    assert merged == {"base_url": "https://old.example", "model": "m2"}


def test_merge_provider_config_masked_secret_keeps_saved_value() -> None:
    merged = merge_provider_config(
        {"api_key": "sk-real"},
        {"api_key": "********abcd"},
    )
    assert merged == {"api_key": "sk-real"}


def test_merge_provider_config_blank_secret_keeps_saved_value() -> None:
    merged = merge_provider_config({"api_key": "sk-real"}, {"api_key": "   "})
    assert merged == {"api_key": "sk-real"}


def test_merge_provider_config_blank_plain_field_is_removed() -> None:
    merged = merge_provider_config({"model": "m1", "keep": "k"}, {"model": ""})
    assert merged == {"keep": "k"}


@pytest.mark.parametrize(
    "provider",
    ["apilio", "metaso", "cos", "deepseek", "hifly", "tikhub", "dashscope", "douyidou"],
)
def test_require_supported_provider_accepts_known_providers(provider: str) -> None:
    assert require_supported_provider(provider) == provider


def test_require_supported_provider_normalizes_case() -> None:
    assert require_supported_provider("COS") == "cos"


def test_require_supported_provider_rejects_unknown_with_422() -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_supported_provider("legacy-provider")
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == {
        "code": "UNSUPPORTED_PROVIDER",
        "message": "该服务已不受支持。",
    }


def test_get_provider_tester_composes_storage_over_hifly_over_noop() -> None:
    tester = get_provider_tester()
    assert isinstance(tester, StorageProviderTester)
    assert isinstance(tester.fallback, HiflyProviderTester)
    assert isinstance(tester.fallback.fallback, NoopProviderTester)


def test_storage_tester_empty_cos_config_falls_through_to_noop() -> None:
    result = StorageProviderTester().connection_test("cos", {})
    assert result == ProviderTestResult(
        status="not_configured",
        provider="cos",
        test_kind="connection",
    )


def test_storage_tester_non_cos_provider_falls_through_to_noop() -> None:
    result = StorageProviderTester().connection_test("metaso", {"api_key": "k"})
    assert result == ProviderTestResult(
        status="configured_only",
        provider="metaso",
        test_kind="connection",
    )


class _FixedCreditProbe:
    def __init__(self, credit: int) -> None:
        self._credit = credit

    def account_credit(self) -> int:
        return self._credit


def test_hifly_tester_reports_account_credit() -> None:
    tester = HiflyProviderTester(client_factory=lambda _config: _FixedCreditProbe(42))
    result = tester.connection_test("hifly", {"api_key": "k"})
    assert result == ProviderTestResult(
        status="ok",
        provider="hifly",
        test_kind="account_credit",
        account_credit=42,
    )


def test_hifly_tester_delegates_non_hifly_provider_to_fallback() -> None:
    result = HiflyProviderTester().connection_test("metaso", {})
    assert result == ProviderTestResult(
        status="not_configured",
        provider="metaso",
        test_kind="connection",
    )


@pytest.mark.parametrize(
    "denied_namespace",
    [
        "projects",
        "generation-results",
        "users",
        "materials",
        "verified-uploads",
        "viral/cover",
        "viral/prepared",
    ],
)
def test_storage_readiness_rejects_missing_business_directory(monkeypatch, denied_namespace):
    adapter = FakeStorageAdapter(provider="cos", bucket="contract-bucket")
    original_put = adapter.put_object

    def restricted_put(key, content, *, content_type):
        if key.startswith(denied_namespace + "/"):
            raise StorageBackendUnavailable("AccessDenied: private provider details")
        return original_put(key, content, content_type=content_type)

    monkeypatch.setattr(adapter, "put_object", restricted_put)
    tester = StorageProviderTester(storage_factory=lambda _config: adapter)
    with pytest.raises(HTTPException) as failure:
        tester.connection_test("cos", _VALID_COS_CONFIG)
    assert failure.value.status_code == 503
    assert failure.value.detail["namespace"] == denied_namespace
    assert denied_namespace in failure.value.detail["message"]
    assert "private provider" not in str(failure.value.detail)
    assert adapter._objects == {}


def test_storage_readiness_verifies_and_cleans_all_business_directories(monkeypatch):
    adapter = FakeStorageAdapter(provider="cos", bucket="contract-bucket")
    original_put = adapter.put_object
    checked = []

    def record_put(key, content, *, content_type):
        checked.append(key.split("/settings-diagnostics/")[0])
        return original_put(key, content, content_type=content_type)

    monkeypatch.setattr(adapter, "put_object", record_put)
    result = StorageProviderTester(storage_factory=lambda _config: adapter).connection_test(
        "cos", _VALID_COS_CONFIG
    )
    assert result.status == "ok"
    assert checked == [
        "projects",
        "generation-results",
        "users",
        "materials",
        "verified-uploads",
        "viral/cover",
        "viral/prepared",
    ]
    assert adapter._objects == {}


def test_noop_tester_paid_test_requires_real_provider_client() -> None:
    with pytest.raises(HTTPException) as excinfo:
        NoopProviderTester().paid_test("metaso", {"api_key": "k"})
    assert excinfo.value.status_code == 501
    detail = cast("dict[str, object]", excinfo.value.detail)
    assert detail["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"


def test_provider_test_result_dumps_without_none_account_credit() -> None:
    result = ProviderTestResult(status="ok", provider="cos", test_kind="storage_connection")
    assert result.account_credit is None
    assert result.model_dump(exclude_none=True) == {
        "status": "ok",
        "provider": "cos",
        "test_kind": "storage_connection",
    }


def test_remove_cos_lifecycle_rules_removes_legacy_rules_and_reports_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class RecordingLifecycleClient:
        def get_bucket_lifecycle(self, **_kwargs: object) -> dict[str, object]:
            return {
                "Rule": [
                    {
                        "ID": "expire-project-media-180d",
                        "Filter": {"Prefix": "projects/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": 180},
                    }
                ]
            }

        def delete_bucket_lifecycle(self, **kwargs: object) -> None:
            calls.append(cast("dict[str, object]", kwargs))

    def fake_factory(config: object) -> CloudStorageAdapter:
        return CloudStorageAdapter(
            cast("CloudStorageConfig", config), client=RecordingLifecycleClient()
        )

    monkeypatch.setattr("app.settings.create_storage_adapter", fake_factory)

    result = remove_cos_lifecycle_rules(dict(_VALID_COS_CONFIG), actor_id="actor-1")
    assert result["status"] == "removed"
    assert "永久保存" in str(result["message"])
    assert len(calls) == 1


def test_remove_cos_lifecycle_rules_skips_non_cloud_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.settings.create_storage_adapter",
        lambda _config: FakeStorageAdapter(provider="fake", bucket="fake-bucket"),
    )
    result = remove_cos_lifecycle_rules(dict(_VALID_COS_CONFIG), actor_id="actor-1")
    assert result["status"] == "skipped"
    assert "不支持生命周期规则" in str(result["message"])


def test_remove_cos_lifecycle_rules_skips_when_adapter_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_factory(_config: object) -> object:
        raise ValueError("invalid config")

    monkeypatch.setattr("app.settings.create_storage_adapter", broken_factory)
    result = remove_cos_lifecycle_rules(dict(_VALID_COS_CONFIG), actor_id="actor-1")
    assert result["status"] == "skipped"
    assert "跳过" in str(result["message"])


def test_remove_cos_lifecycle_rules_reports_failed_without_blocking_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingLifecycleClient:
        def get_bucket_lifecycle(self, **_kwargs: object) -> dict[str, object]:
            raise StorageBackendUnavailable("cloud lifecycle rule read failed")

    def fake_factory(config: object) -> CloudStorageAdapter:
        return CloudStorageAdapter(
            cast("CloudStorageConfig", config), client=FailingLifecycleClient()
        )

    monkeypatch.setattr("app.settings.create_storage_adapter", fake_factory)
    result = remove_cos_lifecycle_rules(dict(_VALID_COS_CONFIG), actor_id="actor-1")
    assert result["status"] == "failed"
    assert "重新保存" in str(result["message"])


def test_provider_tester_protocol_contract_is_preserved() -> None:
    class _FakeTester:
        def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
            return ProviderTestResult(status="ok", provider=provider, test_kind="connection")

        def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
            raise AssertionError("contract tests must not trigger paid tests")

    probe: ProviderTester = _FakeTester()
    assert probe.connection_test("cos", {}).status == "ok"
