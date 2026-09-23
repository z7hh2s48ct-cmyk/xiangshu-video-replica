"""供应商白名单补全：修复 056 丢失的 zpay，纳入 tikhub/dashscope/douyidou。

Revision ID: 069_provider_whitelist_widen
Revises: 068_script_from_audio_tasks

056 重建 ``ck_provider_settings_supported_provider`` 时沿用了 020 的列表而
漏掉 023 引入的 ``zpay``，导致支付配置保存被 CHECK 拒绝（全量回归）。
本迁移一次性把白名单对齐 ``settings.ProviderName``：补回 zpay，并纳入
C4（tikhub）、提取文案 ASR（dashscope）、链接解析（douyidou）。
"""

from __future__ import annotations

from alembic import op

revision = "069_provider_whitelist_widen"
down_revision = "068_script_from_audio_tasks"
branch_labels = None
depends_on = None

_WIDENED_LIST = (
    "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', "
    "'hifly', 'tikhub', 'dashscope', 'douyidou')"
)
_PREVIOUS_LIST = "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', 'hifly')"


def upgrade() -> None:
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            _WIDENED_LIST,
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM provider_settings WHERE provider IN ('tikhub', 'dashscope', 'douyidou')"
    )
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            _PREVIOUS_LIST,
        )
