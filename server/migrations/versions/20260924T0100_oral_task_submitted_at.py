"""为 ``oral_tasks`` 增加 ``submitted_at`` 列（口播轮询年龄看播的计时锚点）。

背景（上线评审 P1-2）：generation 侧对 RUNNING 任务有 2 小时轮询年龄上限
（``GENERATION_MAX_POLL_AGE_SECONDS``），供应商任务从查询 API 消失时不会
永远占住用户的唯一公平队列槽；但口播任务的 ``task_poll`` waiting 分支只
无限续轮，供应商任务卡在非终态即可永久冻结该用户的队列槽与全平台共享
生成额度。对齐 generation 需要一个「进入 RUNNING（供应商已受理）」时刻，
``oral_tasks`` 只有 ``created_at``/``updated_at``——前者含排队等待时间
（拿它计时会把长队等待错算成供应商卡死），后者每次续轮都会刷新。因此照
抄 generation 的 ``submitted_at`` 形态新增本列，在提交成功进入 RUNNING
的事务里写入。

存量为 NULL 时按 generation 同款 ``COALESCE(submitted_at, created_at)``
计龄：只影响跨越本次部署、且创建已超 2 小时仍在 RUNNING 的行——它们会
转入 SUBMISSION_UNCERTAIN 进人工对账而不是继续无限占槽，属于安全的
失败方向。

列类型跟随本表 ``created_at``（Text + CURRENT_TIMESTAMP 默认），查询侧用
``::timestamptz`` 转换，与 ``zpay_payments``/``generation`` 既有写法一致。

Revision ID: 20260924T0100_oral_task_submitted_at
Revises: 20260924T0000_wechat_transaction_unique
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924T0100_oral_task_submitted_at"
down_revision = "20260924T0000_wechat_transaction_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 无默认值：QUEUED 行不得被盖上提交时刻，本列只在提交成功进入 RUNNING
    # 的事务里显式写入（oral_worker.finalize_oral_work 的 task_submit 分支）。
    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.add_column(sa.Column("submitted_at", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.drop_column("submitted_at")
