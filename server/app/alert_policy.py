"""告警配置只复用已有管理账号和邮件通道，不推导新的外发权限。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db_portable import BusinessConnection

AlertKey = Literal[
    "failure_rate", "unconfigured_rates", "reconciliation", "sensitive_events", "collection_budget"
]
RecordType = Literal[
    "VIDEO",
    "ORAL_VIDEO",
    "ORAL_AVATAR",
    "ORAL_VOICE",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "CHARACTER_VIEW_IMAGE",
    "SOURCE_FRAME_AI_SCORE",
    "SOURCE_FRAME_PROCESS",
    "ANALYSIS",
]


class NotificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: AlertKey
    enabled: bool = True
    threshold_count: int = Field(default=1, ge=1, le=1000000)
    window_minutes: int = Field(default=1440, ge=1, le=10080)
    recipient_user_id: str | None = Field(default=None, max_length=200)
    # 空通道显式待配置；空接收人沿用当前设置，不修改既有收件人。
    channel: Literal["email"] | None = "email"

    @model_validator(mode="after")
    def fixed_event_count(self) -> "NotificationPolicy":
        if self.key in {"failure_rate", "collection_budget"} and self.threshold_count != 1:
            raise ValueError("失败率与预算告警使用各自比例阈值，事件数必须为1")
        return self


class FailureRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: RecordType
    error_code: str | None = Field(default=None, min_length=1, max_length=200)
    threshold_percent: float = Field(ge=0, le=100)
    min_sample_size: int = Field(ge=1, le=1000000)


def load_alert_rules(
    conn: BusinessConnection,
) -> tuple[list[NotificationPolicy], list[FailureRule]]:
    row = conn.execute(
        "SELECT notification_policies_json,failure_rules_json FROM alert_settings WHERE id=1"
    ).fetchone()
    if row is None:
        return [], []
    import json

    def payload(value: object) -> list[object]:
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, list):
            raise ValueError("告警配置必须是数组")
        return list(parsed)

    return (
        [NotificationPolicy.model_validate(v) for v in payload(row[0])],
        [FailureRule.model_validate(v) for v in payload(row[1])],
    )


def policy_for(key: str, policies: list[NotificationPolicy]) -> NotificationPolicy:
    return next((p for p in policies if p.key == key), NotificationPolicy(key=key))  # type: ignore[arg-type]
