"""089_customer_api_keys — CW-078 API Key 独立认证泳道（技术方案 §2.5 B / §3.1 line 224）.

customer_api_keys 保存客户程序的 API Key：明文 ``xsk_live_<8prefix>_<40base62>``
只在生成时返回一次，库内只存 ``key_prefix``（8 字符，认证时先按 prefix 定位行）与
``key_digest``（HMAC-SHA256，版本化 key 派生）。认证流：Bearer 明文 → 拆 prefix →
查 ``customer_api_keys`` → 用配置的 key 版本重算 digest 逐一比对 → 检 ``revoked_at``
→ 注入 ``ApiKeyUser``（不经会话围栏，§2.5 B 独立泳道）。``scopes`` 存 TEXT-JSON
而非 JSONB——维持 head ``jsonb_columns = 0`` 的仓库约定（cw056 §617「JSON 全存 TEXT」）。

同时把 ``security_auth_failures`` 的 dimension CHECK 扩到 ``apikey:ip`` / ``apikey:key``，
承载 §2.5 B「复用 record_auth_failure 做 key 尝试限速」——沿 042 追加维度的先例
（drop + create ``ck_security_auth_failures_dimension``）。``apikey:ip`` 是每地址的
暴力尝试预算，``apikey:key`` 是被尝试 key 的 prefix digest（永不落明文，与
``activate:code`` 同理）。

PG-only：API Key 是客户多实例基础设施，且 SQLite 无 ``ALTER CONSTRAINT``——沿 042
先例，非 postgresql 方言直接 return（internal/桌面 lane 不建此表、不扩此约束，
客户泳道本就 PG-only，见 customer_fence._customer_database_configured）。

downgrade 的维度收窄会孤儿化 ``security_auth_failures`` 里已落的 ``apikey:*`` 审计行
（该表 append-only，无法删除），故按 R-B / 039 先例显式 ``RuntimeError`` 拒绝。

Revision ID: 089_customer_api_keys
Revises: 083_recharge_orders_multi_provider
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "089_customer_api_keys"
down_revision = "083_recharge_orders_multi_provider"
branch_labels = None
depends_on = None

# 042 落地的当前维度集；本迁移在其后追加 apikey:ip / apikey:key。
_OLD_DIMENSIONS = (
    "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account', "
    "'admin:exchange:ip', 'session:fencing')"
)
_NEW_DIMENSIONS = (
    "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account', "
    "'admin:exchange:ip', 'session:fencing', 'apikey:ip', 'apikey:key')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # API Key 泳道是客户 PG 基础设施；internal/桌面 SQLite lane 不参与，
        # 且 SQLite 无法 ALTER CONSTRAINT 收窄/放宽 dimension CHECK（042 先例）。
        return

    op.create_table(
        "customer_api_keys",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 明文 key 的固定 8 字符前缀；认证时先按它定位候选行（唯一，前缀永不复用）。
        sa.Column("key_prefix", sa.Text(), nullable=False),
        # HMAC-SHA256(明文 key) 的十六进制；明文永不落库（§2.5 B）。
        sa.Column("key_digest", sa.Text(), nullable=False),
        # 派生 key_digest 所用的 HMAC key 版本，支持轮换窗口内多版本并存认证。
        sa.Column("key_version", sa.Integer(), nullable=False),
        # 授权范围，TEXT-JSON 数组（维持 jsonb_columns=0）；默认空数组=最小权限。
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # 最近一次成功认证时刻（认证泳道在业务事务内回写）。
        sa.Column("last_used_at", sa.Text()),
        # 软吊销时刻；非空即失效，行保留作审计（不硬删）。
        sa.Column("revoked_at", sa.Text()),
        sa.CheckConstraint("key_version >= 1", name="ck_customer_api_keys_key_version"),
    )
    # prefix 定位候选行的唯一入口；全局唯一（吊销后前缀也不复用）。
    op.create_index(
        "uq_customer_api_keys_prefix",
        "customer_api_keys",
        ["key_prefix"],
        unique=True,
    )
    # 密钥管理页按用户列出其 key（created_at DESC, id 稳定次序）。
    op.create_index(
        "idx_customer_api_keys_user_created",
        "customer_api_keys",
        ["user_id", "created_at", "id"],
    )

    # §2.5 B：把 API Key 尝试限速/审计维度接入既有 security 失败流（042 先例）。
    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _NEW_DIMENSIONS,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # R-B / 039 先例：security_auth_failures 是 append-only 审计流，一旦落了
    # apikey:* 行，把 dimension CHECK 收窄回旧集会让这些行违反约束（PG 校验既有行
    # 直接抛底层错误）或孤儿化审计血缘，故显式拒绝降级而不是静默失败。
    has_apikey_failures = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM security_auth_failures "
            "WHERE dimension IN ('apikey:ip', 'apikey:key'))"
        )
    ).scalar()
    if has_apikey_failures:
        raise RuntimeError(
            "cannot downgrade 089_customer_api_keys: apikey:* security_auth_failures "
            "audit rows must survive the rollback. Keep revision 089, or manually "
            "export the API-key auth-failure trail before rolling back."
        )

    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _OLD_DIMENSIONS,
    )
    op.drop_index("idx_customer_api_keys_user_created", table_name="customer_api_keys")
    op.drop_index("uq_customer_api_keys_prefix", table_name="customer_api_keys")
    op.drop_table("customer_api_keys")
