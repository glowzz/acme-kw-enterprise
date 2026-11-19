"""额度层的服务：解析规则、开始一轮前的判定、管理操作与变更记录。

判定经 ctx.quota.set 登记（KnowWork 的 conversations/quota.py：各端开始一轮之前调，超了抛
QuotaExceeded）。口径见 docs/额度层详细设计.md §2–§4：

- 本月已用与统计页同源（ctx.usage 的 summary，usage_daily 按部署时区切天）；
- kind=person 走「个人额度 ?? 空间默认 ?? 不限」，channel/external 只判空间总额；
- 临时加额叠加在当月有效额度上，按月行失效；
- 判定内部出错放行并记日志——额度是运营手段，不能因为查库失败挡住所有人。
"""

import logging
import statistics
import uuid
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from knowwork_server.access.rls import set_workspace
from knowwork_server.conversations.quota import QuotaExceeded, TurnStart
from knowwork_server.identity.models import User
from knowwork_server.models.recorder import GroupBy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import (
    BLOCK,
    GROUP_TYPES,
    NEAR,
    SOURCE_DEFAULT,
    SOURCE_GROUP,
    SOURCE_MEMBER,
    SOURCE_NONE,
    TARGET_GRANT,
    TARGET_GROUP,
    TARGET_MEMBER,
    TARGET_WORKSPACE,
    WARN,
    QuotaChangeLog,
    QuotaGrant,
    QuotaGroupSetting,
    QuotaMemberSetting,
    QuotaWorkspaceSetting,
)

log = logging.getLogger(__name__)

USER_PERSON = "person"


class Denied(Exception):
    """只有空间管理员/系统管理员能改额度。"""


class Invalid(Exception):
    """输入不合法（额度必须为正、月份格式不对等）。"""


def month_range(today: date) -> tuple[date, date]:
    """本月 [1 号, 下月 1 号)：聚合与「下次恢复」都用它。"""
    start = today.replace(day=1)
    next_month = (start + timedelta(days=45)).replace(day=1)
    return start, next_month


class QuotaService:
    def __init__(self, sessionmaker: async_sessionmaker, access: Any, usage: Any,
                 today: Callable[[], date] = date.today) -> None:
        self._sessionmaker = sessionmaker
        self._access = access          # 判系统管理员（is_system_admin）与空间管理员（check workspace:edit）
        self._usage = usage            # ctx.usage：UsageRecorder 的 summary（本月已用与统计页同源）
        self._today = today            # 可注入：测试钉住「今天」验月界口径

    # ---- 解析（个人有效额度与来源；M1 无组层）----

    async def _workspace_row(self, workspace_id: str) -> QuotaWorkspaceSetting | None:
        async with self._sessionmaker() as session:
            return await session.get(QuotaWorkspaceSetting, workspace_id)

    async def _member_row(self, workspace_id: str, user_id: str) -> QuotaMemberSetting | None:
        async with self._sessionmaker() as session:
            return await session.get(QuotaMemberSetting, (workspace_id, user_id))

    async def _grants_sum(self, workspace_id: str, user_id: str, month: date) -> int:
        async with self._sessionmaker() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(QuotaGrant.extra_tokens), 0))
                .where(QuotaGrant.workspace_id == workspace_id,
                       QuotaGrant.user_id == user_id, QuotaGrant.month == month))
            return int(value)

    async def _group_default(self, workspace_id: str,
                             user_id: str) -> tuple[int | None, str | None, str | None]:
        # 返回 (最高的一档 token 数, 组名, 口径)；没有命中的组就是 (None, None, None)
        """这个人所在的部门/用户组里，默认额度最高的那一档（多个组同时命中取最高）；组不存在忽略。
        department_member / user_group_member 在 RLS 名单里，事务里声明空间。"""
        from knowwork_server.access.models import (
            Department,
            DepartmentMember,
            UserGroup,
            UserGroupMember,
        )

        async with self._sessionmaker() as session:
            await set_workspace(session, workspace_id)
            dept_rows = (await session.execute(
                select(QuotaGroupSetting, Department.name)
                .join(Department, Department.id == QuotaGroupSetting.group_id)
                .join(DepartmentMember, DepartmentMember.department_id == Department.id)
                .where(QuotaGroupSetting.workspace_id == workspace_id,
                       QuotaGroupSetting.group_type == "department",
                       DepartmentMember.workspace_id == workspace_id,
                       DepartmentMember.user_id == user_id))).all()
            group_rows = (await session.execute(
                select(QuotaGroupSetting, UserGroup.name)
                .join(UserGroup, UserGroup.id == QuotaGroupSetting.group_id)
                .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
                .where(QuotaGroupSetting.workspace_id == workspace_id,
                       QuotaGroupSetting.group_type == "user_group",
                       UserGroupMember.workspace_id == workspace_id,
                       UserGroupMember.user_id == user_id))).all()
        candidates = [(row.monthly_tokens, name, row.mode) for row, name in (*dept_rows, *group_rows)]
        if not candidates:
            return None, None, None
        best = max(candidates, key=lambda item: item[0])
        return best[0], best[1], best[2]

    async def _user_kind(self, user_id: str) -> str:
        async with self._sessionmaker() as session:
            row = await session.get(User, user_id)
            return row.kind if row is not None else USER_PERSON

    async def resolve(self, workspace_id: str, user_id: str, *, kind: str | None = None) -> dict[str, Any]:
        """这个人在这个空间本月个人层的额度：tokens None = 不限；带 source 与 mode。空间总额另判一层。"""
        if kind is None:
            kind = await self._user_kind(user_id)
        if kind != USER_PERSON:  # 渠道账号与嵌入的外部用户只判空间总额，没有个人层
            return {"tokens": None, "source": SOURCE_NONE, "sourceName": None,
                    "mode": BLOCK, "grants": 0, "kind": kind}
        member = await self._member_row(workspace_id, user_id)
        setting = await self._workspace_row(workspace_id)
        if member is not None:
            tokens, source, mode, source_name = member.monthly_tokens, SOURCE_MEMBER, member.mode, None
        else:
            group_tokens, group_name, group_mode = await self._group_default(workspace_id, user_id)
            if group_tokens is not None:
                tokens, source, mode, source_name = group_tokens, SOURCE_GROUP, group_mode, group_name
            elif setting is not None and setting.member_default_tokens is not None:
                tokens, source, mode, source_name = (setting.member_default_tokens,
                                                     SOURCE_DEFAULT, setting.mode, None)
            else:
                tokens, source, mode, source_name = None, SOURCE_NONE, BLOCK, None
        grants = await self._grants_sum(workspace_id, user_id, self._today().replace(day=1)) \
            if tokens is not None else 0
        return {"tokens": tokens, "source": source, "sourceName": source_name,
                "mode": mode, "grants": grants, "kind": kind}

    async def monthly_used(self, workspace_id: str, user_id: str) -> int:
        """本月已用（usage_daily，与统计页「按成员」同一条聚合）。"""
        start, _ = month_range(self._today())
        summaries = await self._usage.summary(workspace_id, start, self._today(), GroupBy.USER,
                                              user_id=user_id)
        return int(sum(item.total_tokens for item in summaries))

    async def workspace_used(self, workspace_id: str) -> int:
        start, _ = month_range(self._today())
        summaries = await self._usage.summary(workspace_id, start, self._today(), GroupBy.USER)
        return int(sum(item.total_tokens for item in summaries))

    # ---- 判定（quota.set 的钩子）----

    async def check(self, turn: TurnStart) -> None:
        """开始一轮前的判定：超了抛 QuotaExceeded（文案给用户看、scope 分个人/空间、下月 1 日恢复）。"""
        try:
            await self._check(turn)
        except QuotaExceeded:
            raise
        except Exception:  # 判定链路自身出错：放行并记日志，别挡住所有人
            log.exception("quota check failed for %s in %s，放行这一轮",
                          turn.user_id, turn.workspace_id)

    async def _check(self, turn: TurnStart) -> None:
        workspace_id, user_id = turn.workspace_id, turn.user_id
        kind = await self._user_kind(user_id)
        setting = await self._workspace_row(workspace_id)
        start, next_month = month_range(self._today())

        # 空间总额一层：对所有调用者（含访客与成员）。超了按空间的口径。
        if setting is not None and setting.monthly_total_tokens is not None:
            total_used = await self.workspace_used(workspace_id)
            if total_used >= setting.monthly_total_tokens:
                self._exceed(setting.mode, "workspace", total_used, setting.monthly_total_tokens,
                             next_month, "空间")

        # 个人一层：只对真人（channel/external 只判空间总额，对齐官方口径）。
        if kind == USER_PERSON:
            personal = await self.resolve(workspace_id, user_id, kind=kind)
            if personal["tokens"] is not None:
                effective = personal["tokens"] + personal["grants"]
                used = await self.monthly_used(workspace_id, user_id)
                if used >= effective:
                    self._exceed(personal["mode"], "personal", used, effective, next_month, "个人")

    @staticmethod
    def _exceed(mode: str, scope: str, used: int, tokens: int, resets_on: date, label: str) -> None:
        if mode != WARN:  # 只提醒不拦截：放行，提醒由前端按状态出
            raise QuotaExceeded(
                f"{label}额度已用完（本月已用 {used:,} / {tokens:,} token），每月 1 日恢复；"
                f"可在「设置 → 我的用量」查看，或找空间管理员临时加额",
                scope=scope, resets_on=resets_on)

    # ---- 状态（overview 与「我的用量」共用）----

    @staticmethod
    def _status(used: int, tokens: int | None) -> str | None:
        """ok | near(≥85%) | exhausted；不限（tokens None）没有状态。"""
        if tokens is None or tokens <= 0:
            return None
        if used >= tokens:
            return "exhausted"
        if used >= tokens * NEAR:
            return "near"
        return "ok"

    async def member_state(self, workspace_id: str, user_id: str, *,
                           kind: str | None = None) -> dict[str, Any]:
        """一个人的本月额度状态：解析 + 已用 + 生效值 + 状态。overview、me、status 接口共用。"""
        resolved = await self.resolve(workspace_id, user_id, kind=kind)
        used = await self.monthly_used(workspace_id, user_id)
        effective = None if resolved["tokens"] is None else resolved["tokens"] + resolved["grants"]
        return {"workspaceId": workspace_id, "userId": user_id, "used": used,
                "tokens": resolved["tokens"], "source": resolved["source"], "mode": resolved["mode"],
                "grants": resolved["grants"], "effective": effective,
                "status": self._status(used, effective),
                "resetsOn": month_range(self._today())[1].isoformat()}

    async def overview(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        """额度页的数据：设置（四卡）+ 成员列表（按成员聚合本月）。空间管理员可读。"""
        await self._require_space_admin(actor_id, workspace_id)
        setting = await self._workspace_row(workspace_id)
        members = await self._members(workspace_id)
        rows = []
        for member in members:
            resolved = await self.resolve(workspace_id, member["id"], kind=member["kind"])
            used = await self.monthly_used(workspace_id, member["id"])
            effective = None if resolved["tokens"] is None else resolved["tokens"] + resolved["grants"]
            rows.append({**member, "used": used, "tokens": resolved["tokens"],
                         "source": resolved["source"], "grants": resolved["grants"],
                         "effective": effective, "mode": resolved["mode"],
                         "status": self._status(used, effective)})
        rows.sort(key=lambda row: (-row["used"], row["name"]))
        _, next_month = month_range(self._today())
        return {"workspaceId": workspace_id,
                "monthlyTotalTokens": setting.monthly_total_tokens if setting else None,
                "memberDefaultTokens": setting.member_default_tokens if setting else None,
                "mode": setting.mode if setting else BLOCK,
                "workspaceUsed": await self.workspace_used(workspace_id),
                "resetsOn": next_month.isoformat(),
                "members": rows}

    async def _members(self, workspace_id: str) -> list[dict[str, Any]]:
        """空间成员（user 表 join，带 kind 与角色）。workspace_member 在 RLS 名单里，事务里声明空间。"""
        from knowwork_server.access.models import WorkspaceMember  # 延迟导入，避免装配期环

        async with self._sessionmaker() as session:
            await set_workspace(session, workspace_id)
            rows = (await session.execute(
                select(User.id, User.name, User.email, User.kind, WorkspaceMember.role)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .where(WorkspaceMember.workspace_id == workspace_id)
                .order_by(User.name))).all()
        return [{"id": row.id, "name": row.name, "email": row.email, "kind": row.kind,
                 "role": row.role} for row in rows]

    # ---- 管理操作（全部写变更记录）----

    async def workspace_setting_read(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        """额度页四卡的设置（空间管理员可读；没有行 = 两项都不限）。"""
        await self._require_space_admin(actor_id, workspace_id)
        setting = await self._workspace_row(workspace_id)
        return {"workspaceId": workspace_id,
                "monthlyTotalTokens": setting.monthly_total_tokens if setting else None,
                "memberDefaultTokens": setting.member_default_tokens if setting else None,
                "mode": setting.mode if setting else BLOCK,
                "updatedBy": setting.updated_by if setting else None,
                "updatedAt": setting.updated_at if setting else None}

    async def _require_space_admin(self, actor_id: str, workspace_id: str) -> None:
        if self._access is None or not self._access.check(actor_id, workspace_id, "workspace", "edit"):
            raise Denied("只有空间管理员可以改本空间的额度")

    def _require_system_admin(self, actor_id: str) -> None:
        if self._access is None or not self._access.is_system_admin(actor_id):
            raise Denied("只有系统管理员可以设置空间月总额")

    async def _log(self, session, workspace_id: str, target: str, target_id: str | None,
                   before: dict | None, after: dict | None, changed_by: str) -> None:
        session.add(QuotaChangeLog(workspace_id=workspace_id, target=target, target_id=target_id,
                                   before=before, after=after, changed_by=changed_by))

    async def set_workspace_setting(self, actor_id: str, workspace_id: str, *,
                                    monthly_total_tokens: int | None,
                                    member_default_tokens: int | None, mode: str) -> dict[str, Any]:
        """空间的两项设置。总额要系统管理员；个人默认与口径空间管理员。都是 None/不传即不限。"""
        if monthly_total_tokens is not None:
            self._require_system_admin(actor_id)
        await self._require_space_admin(actor_id, workspace_id)
        if monthly_total_tokens is not None and monthly_total_tokens <= 0:
            raise Invalid("空间月总额必须为正")
        if member_default_tokens is not None and member_default_tokens <= 0:
            raise Invalid("个人默认额度必须为正")
        if mode not in (BLOCK, WARN):
            raise Invalid(f"口径只能是 {BLOCK} 或 {WARN}")
        before_row = await self._workspace_row(workspace_id)
        before = None if before_row is None else {
            "monthly_total_tokens": before_row.monthly_total_tokens,
            "member_default_tokens": before_row.member_default_tokens, "mode": before_row.mode}
        after = {"monthly_total_tokens": monthly_total_tokens,
                 "member_default_tokens": member_default_tokens, "mode": mode}
        async with self._sessionmaker() as session, session.begin():
            statement = select(QuotaWorkspaceSetting).where(
                QuotaWorkspaceSetting.workspace_id == workspace_id)
            row = (await session.scalars(statement)).first()
            if row is None:
                session.add(QuotaWorkspaceSetting(workspace_id=workspace_id, **after, updated_by=actor_id))
            else:
                row.monthly_total_tokens = monthly_total_tokens
                row.member_default_tokens = member_default_tokens
                row.mode = mode
                row.updated_by = actor_id
            await self._log(session, workspace_id, TARGET_WORKSPACE, None, before, after, actor_id)
        return after

    async def set_member_setting(self, actor_id: str, workspace_id: str, user_id: str,
                                 monthly_tokens: int | None, mode: str) -> dict[str, Any]:
        """个人额度：设一个正数；None = 删除（回到空间默认）。"""
        await self._require_space_admin(actor_id, workspace_id)
        if monthly_tokens is not None and monthly_tokens <= 0:
            raise Invalid("个人额度必须为正")
        if mode not in (BLOCK, WARN):
            raise Invalid(f"口径只能是 {BLOCK} 或 {WARN}")
        before_row = await self._member_row(workspace_id, user_id)
        before = None if before_row is None else {
            "monthly_tokens": before_row.monthly_tokens, "mode": before_row.mode}
        async with self._sessionmaker() as session, session.begin():
            if monthly_tokens is None:
                row = await session.get(QuotaMemberSetting, (workspace_id, user_id))
                if row is not None:
                    await session.delete(row)
                after = None
            else:
                statement = select(QuotaMemberSetting).where(
                    QuotaMemberSetting.workspace_id == workspace_id,
                    QuotaMemberSetting.user_id == user_id)
                row = (await session.scalars(statement)).first()
                after = {"monthly_tokens": monthly_tokens, "mode": mode}
                if row is None:
                    session.add(QuotaMemberSetting(workspace_id=workspace_id, user_id=user_id,
                                                   monthly_tokens=monthly_tokens, mode=mode,
                                                   updated_by=actor_id))
                else:
                    row.monthly_tokens = monthly_tokens
                    row.mode = mode
                    row.updated_by = actor_id
            await self._log(session, workspace_id, TARGET_MEMBER, user_id, before, after, actor_id)
        return await self.member_state(workspace_id, user_id)

    async def add_grant(self, actor_id: str, workspace_id: str, user_id: str, extra_tokens: int,
                        reason: str = "") -> dict[str, Any]:
        """临时加额一笔：叠在当月有效额度上，月底（自然月）自动失效。"""
        await self._require_space_admin(actor_id, workspace_id)
        if extra_tokens <= 0:
            raise Invalid("加额必须为正")
        if len(reason) > 200:
            raise Invalid("说明最长 200 字")
        grant_id = f"qgr_{uuid.uuid4().hex}"
        month = self._today().replace(day=1)
        async with self._sessionmaker() as session, session.begin():
            session.add(QuotaGrant(id=grant_id, workspace_id=workspace_id, user_id=user_id,
                                   month=month, extra_tokens=extra_tokens, reason=reason,
                                   granted_by=actor_id))
            await self._log(session, workspace_id, TARGET_GRANT, user_id, None,
                            {"month": month.isoformat(), "extraTokens": extra_tokens, "reason": reason},
                            actor_id)
        state = await self.member_state(workspace_id, user_id)
        return {"grantId": grant_id, **state}

    async def grants(self, actor_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """本空间的当月临时加额（列表页用；历史月份 M2 出页面再放月份参数）。"""
        await self._require_space_admin(actor_id, workspace_id)
        month = self._today().replace(day=1)
        async with self._sessionmaker() as session:
            rows = (await session.execute(
                select(QuotaGrant, User.name).join(User, User.id == QuotaGrant.user_id)
                .where(QuotaGrant.workspace_id == workspace_id, QuotaGrant.month == month)
                .order_by(QuotaGrant.granted_at.desc()))).all()
        return [{"grantId": row.id, "userId": row.user_id, "userName": name,
                 "month": row.month.isoformat(), "extraTokens": row.extra_tokens,
                 "reason": row.reason, "grantedBy": row.granted_by,
                 "grantedAt": row.granted_at} for row, name in rows]

    # ---- 组默认（M2：部门与用户组）----

    async def groups(self, actor_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """组默认额度页的数据：空间里的部门与用户组清单，带上已设的默认（没设为 None）与人数。"""
        await self._require_space_admin(actor_id, workspace_id)
        from knowwork_server.access.models import (
            Department,
            DepartmentMember,
            UserGroup,
            UserGroupMember,
        )

        async with self._sessionmaker() as session:
            await set_workspace(session, workspace_id)
            dept_rows = (await session.execute(
                select(Department.id, Department.name,
                       func.count(DepartmentMember.user_id))
                .outerjoin(DepartmentMember, DepartmentMember.department_id == Department.id)
                .where(Department.workspace_id == workspace_id)
                .group_by(Department.id, Department.name)
                .order_by(Department.name))).all()
            group_rows = (await session.execute(
                select(UserGroup.id, UserGroup.name, func.count(UserGroupMember.user_id))
                .outerjoin(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
                .where(UserGroup.workspace_id == workspace_id)
                .group_by(UserGroup.id, UserGroup.name)
                .order_by(UserGroup.name))).all()
            settings = {row.group_type + ":" + row.group_id: row for row in
                        (await session.scalars(select(QuotaGroupSetting)
                                               .where(QuotaGroupSetting.workspace_id == workspace_id))).all()}
        result = []
        for kind, rows in (("department", dept_rows), ("user_group", group_rows)):
            for row in rows:
                setting = settings.get(f"{kind}:{row.id}")
                result.append({"groupType": kind, "groupId": row.id, "name": row.name,
                               "members": row[2],
                               "monthlyTokens": None if setting is None else setting.monthly_tokens,
                               "mode": setting.mode if setting else BLOCK})
        return result

    async def set_group_setting(self, actor_id: str, workspace_id: str, group_type: str, group_id: str,
                                monthly_tokens: int | None, mode: str) -> dict[str, Any]:
        """组默认：设一个正数；None = 删除。组必须真实存在（防止手滑建出悬空规则）。"""
        await self._require_space_admin(actor_id, workspace_id)
        if group_type not in GROUP_TYPES:
            raise Invalid("组类型只有 department 与 user_group")
        if monthly_tokens is not None and monthly_tokens <= 0:
            raise Invalid("组默认额度必须为正")
        if mode not in (BLOCK, WARN):
            raise Invalid(f"口径只能是 {BLOCK} 或 {WARN}")
        from knowwork_server.access.models import Department, UserGroup

        async with self._sessionmaker() as session:
            await set_workspace(session, workspace_id)
            if group_type == "department":
                exists = await session.get(Department, group_id)
            else:
                exists = await session.get(UserGroup, group_id)
        if exists is None:
            raise Invalid("这个组不存在（可能已删除）")
        before_row = await self._group_setting_row(workspace_id, group_type, group_id)
        before = None if before_row is None else {
            "monthly_tokens": before_row.monthly_tokens, "mode": before_row.mode}
        async with self._sessionmaker() as session, session.begin():
            if monthly_tokens is None:
                row = await self._group_setting_row(workspace_id, group_type, group_id)
                if row is not None:
                    await session.delete(row)
                after = None
            else:
                row = await self._group_setting_row(workspace_id, group_type, group_id)
                after = {"monthly_tokens": monthly_tokens, "mode": mode}
                if row is None:
                    session.add(QuotaGroupSetting(workspace_id=workspace_id, group_type=group_type,
                                                  group_id=group_id, monthly_tokens=monthly_tokens,
                                                  mode=mode, updated_by=actor_id))
                else:
                    row.monthly_tokens = monthly_tokens
                    row.mode = mode
                    row.updated_by = actor_id
            await self._log(session, workspace_id, TARGET_GROUP, group_id, before, after, actor_id)
        return {"groupType": group_type, "groupId": group_id,
                "monthlyTokens": monthly_tokens, "mode": mode}

    async def _group_setting_row(self, workspace_id: str, group_type: str,
                                 group_id: str) -> QuotaGroupSetting | None:
        async with self._sessionmaker() as session:
            return await session.get(QuotaGroupSetting, (workspace_id, group_type, group_id))

    # ---- 参考建议值（M2：最近 30 天每人用了多少 → 建议默认额度）----

    async def suggestion(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        """最近 30 天每人用量的平均/中位数/P90 与建议默认额度（P90 向上取整到 10 万的整倍数），
        折算成约多少轮对话与多少元。只统计真人成员（机器人与访客不占个人额度）。"""
        await self._require_space_admin(actor_id, workspace_id)
        end = self._today()
        start = end - timedelta(days=29)
        summaries = await self._usage.summary(workspace_id, start, end, GroupBy.USER)
        members = {member["id"]: member for member in await self._members(workspace_id)}
        used = [(item.total_tokens, item.key) for item in summaries
                if item.key in members and members[item.key]["kind"] == USER_PERSON]
        used.sort()
        tokens_list = [tokens for tokens, _ in used]
        turns = await self._usage.turns(workspace_id, start, end)
        cost_total = sum(item.cost for item in summaries
                         if item.key in members and members[item.key]["kind"] == USER_PERSON)

        def p90(values: list[int]) -> int:
            """最近一档的 P90：nearest-rank（样本少时插值法会飘）。"""
            import math
            return sorted(values)[math.ceil(len(values) * 0.9) - 1]

        def rounded(value: float) -> int:
            step = 100_000
            return int((value + step - 1) // step * step)

        suggested = rounded(p90(tokens_list)) if tokens_list else None
        per_turn = (sum(tokens_list) / turns) if turns else None
        max_row = max(used, key=lambda item: item[0]) if used else None
        return {
            "days": 30,
            "users": len(tokens_list),
            "average": int(statistics.mean(tokens_list)) if tokens_list else 0,
            "median": int(statistics.median(tokens_list)) if tokens_list else 0,
            "p90": p90(tokens_list) if tokens_list else 0,
            "max": {"userId": max_row[1], "name": members[max_row[1]]["name"],
                    "tokens": max_row[0]} if max_row else None,
            "suggestedDefault": suggested,
            "perTurnTokens": int(per_turn) if per_turn else None,
            "quotaTurns": int(suggested / per_turn) if suggested and per_turn else None,
            "costTotal": float(cost_total),
            "quotaCost": (float(suggested * cost_total / sum(tokens_list))
                          if suggested and sum(tokens_list) else None),
        }

    async def changes(self, actor_id: str, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """变更记录（M2 出页面；M1 先给接口与落库）。"""
        await self._require_space_admin(actor_id, workspace_id)
        async with self._sessionmaker() as session:
            rows = (await session.scalars(
                select(QuotaChangeLog).where(QuotaChangeLog.workspace_id == workspace_id)
                .order_by(QuotaChangeLog.id.desc()).limit(min(max(limit, 1), 500)))).all()
        return [{"id": row.id, "target": row.target, "targetId": row.target_id, "before": row.before,
                 "after": row.after, "changedBy": row.changed_by, "changedAt": row.changed_at}
                for row in rows]

    # ---- 个人端 ----

    async def me(self, actor_id: str) -> list[dict[str, Any]]:
        """我所在空间的额度状态（「我的用量」顶栏与输入框提醒共用）：有额度层配置的空间才出现。"""
        workspaces = await self._access.my_workspaces(actor_id)
        states = []
        for view in workspaces:
            resolved = await self.resolve(view.id, actor_id)
            setting = await self._workspace_row(view.id)
            if resolved["tokens"] is None and (setting is None
                                               or setting.monthly_total_tokens is None):
                continue  # 这个空间完全没配额度：不出现（页面写「未设置即不限」）
            state = await self.member_state(view.id, actor_id)
            total = None if setting is None else setting.monthly_total_tokens
            states.append({**state, "workspaceName": view.name,
                           "workspaceTotalTokens": total,
                           "workspaceMode": setting.mode if setting else BLOCK})
        return states
