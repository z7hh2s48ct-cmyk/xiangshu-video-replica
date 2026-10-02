"""Provider codes are independent namespaces; unsupported meanings remain unclassified."""

from dataclasses import dataclass

from app.failure_runbook import FailureExplanation


@dataclass(frozen=True)
class ProviderFailureMapping:
    explanation: FailureExplanation
    revision: str
    evidence: str


# Only the response contract already covered by the repository is registered here.
# Adding a mapping requires its source, revision and a response regression fixture.
PROVIDER_FAILURE_MAPPINGS: dict[tuple[str, str], ProviderFailureMapping] = {
    ("apilio_gemini", "overloaded"): ProviderFailureMapping(
        explanation=FailureExplanation(
            category="PROVIDER_BUSY",
            owner="OPS",
            advice="上游服务繁忙。请核对本轮任务和积分状态，按现有规则稍后重试；不要重复提交。",
        ),
        revision="20261002.1",
        evidence="server/tests/test_external_calls_pg.py::_record_failed_call",
    ),
}


def provider_failure_mapping(
    provider: str | None, error_code: str | None
) -> ProviderFailureMapping | None:
    if not provider or not error_code:
        return None
    return PROVIDER_FAILURE_MAPPINGS.get(
        (provider.strip().casefold(), error_code.strip().casefold())
    )


def provider_failure_explanation(
    provider: str | None, error_code: str | None
) -> FailureExplanation:
    mapping = provider_failure_mapping(provider, error_code)
    return (
        mapping.explanation
        if mapping
        else FailureExplanation(
            category="UNCLASSIFIED",
            owner="ENGINEERING",
            advice="服务商原因尚未归类。请交技术核对调用记录，并登记该服务商错误码的依据；核对前不要重复提交或补偿。",
        )
    )
