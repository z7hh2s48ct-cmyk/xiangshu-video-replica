"""客户标注：标签 / 备注 / 负责人（方案 P2-3）。

运营对客户的长期协作信息此前无处落库：``users`` 只有账号与公司名、
``customer_unit_prices`` 只管定价，「这个客户归谁跟、是什么类型、有什么
上下文要交代」只能散落在外部表格里。本迁移新增一张按用户主键关联的小表：

- ``tags_json``：标签数组（JSONB，上限 10 个），列表页作为 chips 展示；
- ``note``：自由备注（上限 2000 字），详情页展示与编辑；
- ``owner_user_id``：负责人（管理员账号，可空——没人认领时显式为空，
  而不是编造一个默认值）；
- ``updated_by_user_id`` / ``updated_at``：最后一次修改的人与时间，
  与 ``customer_unit_prices`` 同一审计口径。

空标注不落行：应用层在三个字段全空时删除整行（而不是保留一行空壳），
因此本表天然只含「有信息」的行，列表 LEFT JOIN 不需要过滤。
PG-only（业务库唯一真源）。downgrade 直接删表：标注是协作信息，不是账务事实。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929T1000_customer_annotations"
down_revision = "20260928T1000_external_call_response_log"
branch_labels = None
depends_on = None

_TABLE = "customer_annotations"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tags_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "owner_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        # 上限在数据库层也钉住：路由是运营看到的 400，CHECK 是最后一道防线
        # （与 revision 039 的「应用层文案 + 数据库护栏」双保险同一思路）。
        sa.CheckConstraint(
            "jsonb_typeof(tags_json) = 'array'",
            name="ck_customer_annotations_tags_array",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(tags_json) <= 10",
            name="ck_customer_annotations_tags_count",
        ),
        sa.CheckConstraint(
            "char_length(note) <= 2000",
            name="ck_customer_annotations_note_length",
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table(_TABLE)
