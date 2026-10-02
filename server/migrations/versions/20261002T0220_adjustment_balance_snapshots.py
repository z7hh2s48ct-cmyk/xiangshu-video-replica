"""调整前后余额只记录真实写事务的快照，不猜测历史钱包期初数。"""

from alembic import op

revision = "20261002T0220_adjustment_balance_snapshots"
down_revision = "20261002T0115_generation_status_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE admin_adjustments ADD COLUMN balance_before integer")
    op.execute("ALTER TABLE admin_adjustments ADD COLUMN balance_after integer")
    op.execute("""ALTER TABLE admin_adjustments ADD CONSTRAINT ck_adjustment_balance_snapshots
        CHECK((balance_before IS NULL AND balance_after IS NULL) OR
              (balance_before IS NOT NULL AND balance_after IS NOT NULL AND
               balance_before>=0 AND balance_after>=0))""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE admin_adjustments DROP CONSTRAINT ck_adjustment_balance_snapshots")
    op.execute("ALTER TABLE admin_adjustments DROP COLUMN balance_before")
    op.execute("ALTER TABLE admin_adjustments DROP COLUMN balance_after")
