"""20260912T1400_customer_registration_credentials — self-service customer registration.

Revision ID: 20260912T1400_customer_registration_credentials
Revises: 20260912T1353_customer_discounts

CW-076 (Phase 4 · 注册): adds the two credential columns the customer
self-registration endpoint writes.

- ``users.password_hash`` holds ONLY a salted memory-hard hash (the same
  scrypt scheme the administrator login already uses); plaintext never
  reaches this schema. It stays NULL for the internal/admin lane and for
  historical activation-code customers, who authenticate by other means.
- ``users.registration_source`` records HOW the account was created so a
  self-registered customer is distinguishable from an admin-created,
  activation-code-bound or bootstrap account.

The CHECK guards only constrain values that are actually present (every clause
is ``IS NULL OR ...``), so the columns can be added to a populated ``users``
table without a validation failure. One guard encodes the registration
invariant at the database level: a ``self_register`` account MUST carry a
password hash, so a registration bug can never persist a password-less
self-service login target.

PG-only: the customer edition is PostgreSQL-only (CW-025) and the SQLite
internal lane never self-registers customers, so the revision is a guarded
no-op there (alembic still stamps the version head).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260912T1400_customer_registration_credentials"
# 后合者重挂+重编号（并行开发迁移Head手册 §3 + MIGRATION-GUARD-20260912 命名守卫）：
# 开工时父为 081_oral_unit_price、旧名 088_customer_registration_credentials；并行期间
# main 链头推进到 20260912T1353_customer_discounts（089/discounts 先合并），且守卫规定
# 新迁移必须用时间戳命名——未发布（未合入 main）的 revision 按守卫改名并重挂到新链头。
down_revision = "20260912T1353_customer_discounts"
branch_labels = None
depends_on = None

# Closed set of account-origins. 'self_register' is the CW-076 public endpoint;
# the others are reserved for the existing admin/activation/bootstrap creation
# paths so a later task can backfill them without another enum widening.
_REGISTRATION_SOURCES = "'self_register', 'admin_create', 'activation_code', 'bootstrap'"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("registration_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_users_password_hash_not_blank",
        "users",
        "password_hash IS NULL OR length(trim(password_hash)) > 0",
    )
    op.create_check_constraint(
        "ck_users_registration_source_known",
        "users",
        f"registration_source IS NULL OR registration_source IN ({_REGISTRATION_SOURCES})",
    )
    op.create_check_constraint(
        "ck_users_self_register_has_password",
        "users",
        "registration_source IS NULL OR registration_source <> 'self_register' "
        "OR password_hash IS NOT NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Refuse to drop the credential columns while any account still carries
    # them: a self-registered customer's password hash is that account's only
    # login secret, so silently discarding it would lock real customers out
    # with no trace (the 026/044/083 data-loss-guard precedent). An empty
    # rehearsal database has no credentialed rows and downgrades symmetrically.
    credentialed = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM users "
            "WHERE password_hash IS NOT NULL OR registration_source IS NOT NULL"
        )
    ).scalar()
    if credentialed:
        raise RuntimeError(
            "cannot downgrade 20260912T1400_customer_registration_credentials: "
            f"{credentialed} user(s) still carry registration credentials "
            "(password_hash / registration_source); remove or migrate them first"
        )

    op.drop_constraint("ck_users_self_register_has_password", "users", type_="check")
    op.drop_constraint("ck_users_registration_source_known", "users", type_="check")
    op.drop_constraint("ck_users_password_hash_not_blank", "users", type_="check")
    op.drop_column("users", "registration_source")
    op.drop_column("users", "password_hash")
