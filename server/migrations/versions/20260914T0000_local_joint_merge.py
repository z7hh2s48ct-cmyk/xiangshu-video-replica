"""Join shared viral media and the already installed local payment/account schema."""

revision = "20260914T0000_local_joint_merge"
down_revision = (
    "20260913T1600_shared_viral_media",
    "20260913T1825_h3_account_pool",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
