"""回填衰减水位 last_decayed_at = last_organized_at（R4 审查：回填必须独立成新 revision）。

旧实现（≤R2）按 last_organized_at 做过增量衰减——对「队列真的跑完过」的存量行，score 已
反映到那一刻；对「只入队未跑完」的行，该值偏晚，回填会**少**衰减一次 0.85（温和方向，
比 NULL 导致的重复衰减安全）。降级后 last_decayed_at 丢失、last_organized_at 为 NULL 的
窄例外（decay 自建行）不回填——下次整理多吃一个月，同样温和。
"""

from alembic import op

revision = "acme_0003"
down_revision = "acme_0002"
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE memory_setting SET last_decayed_at = last_organized_at "
               "WHERE last_organized_at IS NOT NULL AND last_decayed_at IS NULL")


def downgrade() -> None:
    pass  # 水位列随 acme_0002 的 downgrade 移除，这里无可回退的数据
