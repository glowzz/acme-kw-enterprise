"""个人记忆的存储与状态机（设计 §2/§3/§8）。

全部走 global_sessionmaker：记忆跟着人走、跨空间，三张表不在 RLS 名单里，照 identity 的表用法。
唯一按空间判权的是空间开关（服务层经 AccessService.check 判 workspace:edit——权限唯一真闸在服务层，
路由只做映射）。删除一律物理删除（用户点了删就是删，隐私预期）；归档/取代是系统降级，可恢复。
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from knowwork_server.access.rls import set_workspace
from knowwork_server.jobs.queue import enqueue
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from .dedup import cosine
from .jobs import EXTRACT, ORGANIZE
from .models import (
    ACTIVE,
    ACTIVE_CAP,
    ACTIVE_CONFIDENCE,
    ARCHIVE_BELOW,
    ARCHIVED,
    BY_TOOL,
    BY_USER,
    CONTENT_MAX,
    DECAY_FACTOR,
    EXTRACTED,
    FACT,
    FOCUS,
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
            "originWorkspaceId": row.origin_workspace_id,
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
            await _acquire_capacity_lock(session, user_id)  # 改内容与整理的补向量互斥（同 remember_update）
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
        """待确认 → 生效中；满额 409。带对手指针的候选（replace 但当时没生效的）确认时沿链
        把仍生效的对手降为 SUPERSEDED 指回本行——互斥在这里闭环；降一升一是容量中性的，
        先做互斥再查容量（与 restore 同口径，满额不假报 409）。"""
        async with self._sessionmaker() as session, session.begin():
            row = await _owned(session, user_id, memory_id)
            if row.status != PENDING:
                raise Conflict("只有待确认的记忆可以确认")
            await _acquire_capacity_lock(session, user_id)
            chain = [row.superseded_by] if row.superseded_by else []
            seen: set[str] = set()
            while chain:
                rival_id = chain.pop()
                if rival_id in seen:
                    continue
                seen.add(rival_id)
                rival = await session.get(Memory, rival_id)
                if rival is None or rival.user_id != user_id:
                    continue
                next_pointer = rival.superseded_by
                if rival.status == ACTIVE:
                    rival.status = SUPERSEDED
                    rival.superseded_by = row.id
                if next_pointer and next_pointer != row.id:
                    chain.append(next_pointer)
            await _require_capacity(session, user_id)
            row.status = ACTIVE
            row.superseded_by = None
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
        if self._access is None or not self._access.check(user_id, workspace_id, "workspace", "use"):
            raise Denied("只有本空间成员可以整理自己的记忆")
        if not (await self.setting(user_id))["enabled"]:
            raise Conflict("个人记忆已关闭，先在设置里打开再整理")
        if (await self.workspace_disabled(workspace_id))["disabled"]:
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
        return {"enabled": row.enabled if row is not None else True,
                "lastOrganizedAt": row.last_organized_at if row is not None else None}

    async def last_organized_on(self, user_id: str) -> str | None:
        """上次整理的日期（本地口径由调用方定）：提供者判断「今天整理过没有」用。"""
        value = (await self.setting(user_id))["lastOrganizedAt"]
        return value.date().isoformat() if value is not None else None

    async def mark_organized(self, user_id: str) -> None:
        """写「整理过」标记：提供者顺手入队成功后写（M3 的整理 handler 完成时也写它）。
        upsert 不动 enabled——标记只管记账。"""
        async with self._sessionmaker() as session, session.begin():
            statement = pg_insert(MemorySetting).values(user_id=user_id, enabled=True,
                                                        last_organized_at=_now())
            await session.execute(statement.on_conflict_do_update(
                index_elements=[MemorySetting.user_id],
                set_={"last_organized_at": statement.excluded.last_organized_at}))

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
        if self._access is None or not self._access.check(actor_id, workspace_id, "workspace", "use"):
            raise Denied("只有本空间成员可以查看本空间的记忆开关")
        return await self.workspace_disabled(workspace_id)

    async def workspace_disabled(self, workspace_id: str) -> dict[str, Any]:
        """开关的原始读取（不判权）：内部链路（organize 的闸、提供者、worker 存量任务复查）复用。
        外部路由一律走 workspace_setting(actor, ws)，判权在那一层。"""
        async with self._sessionmaker() as session:
            row = await session.get(MemoryWorkspaceSetting, workspace_id)
        return {"disabled": row.disabled if row is not None else False}

    async def set_workspace_setting(self, actor_id: str, workspace_id: str, disabled: bool) -> dict[str, Any]:
        """空间管理员停用/启用本空间的记忆。管理员判定在服务层（workspace:edit，owner/admin 过——
        上游默认权限表 rules.py _DEFAULTS：ADMIN 给 workspace:edit、EDITOR 只有 workspace:use，
        成员停不掉整个空间的记忆），判不了宁可 403 不放过。"""
        if self._access is None or not self._access.check(actor_id, workspace_id, "workspace", "edit"):
            raise Denied("只有空间管理员可以停用本空间的记忆")
        async with self._sessionmaker() as session, session.begin():
            statement = pg_insert(MemoryWorkspaceSetting).values(
                workspace_id=workspace_id, disabled=disabled)
            await session.execute(statement.on_conflict_do_update(
                index_elements=[MemoryWorkspaceSetting.workspace_id],
                set_={"disabled": statement.excluded.disabled, "updated_at": func.now()}))
        return {"disabled": disabled}

    # ---- M2：注入与提取链路用的原语 ----

    async def active_snapshot(self, user_id: str) -> list[dict[str, Any]]:
        """生效中的全部记忆（≤200 行），给提供者一次读全：standing 直接用、turn 段算余弦。
        embedding 原样带出（没算过的为 None）。"""
        async with self._sessionmaker() as session:
            rows = await session.scalars(select(Memory).where(Memory.user_id == user_id,
                                                              Memory.status == ACTIVE)
                                          .order_by(Memory.score.desc(), Memory.id))
            return [{"id": row.id, "type": row.type, "content": row.content,
                     "score": row.score, "embedding": row.embedding,
                     "expiresAt": row.expires_at} for row in rows]

    async def dedup_pool(self, user_id: str) -> list[dict[str, Any]]:
        """判重的比对池：生效中 + 待确认的记忆（id/内容/来源/向量）。提取落库前对它算签名、
        否定极性与余弦；归档与已被更新的不参与（它们不会再注入）。source 给判定清单标注
        「手动添加不可改写」（人工优先，§7）。"""
        async with self._sessionmaker() as session:
            rows = await session.scalars(select(Memory).where(
                Memory.user_id == user_id, Memory.status.in_((ACTIVE, PENDING))))
            return [{"id": row.id, "content": row.content, "status": row.status,
                     "source": row.source, "embedding": row.embedding} for row in rows]

    async def track_mentions(self, user_id: str, memory_ids: Sequence[str]) -> None:
        """记账只记 turn 段命中的条目（standing 常驻不算提及——按「在场」记账会让衰减永不触发，
        设计 §4）。fire-and-forget：由提供者放后台任务、吞异常。"""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return
        async with self._sessionmaker() as session, session.begin():
            await session.execute(update(Memory).where(Memory.user_id == user_id, Memory.id.in_(ids))
                                  .values(mention_count=Memory.mention_count + 1,
                                          last_mentioned_at=func.now()))

    async def enqueue_extract(self, user_id: str, workspace_id: str, conversation_id: str, turn: int,
                              question: str, answer: str) -> str:
        """回合结束排提取任务（on_turn_ended 的贡献者调）。subject=conversation_id：同一会话连续
        几轮合并成一条、载荷换最新；载荷自带回答全文（run_summary 不存）。job 表在 RLS 名单里。"""
        async with self._sessionmaker() as session, session.begin():
            await set_workspace(session, workspace_id)
            return await enqueue(session, EXTRACT, workspace_id=workspace_id, subject_id=conversation_id,
                                 payload={"user_id": user_id, "conversation_id": conversation_id,
                                          "turn": turn, "question": question[:2000],
                                          "answer": answer[:6000]})

    async def store_extract(self, user_id: str, workspace_id: str,
                            rows: Sequence[dict[str, Any]]) -> dict[str, int]:
        """提取候选落库（extract.py 已把规则判重、相似度、LLM 判定算完，这里只应用结果）。

        每行：{type, content, quote, confidence, embedding, embedding_model,
               action: "insert"|"update"|"replace", target: 目标记忆 id}。
        - update/replace 的目标必须是本人 ACTIVE 且 source=extracted 的行（人工优先，设计 §7：
          手动添加与「记住」写入的不被系统改写/取代）；目标不可写时退化为普通入库（不丢候选）；
        - replace 只有在新行确定生效时才降旧行——置信被钳、无出处、满额都会让新行落待确认，
          旧 ACTIVE 保持原状，互斥留给 confirm（pending 行暂存对手指针，确认时降对手）；
        - insert/replace：置信 ≥0.8 且有 quote 直接生效，否则待确认；score 记提取置信度（§2）；
        - 满额不报错——降为待确认（M2 无腾位，M3 整理腾位后可手动确认）。
        返回 {active, pending, updated, replaced, dropped} 给 facts 与日志。"""
        now = _now()
        counts = {"active": 0, "pending": 0, "updated": 0, "replaced": 0, "dropped": 0}
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            for item in rows:
                raw_content = item["content"].strip()
                if not 1 <= len(raw_content) <= CONTENT_MAX:
                    counts["dropped"] += 1   # 超长陈述不是好记忆：丢弃本行，不炸整批（R2 前的教训）
                    continue
                confidence = min(max(float(item.get("confidence", 0.0)), 0.0), 1.0)
                action, target = item.get("action", "insert"), item.get("target")
                base = {"type": item["type"], "content": raw_content, "quote": item.get("quote", ""),
                        "origin_workspace_id": workspace_id, "embedding": item.get("embedding"),
                        "embedding_model": item.get("embedding_model")}
                target_row = None
                if target:
                    candidate_target = await session.get(Memory, target)
                    if (candidate_target is not None and candidate_target.user_id == user_id
                            and candidate_target.status == ACTIVE
                            and candidate_target.source == EXTRACTED):
                        target_row = candidate_target
                if action == "update" and target_row is not None:
                    row = target_row
                    row.content = base["content"]
                    row.quote = base["quote"] or row.quote
                    row.type = base["type"]
                    row.embedding = base["embedding"]      # 改写后的内容配候选的向量
                    row.embedding_model = base["embedding_model"]
                    row.score = max(row.score, confidence)
                    # 期限跟最终类型走；已有期限保留（与 edit 同口径）
                    if base["type"] == TASK:
                        if row.expires_at is None:
                            row.expires_at = _now() + timedelta(days=TASK_TTL_DAYS)
                    else:
                        row.expires_at = None
                    row.mention_count += 1
                    row.last_mentioned_at = now
                    counts["updated"] += 1
                    continue
                new_id = f"mem_{uuid.uuid4().hex}"
                confident = confidence >= ACTIVE_CONFIDENCE and bool(base["quote"])
                status = ACTIVE if confident else PENDING
                if status == ACTIVE:
                    active_count = await session.scalar(select(func.count()).select_from(Memory)
                                                        .where(Memory.user_id == user_id,
                                                               Memory.status == ACTIVE))
                    if int(active_count or 0) >= ACTIVE_CAP:
                        status = PENDING  # M2 无腾位：满额先进待确认，M3 整理腾位后可手动确认
                # replace 只在真生效时降旧行；没生效就暂存对手指针，confirm 时再降（不凭空消失）
                superseded_by = target_row.id if (action == "replace" and target_row is not None
                                                  and status == PENDING) else None
                if action == "replace" and target_row is not None and status == ACTIVE:
                    target_row.status = SUPERSEDED
                    target_row.superseded_by = new_id
                    counts["replaced"] += 1
                session.add(Memory(
                    id=new_id, user_id=user_id, status=status, source=EXTRACTED, score=confidence,
                    expires_at=_now() + timedelta(days=TASK_TTL_DAYS) if base["type"] == TASK else None,
                    superseded_by=superseded_by, **base))
                counts["active" if status == ACTIVE else "pending"] += 1
        return counts

    # ---- M3：整理链路的原语（人工优先：合并/降权/腾位只动 source=extracted，设计 §7） ----

    async def archive_expired(self, user_id: str) -> int:
        """步骤 1 过期清理：pending 超 30 天、在办事项期限已过 → archived。
        到期清理对所有来源生效（期限是用户自己声明的语义，R3 定口径）。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            r1 = await session.execute(update(Memory).where(
                Memory.user_id == user_id, Memory.status == PENDING,
                Memory.created_at < _now() - timedelta(days=TASK_TTL_DAYS)).values(status=ARCHIVED))
            r2 = await session.execute(update(Memory).where(
                Memory.user_id == user_id, Memory.type == TASK, Memory.status == ACTIVE,
                Memory.expires_at.isnot(None), Memory.expires_at <= _now()).values(status=ARCHIVED))
            return (r1.rowcount or 0) + (r2.rowcount or 0)

    async def merge_candidates(self, user_id: str, *, limit_pairs: int = 20,
                               threshold: float = 0.90) -> list[dict[str, Any]]:
        """步骤 2 的候选对：extracted 且 ACTIVE、有向量、余弦 ≥0.90 的对子（贪心配对，
        一行至多进一对）。LLM 判定合并与否在这里之外做。"""
        async with self._sessionmaker() as session:
            rows = await session.scalars(select(Memory).where(
                Memory.user_id == user_id, Memory.status == ACTIVE,
                Memory.source == EXTRACTED, Memory.embedding.isnot(None)).order_by(Memory.score.desc()))
            items = [{"id": row.id, "content": row.content, "embedding": row.embedding} for row in rows]
        pairs: list[dict[str, Any]] = []
        used: set[str] = set()
        for i, a in enumerate(items):
            if a["id"] in used:
                continue
            for b in items[i + 1:]:
                if b["id"] in used or cosine(a["embedding"], b["embedding"]) < threshold:
                    continue
                pairs.append({"a_id": a["id"], "a_content": a["content"],
                              "b_id": b["id"], "b_content": b["content"]})
                used.update((a["id"], b["id"]))
                break
            if len(pairs) >= limit_pairs:
                break
        return pairs

    async def apply_merges(self, user_id: str, merges: Sequence[dict[str, Any]]) -> int:
        """步骤 2 落库：keep 行改写为合并表述、drop 行 archived（指回 keep，可恢复可导航）。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            applied = 0
            for merge in merges:
                keep = await session.get(Memory, merge["keep_id"])
                drop = await session.get(Memory, merge["drop_id"])
                if (keep is None or drop is None or keep.user_id != user_id or drop.user_id != user_id
                        or keep.status != ACTIVE or drop.status != ACTIVE
                        or keep.source != EXTRACTED or drop.source != EXTRACTED):
                    continue
                keep.content = _content_of(merge["content"])
                keep.embedding = None  # 合并表述是新内容，旧向量作废，步骤 5 补算
                keep.embedding_model = None
                drop.status = ARCHIVED
                drop.superseded_by = keep.id
                applied += 1
            return applied

    async def decay_scores(self, user_id: str) -> int:
        """步骤 3 降权：fact/focus 且 extracted 的，按整月 gap 每月 ×0.85；score <0.15 → archived。
        preference 稳定不衰减、task 靠期限、手动/工具行不参与。
        衰减是**增量**的（R2 修正）：只应用「上次实际衰减之后」新累积的整月数——score 是上次衰减后
        的值，若每次都对全量 gap 再乘一遍，每天一次的整理会在两周内清空所有一个月没被提及的行。
        水位的读与写收在本事务内（同一把锁）：并发双跑/失败重试都不会重复应用同一增量。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            setting = await session.get(MemorySetting, user_id)
            watermark = setting.last_decayed_at if setting is not None else None
            rows = await session.scalars(select(Memory).where(
                Memory.user_id == user_id, Memory.status == ACTIVE, Memory.source == EXTRACTED,
                Memory.type.in_((FACT, FOCUS))))
            archived = 0
            for row in rows:
                anchor = row.last_mentioned_at or row.created_at
                total_months = (_now() - anchor).days // 30
                applied_months = 0
                if watermark is not None:
                    applied_months = max(0, (watermark - anchor).days // 30)
                incremental = max(0, total_months - applied_months)
                if incremental <= 0:
                    continue
                row.score = row.score * (DECAY_FACTOR ** incremental)
                if row.score < ARCHIVE_BELOW:
                    row.status = ARCHIVED
                    archived += 1
            # 衰减水位在同一事务里推进：排队标记（last_organized_at）≠ 已衰减，两者别合并
            if setting is None:
                session.add(MemorySetting(user_id=user_id, enabled=True, last_decayed_at=_now()))
            else:
                setting.last_decayed_at = _now()
            return archived

    async def trim_active(self, user_id: str, cap: int = ACTIVE_CAP) -> int:
        """步骤 4 腾位：手动/工具行不计入可腾名额——extracted 的生效行数压到
        「cap − 手动生效数」以内，从 score 最低的开始 archived。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            manual = await session.scalar(select(func.count()).select_from(Memory).where(
                Memory.user_id == user_id, Memory.status == ACTIVE,
                Memory.source.in_((BY_USER, BY_TOOL))))
            allowed = max(0, cap - int(manual or 0))
            rows = await session.scalars(select(Memory).where(
                Memory.user_id == user_id, Memory.status == ACTIVE, Memory.source == EXTRACTED)
                .order_by(Memory.score.asc(), Memory.id))
            extracted = list(rows)
            over = len(extracted) - allowed
            archived = 0
            for row in extracted[:max(0, over)]:
                row.status = ARCHIVED
                archived += 1
            return archived

    async def stale_vector_rows(self, user_id: str, alias: str, *,
                                limit: int = 96) -> list[dict[str, Any]]:
        """步骤 5 的补算清单：向量缺失或嵌入模型换了（维度/口径都不齐）的行，全部状态都补。
        带上 content：回写时校验行内容没变（清单读在锁外，期间被改写的行跳过，
        R2 前的 P1 修正——旧向量覆写新内容行且别名相同、永不自愈）。"""
        async with self._sessionmaker() as session:
            rows = await session.scalars(select(Memory).where(
                Memory.user_id == user_id,
                or_(Memory.embedding.is_(None), Memory.embedding_model != alias)).limit(limit))
            return [{"id": row.id, "content": row.content} for row in rows]

    async def store_vectors(self, user_id: str, alias: str,
                            items: Sequence[dict[str, Any]]) -> int:
        """步骤 5 落库：{id, embedding} 列表回写（拿锁，与 remember_update/edit 互斥）；
        行内容与取清单时不一致的跳过——旧向量不覆写新内容。"""
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)
            stored = 0
            for item in items:
                row = await session.get(Memory, item["id"])
                if row is None or row.user_id != user_id or row.content != item["content"]:
                    continue
                row.embedding = item["embedding"]
                row.embedding_model = alias
                stored += 1
            return stored

    # ---- M3：「记住」工具的原语 ----

    async def remember_insert(self, user_id: str, workspace_id: str, content: str, kind: str,
                              embedding: list[float] | None = None,
                              embedding_model: str | None = None) -> dict[str, Any]:
        """工具写入：用户亲口说的，直接生效、source=tool；满额明确报错（工具能向用户解释）。
        embedding 与它的模型别名必须成对写——只写向量不写别名，stale_vector_rows 的
        「别名不一致」条件永远不命中，这行永远进不了补算清单（R2 前的 P2）。"""
        content = _content_of(content)
        async with self._sessionmaker() as session, session.begin():
            await _require_capacity(session, user_id)
            row = Memory(id=f"mem_{uuid.uuid4().hex}", user_id=user_id, type=kind, content=content,
                         source=BY_TOOL, status=ACTIVE, score=1.0,
                         origin_workspace_id=workspace_id, embedding=embedding,
                         embedding_model=embedding_model,
                         expires_at=_now() + timedelta(days=TASK_TTL_DAYS) if kind == TASK else None)
            session.add(row)
            view = _view(row)
        return view

    async def remember_update(self, user_id: str, memory_id: str, content: str, kind: str) -> dict[str, Any]:
        """工具改写近重复的旧说法：内容/类型按用户新表述，向量作废待整理补算，记账 +1。
        期限归属跟最终类型走（与 edit 同一不变式：task 补默认期限，非 task 摘掉）。"""
        content = _content_of(content)
        async with self._sessionmaker() as session, session.begin():
            await _acquire_capacity_lock(session, user_id)  # 与整理的补向量互斥：防旧向量覆写新内容行
            row = await _owned(session, user_id, memory_id)
            if row.status != ACTIVE:
                raise Conflict("只有生效中的记忆可以更新")
            row.content = content
            row.type = kind
            row.embedding = None
            row.embedding_model = None
            if kind == TASK:
                row.expires_at = _now() + timedelta(days=TASK_TTL_DAYS)
            else:
                row.expires_at = None
            row.mention_count += 1
            row.last_mentioned_at = _now()
            view = _view(row)
        return view

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
