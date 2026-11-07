"""回填衰减水位 last_decayed_at = last_organized_at（R4 审查：回填必须独立成新 revision）。

方向只少不多：把水位回填成 last_organized_at 后，增量衰减只会比理论值**少**。量级要如实说：
对「只入队未跑完整理」的存量行（R2-stamped 环境的常态——提供者每轮入队都写标记，而衰减
从未跑过），该标记可能是最近一次聊天，回填后至下次 30 天边界前增量为 0，**真实丢失的是
「上次真实衰减 → 该标记」之间的全部整月数**（可能好几个月，此后永久不补）。丢的是衰减
不是虚构——行偏活不偏死，用户可删可整理，比 NULL 导致的重复衰减（把行提前归档）安全。
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
