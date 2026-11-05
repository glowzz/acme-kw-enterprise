"""个人记忆的存储与状态机（设计 §2/§3/§8）。

全部走 global_sessionmaker：记忆跟着人走、跨空间，三张表不在 RLS 名单里，照 identity 的表用法。
唯一按空间判权的是空间开关（服务层经 AccessService.check 判 workspace:edit——权限唯一真闸在服务层，
路由只做映射）。删除一律物理删除（用户点了删就是删，隐私预期）；归档/取代是系统降级，可恢复。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from knowwork_server.access.rls import set_workspace
from knowwork_server.jobs.queue import enqueue
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from .jobs import ORGANIZE
from .models import (
    ACTIVE,
    ACTIVE_CAP,
    ARCHIVED,
    BY_USER,
    CONTENT_MAX,
    PENDING,
    SUPERSEDED,
    TASK,
    TASK_TTL_DAYS,
    Memory,
    MemorySetting,
    MemoryWorkspaceSetting,
)

log = logging.getLogger(__name__)

_FULL_MESSAGE = f"生效中的记忆已达 {ACTIVE_CAP} 条，先整理或删除几条再添加"


class MemoryError(Exception):
    """业务错误基类：message 直接给用户看。"""


class Invalid(MemoryError):
    """输入不合法（路由映射 422）。"""


class NotFound(MemoryError):
    """记忆不存在或不属于这个用户（404——不区分「没有」与「别人的」，不泄漏 id 存在性）。"""


class Conflict(MemoryError):
    """状态机不允许（如生效满额、重复确认），409。"""


class Denied(MemoryError):
    """不是这个空间的管理员，403。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _view(row: Memory) -> dict[str, Any]:
    """给前端的形态：camelCase，同 web/src/api 的惯例。embedding 不出接口（内部字段）。"""
    return {"id": row.id, "type": row.type, "content": row.content, "quote": row.quote,
            "source": row.source, "status": row.status, "score": row.score,
            "mentionCount": row.mention_count, "lastMentionedAt": row.last_mentioned_at,
            "expiresAt": row.expires_at, "supersededBy": row.superseded_by,
            "createdAt": row.created_at, "updatedAt": row.updated_at}


class MemoryService:
    def __init__(self, sessionmaker: async_sessionmaker, access: Any = None) -> None:
        # global_sessionmaker：不声明空间；enqueue 的事务里显式 set_workspace
        self._sessionmaker = sessionmaker
        self._access = access              # AccessService，只用于空间开关的管理员判定；测试可给假件

    # ---- 列表与统计 ----

    async def page(self, user_id: str, *, status: str, limit: int = 50,
                   offset: int = 0) -> tuple[list[dict], int]:
        async with self._sessionmaker() as session:
            total = await session.scalar(select(func.count()).select_from(Memory)
                                         .where(Memory.user_id == user_id, Memory.status == status))
            rows = await session.scalars(select(Memory)
                                         .where(Memory.user_id == user_id, Memory.status == status)
                                         .order_by(Memory.created_at.desc(), Memory.id)
                                         .offset(offset).limit(limit))
            return [_view(row) for row in rows], int(total or 0)

    async def counts(self, user_id: str) -> dict[str, Any]:
        """页签角标与 200 上限；watch 二期启用，一期恒 0。"""
        async with self._sessionmaker() as session:
            rows = await session.execute(select(Memory.status, func.count())
                                         .where(Memory.user_id == user_id).group_by(Memory.status))
            found = {status: int(count) for status, count in rows.all()}
        return {"active": found.get(ACTIVE, 0), "pending": found.get(PENDING, 0),
                "superseded": found.get(SUPERSEDED, 0), "archived": found.get(ARCHIVED, 0),
                "cap": ACTIVE_CAP}

    # ---- 增删改（M1 的来源都是本人：手动添加） ----

    async def add(self, user_id: str, *, type: str, content: str) -> dict[str, Any]:
        """手动添加：本人添加本人负责，直接生效。在办事项给默认期限，编辑里可改。
        个人开关关闭时后端也拦（设计 §9：关闭后助手不再读取或新增）。"""
        if not (await self.setting(user_id))["enabled"]:
            raise Conflict("个人记忆已关闭，先在设置里打开再添加")
        content = _content_of(content)
        async with self._sessionmaker() as session, session.begin():
            await _require_capacity(session, user_id)
            row = Memory(id=f"mem_{uuid.uuid4().hex}", user_id=user_id, type=type, content=content,
                         source=BY_USER, status=ACTIVE,
                         expires_at=_now() + timedelta(days=TASK_TTL_DAYS) if type == TASK else None)
            session.add(row)
            view = _view(row)   # 事务块内取视图：不依赖 sessionmaker 的 expire_on_commit 配置
        return view

    async def edit(self, user_id: str, memory_id: str, *, content: str | None = None,
                   type: str | None = None, expires_at: datetime | None = None,
                   clear_expires: bool = False) -> dict[str, Any]:
        if expires_at is not None and expires_at.tzinfo is None:
            raise Invalid("期限要带时区（例如 2026-12-01T00:00:00Z）")
        async with self._sessionmaker() as session, session.begin():
            row = await _owned(session, user_id, memory_id)
            if content is not None:
                row.content = _content_of(content)
                row.embedding = None      # 内容变了，旧向量不再代表它；M3 的整理任务懒重算补齐
                row.embedding_model = None
            if type is not None and type != row.type:
                row.type = type
                if type == TASK and row.expires_at is None:
                    row.expires_at = _now() + timedelta(days=TASK_TTL_DAYS)
            # 期限的归属跟着最终类型走：非在办一律摘掉（同时传 type=fact 和 expiresAt 的请求也不留期限）
            if row.type != TASK:
                row.expires_at = None
            elif clear_expires:
                row.expires_at = None
            elif expires_at is not None:
                row.expires_at = expires_at
            view = _view(row)
        return view

    async def delete(self, user_id: str, memory_id: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.delete(await _owned(session, user_id, memory_id))

    # ---- 状态机（设计 §3） ----

    async def confirm(self, user_id: str, memory_id: str) -> dict[str, Any]:
        """待确认 → 生效中；满额 409。"""
        async with self._sessionmaker() as session, session.begin():
            row = await _owned(session, user_id, memory_id)
            if row.status != PENDING:
                raise Conflict("只有待确认的记忆可以确认")
            await _require_capacity(session, user_id)
            row.status = ACTIVE
            view = _view(row)
        return view

    async def dismiss(self, user_id: str, memory_id: str) -> None:
        """待确认忽略 = 物理删除。"""
        async with self._sessionmaker() as session, session.begin():
            row = await _owned(session, user_id, memory_id)
            if row.status != PENDING:
                raise Conflict("只有待确认的记忆可以忽略")
            await session.delete(row)

    async def restore(self, user_id: str, memory_id: str) -> dict[str, Any]:
        """已归档 / 已被更新 → 生效中；满额 409（提示先删几条）。

        连带语义（R3-R6 审查逐步补齐）：
        - 恢复是否认它的全部后继：沿取代链（superseded_by 正向）把仍然生效的后继降为
          SUPERSEDED 并把指针指回本行。**指针回写是往返互斥的关键**——后继将来恢复时靠
          它找到本行，没有它，恢复后继会把矛盾的一对同时复活；顺链遍历保证多跳链
          （后继自身又被更新的那种）整条收敛。先存原指针再覆写是防御护栏：当前不变式下
          生效行的指针恒空，两种写法等价，但链上语义不应依赖这个巧合。
        - 整个恢复（本行状态闸、链种子、后继 RMW、写入）都在同一把 advisory 事务锁内：
          并发的两个恢复不会把同一个后继各自降级、互相覆盖指针（那会永久断链）；
          M3 起 organize 也会写 status/superseded_by，锁外读会看到脏形状（R6 收窄）。
        - 链处理是容量中性的（降级若干、激活一条），所以先做互斥再查容量，不会误报满额；
        - 过期归档的在办事项恢复 = 再给一个默认期限窗口，不然一恢复下一次整理又把它归档。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)  # 锁盖全程：状态闸、链种子、后继 RMW、写入
            row = await _owned(session, user_id, memory_id)
            if row.status not in (ARCHIVED, SUPERSEDED):
                raise Conflict("只有已归档或已被更新的记忆可以恢复")
            chain = [row.superseded_by] if row.superseded_by else []
            seen: set[str] = set()
            while chain:
                successor_id = chain.pop()
                if successor_id in seen:
                    continue
                seen.add(successor_id)
                successor = await session.get(Memory, successor_id)
                if successor is None or successor.user_id != user_id:
                    continue
                next_pointer = successor.superseded_by  # 先取原指针再覆写（R5 的防御护栏，见 docstring）
                if successor.status == ACTIVE:
                    successor.status = SUPERSEDED  # 被「恢复的旧版」取代——§3 的恢复转移（R6 补进设计表）
                    successor.superseded_by = row.id
                if next_pointer and next_pointer != row.id:
                    chain.append(next_pointer)
            await _require_capacity(session, user_id)
            if row.type == TASK and row.expires_at is not None and row.expires_at <= _now():
                row.expires_at = _now() + timedelta(days=TASK_TTL_DAYS)
            row.status = ACTIVE
            row.superseded_by = None  # 本行现役；被它压下去的后继各自指着它，链没断
            view = _view(row)
        return view

    # ---- 整理（M1 只入队；handler 在 M3 注册） ----

    async def organize(self, user_id: str, workspace_id: str) -> str:
        """排一条整理任务，返回任务 id。workspace_id 只为任务里的模型路由（按空间查模型目录）；
        数据不按空间筛。同一空间同一人排队中的任务会合并，连点无副作用。

        入队前过三道闸：本人得是这个空间的成员（写它的 job 表）、个人开关开着、空间开关开着——
        判权在服务层（workspace:use 即成员，owner/admin/editor 都含），判不了宁可 403。"""
        if self._access is None or not await self._access.check(user_id, workspace_id, "workspace", "use"):
            raise Denied("只有本空间成员可以整理自己的记忆")
        if not (await self.setting(user_id))["enabled"]:
            raise Conflict("个人记忆已关闭，先在设置里打开再整理")
        if (await self._workspace_disabled(workspace_id))["disabled"]:
            raise Conflict("本空间已停用记忆，整理暂不执行")
        async with self._sessionmaker() as session, session.begin():
            await set_workspace(session, workspace_id)  # job 表在 RLS 名单里
            job_id = await enqueue(session, ORGANIZE, workspace_id=workspace_id, subject_id=user_id,
                                   payload={"user_id": user_id, "workspace_id": workspace_id})
        return job_id

    # ---- 开关（个人 + 空间） ----

    async def setting(self, user_id: str) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            row = await session.get(MemorySetting, user_id)
        return {"enabled": row.enabled if row is not None else True}

    async def set_setting(self, user_id: str, enabled: bool) -> dict[str, Any]:
        """关闭只停链路（注入给空、提取停、工具不给），数据保留——重新打开接着用（设计 §1）。
        upsert 写法：两个并发的「首次保存」都查不到行时，普通的 get+insert 会撞主键 500。"""
        async with self._sessionmaker() as session, session.begin():
            statement = pg_insert(MemorySetting).values(user_id=user_id, enabled=enabled)
            await session.execute(statement.on_conflict_do_update(
                index_elements=[MemorySetting.user_id],
                # 裸 upsert 不走 ORM 的 onupdate，时间戳要显式带上
                set_={"enabled": statement.excluded.enabled, "updated_at": func.now()}))
        return {"enabled": enabled}

    async def workspace_setting(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        """空间开关的读取：成员可读（页面提示「本空间已停用」用），非成员 403——
        不给外人探测任意空间开关状态的口子。"""
        if self._access is None or not await self._access.check(actor_id, workspace_id, "workspace", "use"):
            raise Denied("只有本空间成员可以查看本空间的记忆开关")
        return await self._workspace_disabled(workspace_id)

    async def _workspace_disabled(self, workspace_id: str) -> dict[str, Any]:
        """开关的原始读取（不判权）：内部链路（organize 的闸、M2 的提供者）复用。"""
        async with self._sessionmaker() as session:
            row = await session.get(MemoryWorkspaceSetting, workspace_id)
        return {"disabled": row.disabled if row is not None else False}

    async def set_workspace_setting(self, actor_id: str, workspace_id: str, disabled: bool) -> dict[str, Any]:
        """空间管理员停用/启用本空间的记忆。管理员判定在服务层（workspace:edit，owner/admin 过——
        上游默认权限表 rules.py _DEFAULTS：ADMIN 给 workspace:edit、EDITOR 只有 workspace:use，
        成员停不掉整个空间的记忆），判不了宁可 403 不放过。"""
        if self._access is None or not await self._access.check(actor_id, workspace_id, "workspace", "edit"):
            raise Denied("只有空间管理员可以停用本空间的记忆")
        async with self._sessionmaker() as session, session.begin():
            statement = pg_insert(MemoryWorkspaceSetting).values(
                workspace_id=workspace_id, disabled=disabled)
            await session.execute(statement.on_conflict_do_update(
                index_elements=[MemoryWorkspaceSetting.workspace_id],
                set_={"disabled": statement.excluded.disabled, "updated_at": func.now()}))
        return {"disabled": disabled}

    # ---- 导出与清空 ----

    async def export(self, user_id: str) -> list[dict[str, Any]]:
        async with self._sessionmaker() as session:
            rows = await session.scalars(select(Memory).where(Memory.user_id == user_id)
                                         .order_by(Memory.created_at.desc(), Memory.id))
            return [_view(row) for row in rows]

    async def clear(self, user_id: str) -> int:
        """物理清空本人全部记忆（含各状态）与个人开关。返回删掉的条数给审计与提示用。"""
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(delete(Memory).where(Memory.user_id == user_id))
            await session.execute(delete(MemorySetting).where(MemorySetting.user_id == user_id))
        return result.rowcount


# ---- 内部工具 ----

def _content_of(content: str) -> str:
    """去掉首尾空白后的长度校验：空串、超 500 字都是输入不合法（422）。"""
    trimmed = content.strip()
    if not 1 <= len(trimmed) <= CONTENT_MAX:
        raise Invalid(f"记忆内容要是一句 1~{CONTENT_MAX} 字的陈述")
    return trimmed


async def _owned(session, user_id: str, memory_id: str) -> Memory:
    row = await session.get(Memory, memory_id)
    if row is None or row.user_id != user_id:
        raise NotFound("没有这条记忆")
    return row


async def _acquire_capacity_lock(session, user_id: str) -> None:
    """同一用户的 advisory 事务锁：恢复的全程（状态闸/链遍历/写入）与满额「查+写」共用一把，
    同一用户的并发操作在此串行化，事务结束自动释放（对同会话同 key 可重入，重复取锁不阻塞）。"""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:uid))"), {"uid": user_id})


async def _require_capacity(session, user_id: str) -> None:
    """生效满额判定：锁后 count——普通 SELECT count 不加锁，READ COMMITTED 下两个并发事务能
    各自数到 199 再各插一条，把 active 顶过 200（设计 §8 的承诺靠锁兑现）。"""
    await _acquire_capacity_lock(session, user_id)
    active = await session.scalar(select(func.count()).select_from(Memory)
                                  .where(Memory.user_id == user_id, Memory.status == ACTIVE))
    if int(active or 0) >= ACTIVE_CAP:
        raise Conflict(_FULL_MESSAGE)
