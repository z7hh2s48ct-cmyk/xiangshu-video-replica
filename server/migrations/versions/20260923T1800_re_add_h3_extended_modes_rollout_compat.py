"""Re-add ``runtime_settings.h3_extended_modes_enabled`` as a rollout shim.

20260923T0000 dropped this column in the same release train that stopped
reading it. That broke the deployment contract the rollout script relies on
(``DATABASE_HEAD_LEFT_FORWARD_COMPATIBLE``): during the MIGRATE → ROLL_API
window an old image still runs ``SELECT h3_extended_modes_enabled`` on the
generation protocol path and 500s, and an image rollback after the deploy
would keep failing until ``database-before.dump`` is restored by hand.

This migration restores the column exactly as 075_independent_creation
created it (Boolean NOT NULL, server_default FALSE). New code never reads it;
old code reads FALSE and behaves exactly as it did before the train. Drop it
again in a later release, once no pre-#196 image can run against the schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260923T1800_re_add_h3_extended_modes_rollout_compat"
down_revision = "20260923T1200_admin_refund_adjustment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 部署兼容垫片：列值本身无业务意义（新代码不读），存在性即目的。
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "h3_extended_modes_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("FALSE"),
            )
        )


def downgrade() -> None:
    # 回到 20260923T1200 的形状（列不存在）；执行本 downgrade 的前提同样是
    # 没有旧镜像会再读该列。
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_column("h3_extended_modes_enabled")
