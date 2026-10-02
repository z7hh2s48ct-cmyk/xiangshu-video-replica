"""为缺失开始时间的旧实时记录补上可过期租约，避免永久运行中。"""

from alembic import op

revision = "20261002T0030_realtime_legacy_lease"
down_revision = "20261001T2330_collection_record_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    # 开始时间未知时保持未知；租约使用既有创建时间，不触发重发或补扣。
    op.execute("""UPDATE viral_collection_batches
      SET lease_expires_at=COALESCE(started_at,created_at,clock_timestamp())+interval '10 minutes'
      WHERE run_status='RUNNING' AND lease_expires_at IS NULL
        AND config_json::jsonb->>'trigger_kind'='realtime';""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    pass
