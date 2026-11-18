"""额度层的五张表：全局表（identity 同款，不进 RLS_TABLES），显式带 workspace_id 过滤。

设计见 docs/额度层详细设计.md §5。声明模型与迁移（migrations/acme_0004）字段一致，改字段两处同步。
规则三层各带 mode（block 拦截 / warn 只提醒）：生效规则是谁，提示口径就跟谁。
"""

from datetime import date, datetime

from knowwork_server.db.base import Base
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# 提醒口径：block 超额拦这一轮（QuotaExceeded），warn 放行只提醒
BLOCK, WARN = "block", "warn"
MODES = (BLOCK, WARN)
# 额度来自（overview 标注）：个人设置 / 空间默认 / 不限（M2 加部门与用户组）
SOURCE_MEMBER, SOURCE_DEFAULT, SOURCE_NONE = "member", "default", "none"
# 状态（overview 成员行与我的用量）：已用 85% 起「接近上限」，用完「已用完」
NEAR = 0.85
# 组类型（M2 的表，M1 建好不使用）
GROUP_TYPES = ("department", "user_group")
# 变更记录的 target
TARGET_WORKSPACE, TARGET_MEMBER, TARGET_GRANT = "workspace", "member", "grant"


class QuotaWorkspaceSetting(Base):
    """空间的两个数：月总额（全空间所有调用加起来，含访客）与个人默认额度（没设个人额度的成员用它）。
    没有行 = 两项都不限。系统管理员设总额，空间管理员设个人默认与口径。"""

    __tablename__ = "quota_workspace_setting"

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    monthly_total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    member_default_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default=BLOCK, server_default=BLOCK)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class QuotaMemberSetting(Base):
    """成员的个人额度：优先于空间默认。成员移出空间后行保留（再回来还生效）。"""

    __tablename__ = "quota_member_setting"
    __table_args__ = (
        CheckConstraint("monthly_tokens > 0", name="ck_quota_member_positive"),
        Index("ix_quota_member_ws", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("user.id", ondelete="CASCADE"),
                                         primary_key=True)
    monthly_tokens: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(8), default=BLOCK, server_default=BLOCK)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class QuotaGroupSetting(Base):
    """部门/用户组的默认额度（M2 启用）：一次在多个组里取最高，个人设置覆盖组默认。组删除后行保留、
    解析时按组不存在忽略（group_id 不外键，跨模块松耦合）。"""

    __tablename__ = "quota_group_setting"
    __table_args__ = (
        CheckConstraint("group_type in ('department', 'user_group')", name="ck_quota_group_type"),
        CheckConstraint("monthly_tokens > 0", name="ck_quota_group_positive"),
    )

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    monthly_tokens: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(8), default=BLOCK, server_default=BLOCK)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class QuotaGrant(Base):
    """临时加额：叠加在当月有效额度上，月底自动失效（查询只取 month = 当前月）。
    month 是当月 1 号（date），同一个人同月可加多笔（合计生效）。"""

    __tablename__ = "quota_grant"
    __table_args__ = (
        CheckConstraint("extra_tokens > 0", name="ck_quota_grant_positive"),
        Index("ix_quota_grant_lookup", "workspace_id", "month", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # qgr_<uuid hex>
    workspace_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("user.id", ondelete="CASCADE"))
    month: Mapped[date] = mapped_column(Date)
    extra_tokens: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    granted_by: Mapped[str] = mapped_column(String(32))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuotaChangeLog(Base):
    """变更记录：谁在什么时候把谁的额度从多少改成多少（M2 出页面，M1 先落库）。只插不改。"""

    __tablename__ = "quota_change_log"
    __table_args__ = (Index("ix_quota_change_ws", "workspace_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(16))  # workspace | member | grant
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 空间本身为 None
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


QUOTA_TABLES = tuple(t.__table__ for t in (QuotaWorkspaceSetting, QuotaMemberSetting,
                                           QuotaGroupSetting, QuotaGrant, QuotaChangeLog))
