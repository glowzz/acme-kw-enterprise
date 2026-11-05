"""记忆层的三张表：个人的、跨空间的（identity 同款：全局表，不进 RLS_TABLES）。

设计见 docs/记忆层详细设计.md §2（对照上游 v0.0.4）。建表走扩展自己的迁移分支
（memory_migrations/acme_0001）；这里只声明模型，迁移与模型要保持字段一致。
"""

from datetime import datetime

from knowwork_server.db.base import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# 记忆类型：偏好（回答格式/语言/详略）、长期关注、事实、在办事项（带期限）
PREFERENCE, FOCUS, FACT, TASK = "preference", "focus", "fact", "task"
TYPES = (PREFERENCE, FOCUS, FACT, TASK)
# 来源：系统提取 / 手动添加 / 会话里「记住」写入（整理只动 extracted，人工优先）
EXTRACTED, BY_USER, BY_TOOL = "extracted", "user", "tool"
SOURCES = (EXTRACTED, BY_USER, BY_TOOL)
# 状态机（设计 §3）：生效中会被注入；待确认等用户处理；watch 二期启用；superseded 是被新记忆
# 取代的旧版本（superseded_by 留审计链）；archived 是到期/降权/腾位的归档，可恢复
ACTIVE, PENDING, WATCH, SUPERSEDED, ARCHIVED = "active", "pending", "watch", "superseded", "archived"
ACTIVE_CAP = 200    # 生效中的条数上限：注入预算可控（设计 §1）
CONTENT_MAX = 500   # 一句第三人称陈述，页面与迁移的长度约束同一口径
QUOTE_MAX = 200     # 提取时逐字摘录的用户原话上限
TASK_TTL_DAYS = 30  # 在办事项的默认期限（天）：期限不让提取模型猜日期，编辑里可改（设计 §5）


class Memory(Base):
    __tablename__ = "memory"
    __table_args__ = (
        CheckConstraint("type in ('preference', 'focus', 'fact', 'task')", name="ck_memory_type"),
        CheckConstraint("source in ('extracted', 'user', 'tool')", name="ck_memory_source"),
        CheckConstraint("status in ('active', 'pending', 'watch', 'superseded', 'archived')",
                        name="ck_memory_status"),
        CheckConstraint(f"char_length(content) between 1 and {CONTENT_MAX}", name="ck_memory_content_len"),
        Index("ix_memory_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # mem_<uuid hex>，照 job_<hex> 的命名办法
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # 提取时的用户原话逐字摘录（应用层截断到 QUOTE_MAX）；手动添加/工具写入为空。
    # 待确认页展开可见出处，判断「确认/忽略」用（借鉴 octop-memory 的证据链）
    quote: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 在哪个空间提取的：只做排查与重嵌向量时的模型路由参考，不判权（记忆跨空间）
    origin_workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # default 与 server_default 并写：ORM 插入走 default，迁移建出的表对裸 SQL 也有 DEFAULT，两条路径同构
    source: Mapped[str] = mapped_column(String(16), default=EXTRACTED, server_default=EXTRACTED)
    status: Mapped[str] = mapped_column(String(16), default=PENDING, server_default=PENDING)
    # 提取置信度起步，整理时衰减（设计 §7）
    score: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    # 写入时算（best-effort），失败留空；每用户 ≤200 行，进程内算余弦，不引 pgvector。
    # 换嵌入模型后维度可能不齐：不齐的跳过相似度，整理任务懒重算补齐
    embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 记账只记 turn 段命中（这一轮的提问真的用上了）；standing 常驻不算提及——按「在场」记账
    # 会让 last_mentioned_at 永远是今天，衰减永不触发（设计 §4）
    mention_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_mentioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 在办事项到期归档；被哪条新记忆取代（不设 FK：简化恢复语义）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # onupdate 由 ORM 在 UPDATE 语句里带上：编辑/整理动过之后前端看得到新时间，整理的衰减也按它算
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class MemorySetting(Base):
    """个人记忆开关；没有行 = 默认开启。last_organized_at 兼作「今天整理过没有」的标记（M3 用）。"""

    __tablename__ = "memory_setting"

    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("user.id", ondelete="CASCADE"),
                                         primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_organized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class MemoryWorkspaceSetting(Base):
    """空间级开关（企业合规口，设计 §1）：默认启用；空间管理员停用后，本空间的注入、提取、
    记住工具全停，个人数据保留、随时恢复。全局表（判空间管理员经 AccessService，不进 RLS）。
    空间关闭后残留行无害（只有一个布尔，无个人数据），不随空间级联清理。"""

    __tablename__ = "memory_workspace_setting"

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
