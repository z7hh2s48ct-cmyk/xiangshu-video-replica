"""api_metadata 允许在计费请求仍处于 PENDING 时写入（追加修复）。

``20260913T1100_itemized_billing`` 的不可变触发器把 ``billing_operations`` 上除
终态列以外的任何字段都当作已冻结事实，而 ``20260920T0100`` 之后的
``begin_source_attempt`` 需要在客户计费请求上补写 ``api_metadata``（viral_data
的 API 类型，供管理端按节点拆解成本）——两条规则相撞：带客户计费上下文的
搜索 / 详情 / 刷新外呼会在 ``UPDATE billing_operations SET api_metadata`` 处被
拒绝，整次外呼连同预留一起失败。刷新接口此前绕过计量层才没暴露这一点。

修复方式是把 ``api_metadata`` 加入该表的可写列白名单：请求仍是 PENDING 时可写，
一旦进入终态（SUCCEEDED / FAILED / CANCELLED）整行依旧冻结——金额事实的审计
语义不变，只是把描述性元数据与账务事实分开对待。

Revision ID: 20260925T1400_api_metadata_pending_write
Revises: 20260924T0200_customer_oral_task_visibility
"""

from __future__ import annotations

from alembic import op

revision = "20260925T1400_api_metadata_pending_write"
down_revision = "20260924T0200_customer_oral_task_visibility"
branch_labels = None
depends_on = None

# 触发器函数被三张表共用（billing_operations / billing_attempts / billing_evidence），
# 只替换其中的 billing_operations 分支：可写列白名单多一个 api_metadata。
# 白名单以「前置逗号 + 值」注入（而非「值 + 后置逗号」）：降级时传空串必须得到与
# 20260913T1100 逐字相同的 ARRAY 字面量，写成后置逗号会留下 `'completed_at',]`
# ——那是语法错，降级路径会当场炸掉（升级路径恰好掩盖了它）。
_CREATE_TRIGGER_FUNCTION = """
        CREATE OR REPLACE FUNCTION billing_refuse_fact_rewrite() RETURNS trigger
          LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='TRUNCATE' OR TG_TABLE_NAME='billing_evidence' THEN
            RAISE EXCEPTION 'billing evidence is append only';
          END IF;
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'billing facts cannot be deleted'; END IF;
          IF TG_TABLE_NAME='billing_operations' THEN
            IF (to_jsonb(OLD)-ARRAY['state','actual_units','charged_credits','revenue_fen',
              'nominal_revenue_fen','completed_at'{operations_extra}])
                IS DISTINCT FROM
               (to_jsonb(NEW)-ARRAY['state','actual_units','charged_credits','revenue_fen',
                 'nominal_revenue_fen','completed_at'{operations_extra}])
              OR (OLD.state<>'PENDING' AND NEW IS DISTINCT FROM OLD)
            THEN RAISE EXCEPTION
              'accepted billing snapshots and terminal facts are immutable'; END IF;
          ELSIF TG_TABLE_NAME='billing_attempts' THEN
            IF (to_jsonb(OLD)-ARRAY['state','usage','cost_fen','completed_at']) IS DISTINCT FROM
               (to_jsonb(NEW)-ARRAY['state','usage','cost_fen','completed_at'])
              OR (OLD.state<>'PENDING' AND NEW IS DISTINCT FROM OLD)
            THEN RAISE EXCEPTION 'provider rate snapshots and completed costs are immutable';
              END IF;
          END IF;
          RETURN NEW;
        END $$;
"""


def _replace_trigger_function(*, allow_api_metadata: bool) -> None:
    op.execute(
        _CREATE_TRIGGER_FUNCTION.format(
            operations_extra=",'api_metadata'" if allow_api_metadata else ""
        )
    )


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    _replace_trigger_function(allow_api_metadata=True)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _replace_trigger_function(allow_api_metadata=False)
