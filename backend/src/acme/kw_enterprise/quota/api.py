"""额度层路由：个人端（/api/quota）与管理端（/api/workspaces/{ws}/quota/...）。

只取调用者、调服务、映射错误（同 memory/api.py 的分工）。个人端只要登录（成员本人才有额度）；
管理端的空间管理员判定在服务层（workspace:edit），空间月总额要系统管理员（服务层 is_system_admin）。
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from knowwork_server.audit.context import annotate
from knowwork_server.auth.principal import Principal
from pydantic import BaseModel, ConfigDict, Field

from .service import Denied, Invalid, QuotaService

WorkspacePath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]
UserPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]
Mode = Literal["block", "warn"]


async def _call(awaitable) -> Any:
    try:
        return await awaitable
    except Denied as error:
        raise HTTPException(status_code=403,
                            detail={"code": "quota_denied", "message": str(error)}) from error
    except Invalid as error:
        raise HTTPException(status_code=422,
                            detail={"code": "quota_invalid", "message": str(error)}) from error


class WorkspaceSettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None = 清掉这一项（不限）。monthlyTotalTokens 只有系统管理员能改。
    monthlyTotalTokens: int | None = Field(default=None, ge=1)
    memberDefaultTokens: int | None = Field(default=None, ge=1)
    mode: Mode = "block"


class MemberSettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthlyTokens: int | None = Field(default=None, ge=1)  # None = 删除个人额度，回到空间默认
    mode: Mode = "block"


class GroupSettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthlyTokens: int | None = Field(default=None, ge=1)  # None = 删除组默认
    mode: Mode = "block"


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    extraTokens: int = Field(ge=1)
    reason: str = Field(default="", max_length=200)


def build_routers(ctx, service: QuotaService | None = None) -> list[APIRouter]:
    """装配额度路由；service 参数给测试注入替身用。"""
    service = service or QuotaService(ctx.global_sessionmaker, ctx.access, ctx.usage)
    router = APIRouter(prefix="/api/quota", tags=["额度"])
    workspace_router = APIRouter(prefix="/api/workspaces/{workspace_id}/quota", tags=["额度"])
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    # ---- 个人端 ----

    @router.get("/me")
    async def my_quota(who: Who) -> list[dict]:
        """我所在空间的额度状态（「我的用量」顶栏与输入框提醒共用）；没配额度的空间不出现"""
        return await _call(service.me(who.user_id))

    @router.get("/status")
    async def quota_status(who: Who, workspace_id: str = Query(min_length=1, max_length=64)) -> dict:  # noqa: B008
        """单个空间的本人额度状态（输入框按当前空间问）"""
        return await _call(service.member_state(workspace_id, who.user_id))

    # ---- 管理端（空间管理员）----

    @workspace_router.get("/setting")
    async def get_setting(workspace_id: WorkspacePath, who: Who) -> dict:
        """空间的总额与个人默认（额度页四卡用；空间管理员可读，没有行 = 全不限）"""
        return await _call(service.workspace_setting_read(who.user_id, workspace_id))

    @workspace_router.put("/setting")
    async def put_setting(workspace_id: WorkspacePath, body: WorkspaceSettingBody, who: Who) -> dict:
        """空间月总额（系统管理员）与个人默认额度、口径（空间管理员）"""
        result = await _call(service.set_workspace_setting(
            who.user_id, workspace_id, monthly_total_tokens=body.monthlyTotalTokens,
            member_default_tokens=body.memberDefaultTokens, mode=body.mode))
        annotate(action="quota.setting.update", resource_type="quota", resource_id=workspace_id,
                 monthly_total=body.monthlyTotalTokens, member_default=body.memberDefaultTokens,
                 mode=body.mode)
        return result

    @workspace_router.get("/overview")
    async def overview(workspace_id: WorkspacePath, who: Who) -> dict:
        """额度页：设置 + 成员列表（本月已用/额度/来源/状态）"""
        return await _call(service.overview(who.user_id, workspace_id))

    @workspace_router.put("/member/{user_id}")
    async def put_member(workspace_id: WorkspacePath, user_id: UserPath, body: MemberSettingBody,
                         who: Who) -> dict:
        """个人额度（None 删除，回到空间默认）"""
        result = await _call(service.set_member_setting(who.user_id, workspace_id, user_id,
                                                        body.monthlyTokens, body.mode))
        annotate(action="quota.member.update", resource_type="quota", resource_id=user_id,
                 workspace_id=workspace_id, monthly_tokens=body.monthlyTokens, mode=body.mode)
        return result

    @workspace_router.post("/grants", status_code=201)
    async def add_grant(workspace_id: WorkspacePath, body: GrantBody, who: Who) -> dict:
        """临时加额一笔（当月有效，月底自动失效）"""
        result = await _call(service.add_grant(who.user_id, workspace_id, body.userId,
                                               body.extraTokens, body.reason))
        annotate(action="quota.grant.add", resource_type="quota", resource_id=body.userId,
                 workspace_id=workspace_id, extra_tokens=body.extraTokens, reason=body.reason)
        return result

    @workspace_router.get("/grants")
    async def list_grants(workspace_id: WorkspacePath, who: Who) -> list[dict]:
        """本空间的临时加额（M1 只出当月）"""
        return await _call(service.grants(who.user_id, workspace_id))

    @workspace_router.get("/groups")
    async def list_groups(workspace_id: WorkspacePath, who: Who) -> list[dict]:
        """部门与用户组清单（带人数与已设的默认额度；没设为 None）"""
        return await _call(service.groups(who.user_id, workspace_id))

    @workspace_router.put("/group/{group_type}/{group_id}")
    async def put_group(workspace_id: WorkspacePath, group_type: str, group_id: UserPath,
                        body: GroupSettingBody, who: Who) -> dict:
        """组默认额度（None 删除）；一次在多个组里的人取最高的那档，个人设置覆盖组默认"""
        result = await _call(service.set_group_setting(who.user_id, workspace_id, group_type,
                                                       group_id, body.monthlyTokens, body.mode))
        annotate(action="quota.group.update", resource_type="quota", resource_id=group_id,
                 workspace_id=workspace_id, group_type=group_type,
                 monthly_tokens=body.monthlyTokens, mode=body.mode)
        return result

    @workspace_router.get("/suggestion")
    async def suggestion(workspace_id: WorkspacePath, who: Who) -> dict:
        """最近 30 天每人用量的参考（平均/中位数/P90/最多）与建议的默认额度"""
        return await _call(service.suggestion(who.user_id, workspace_id))

    @workspace_router.get("/changes")
    async def list_changes(workspace_id: WorkspacePath, who: Who, limit: int = 100) -> list[dict]:
        """变更记录（M2 出页面，M1 先给接口）"""
        return await _call(service.changes(who.user_id, workspace_id, limit=limit))

    return [router, workspace_router]
