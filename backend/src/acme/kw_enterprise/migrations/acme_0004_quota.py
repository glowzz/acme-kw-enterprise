"""额度层的五张表：quota_workspace_setting、quota_member_setting、quota_group_setting、quota_grant、quota_change_log。

字段口径与 acme/kw_enterprise/quota/models.py 一致，改字段两处同步。M1 用 workspace/member/grant/log
四张，group_setting 随分支建好（M2 启用部门与用户组默认时不再加迁移）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "acme_0004"
down_revision = "acme_0003"  # 同分支链式：不重复声明 branch_labels（alembic 会报分支名冲突）
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_workspace_setting",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("monthly_total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("member_default_tokens", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="block"),
        sa.Column("updated_by", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "quota_member_setting",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("monthly_tokens", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="block"),
        sa.Column("updated_by", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("monthly_tokens > 0", name="ck_quota_member_positive"),
    )
    op.create_index("ix_quota_member_ws", "quota_member_setting", ["workspace_id"])

    op.create_table(
        "quota_group_setting",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("group_type", sa.String(length=16), primary_key=True),
        sa.Column("group_id", sa.String(length=64), primary_key=True),
        sa.Column("monthly_tokens", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="block"),
        sa.Column("updated_by", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("group_type in ('department', 'user_group')", name="ck_quota_group_type"),
        sa.CheckConstraint("monthly_tokens > 0", name="ck_quota_group_positive"),
    )

    op.create_table(
        "quota_grant",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("extra_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("granted_by", sa.String(length=32), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("extra_tokens > 0", name="ck_quota_grant_positive"),
    )
    op.create_index("ix_quota_grant_lookup", "quota_grant", ["workspace_id", "month", "user_id"])

    op.create_table(
        "quota_change_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("before", JSONB(), nullable=True),
        sa.Column("after", JSONB(), nullable=True),
        sa.Column("changed_by", sa.String(length=32), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quota_change_ws", "quota_change_log", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("quota_change_log")
    op.drop_table("quota_grant")
    op.drop_table("quota_group_setting")
    op.drop_table("quota_member_setting")
    op.drop_table("quota_workspace_setting")
