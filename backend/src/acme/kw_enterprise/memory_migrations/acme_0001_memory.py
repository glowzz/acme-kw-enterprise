"""记忆层的三张表：memory、memory_setting、memory_workspace_setting。

扩展的第一条迁移：单独一条分支，依赖开源主线的 0039（KnowWork-stable 钉的 v0.0.4 的主线头）。
字段口径与 acme/kw_enterprise/memory/models.py 一致，改字段两处同步。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "acme_0001"
down_revision = None
branch_labels = ("acme",)
depends_on = "0039"


def upgrade() -> None:
    op.create_table(
        "memory",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False, server_default=""),
        sa.Column("origin_workspace_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="extracted"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("embedding", JSONB(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_mentioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type in ('preference', 'focus', 'fact', 'task')", name="ck_memory_type"),
        sa.CheckConstraint("source in ('extracted', 'user', 'tool')", name="ck_memory_source"),
        sa.CheckConstraint("status in ('active', 'pending', 'watch', 'superseded', 'archived')",
                           name="ck_memory_status"),
        sa.CheckConstraint("char_length(content) between 1 and 500", name="ck_memory_content_len"),
    )
    op.create_index("ix_memory_user_id", "memory", ["user_id"])
    op.create_index("ix_memory_user_status", "memory", ["user_id", "status"])

    op.create_table(
        "memory_setting",
        sa.Column("user_id", sa.String(length=32),
                  sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_organized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "memory_workspace_setting",
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("memory_workspace_setting")
    op.drop_table("memory_setting")
    op.drop_table("memory")  # 表上的两条索引随表一起删，不必先 drop_index
