"""空间管理路由（/api/system/workspaces）：列表、新建、关闭给系统管理员；改名给空间所有者或系统管理员
（口径在服务层：access.workspaces 的 _require_system_admin 与 _require(actor, workspace, OWNER)）。
这里只取调用者、调服务、映射错误。路径前缀沿用上游 apidocs 预留的「空间管理」分组
（knowwork_server/apidocs/__init__.py 对 /api/system/workspaces 已分组）。
"""

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from knowwork_server.access.errors import AccessDenied, AccessError, Conflict, NotFound
from knowwork_server.access.workspaces import WorkspaceSummary
from knowwork_server.auth.principal import Principal
from pydantic import BaseModel, ConfigDict, Field

IdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]

_STATUS = {AccessDenied: 403, NotFound: 404, Conflict: 409}


async def _call(awaitable) -> Any:
    """服务层异常 → HTTP：AccessDenied 403、NotFound 404、Conflict 409，其余 400。"""
    try:
        return await awaitable
    except AccessError as error:
        status = next((code for base, code in _STATUS.items() if isinstance(error, base)), 400)
        detail = {"code": "workspace_error", "message": str(error)}
        raise HTTPException(status_code=status, detail=detail) from error


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    owner_user_id: str = Field(validation_alias="ownerUserId", min_length=1, max_length=64)


class RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


def build_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/system/workspaces", tags=["system"])
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    @router.get("")
    async def list_workspaces(who: Who) -> list[dict]:
        """全部未关闭的空间与各自的所有者"""
        found: tuple[WorkspaceSummary, ...] = await _call(ctx.access.all_workspaces(who.user_id))
        # isDefault 给前端隐藏「关闭」入口用（平台配置存在默认空间名下），不在前端硬编码 id 口径
        return [{**asdict(summary), "isDefault": summary.id == "default"} for summary in found]

    @router.post("", status_code=201)
    async def create_workspace(body: CreateBody, who: Who) -> dict:
        """新建空间并指定所有者"""
        workspace_id = await _call(ctx.access.open_workspace(
            who.user_id, name=body.name, owner_user_id=body.owner_user_id))
        return {"id": workspace_id, "name": body.name}

    @router.put("/{workspace_id}")
    async def rename_workspace(workspace_id: IdPath, body: RenameBody, who: Who) -> dict:
        """空间改名（空间所有者或系统管理员）"""
        await _call(ctx.access.rename_workspace(who.user_id, workspace_id, body.name))
        return {"id": workspace_id, "name": body.name}

    @router.delete("/{workspace_id}", status_code=204)
    async def close_workspace(workspace_id: IdPath, who: Who) -> Response:
        """关闭空间（软删除；默认空间不能关）。成员立即失去访问，数据保留。"""
        await _call(ctx.access.close_workspace(who.user_id, workspace_id))
        return Response(status_code=204)

    return router
