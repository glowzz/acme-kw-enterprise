"""memory_setting 拆分两个水位：last_organized_at（排队标记）与 last_decayed_at（衰减水位）。

M3-R2 审查：两者混用会让自动链路的增量衰减常态为 0（提供者入队即写标记，衰减读到当天，
增量恒 0）。新增 last_decayed_at 供衰减事务独占使用；已排队标记的语义不变。
"""

import sqlalchemy as sa
from alembic import op

revision = "acme_0002"
down_revision = "acme_0001"  # 同分支链式：不重复声明 branch_labels（alembic 会报分支名冲突）
depends_on = None


def upgrade() -> None:
    op.add_column("memory_setting", sa.Column("last_decayed_at", sa.DateTime(timezone=True),
                                              nullable=True))


def downgrade() -> None:
    op.drop_column("memory_setting", "last_decayed_at")
