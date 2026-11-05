"""个人记忆路由：/api/memory（本人）与 /api/workspaces/{ws}/memory-setting（空间管理员）。

这里只取调用者、调服务、映射错误（同 workspaces_api 的分工）。全部用会话身份
principal(None)，不设 API Key 范围要求——个人 Key 也是本人身份，风险等价。
清空与开关登记审计动作名，其余路由走审计的自动派生名。
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from knowwork_server.audit.context import annotate
from knowwork_server.auth.principal import Principal
from pydantic import BaseModel, ConfigDict, Field

from .models import CONTENT_MAX
from .service import Conflict, Denied, Invalid, MemoryError, MemoryService, NotFound

MemoryType = Literal["preference", "focus", "fact", "task"]
MemoryStatus = Literal["active", "pending", "superseded", "archived"]
MemoryId = Annotated[str, Path(pattern=r"^mem_[0-9a-f]{32}$")]
WorkspacePath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]

_STATUS = {Denied: 403, NotFound: 404, Conflict: 409, Invalid: 422}


async def _call(awaitable) -> Any:
    """服务层异常 → HTTP：403/404/409/422，其余 400。"""
    try:
        return await awaitable
    except MemoryError as error:
        status = next((code for base, code in _STATUS.items() if isinstance(error, base)), 400)
        detail = {"code": "memory_error", "message": str(error)}
        raise HTTPException(status_code=status, detail=detail) from error


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MemoryType
    content: str = Field(min_length=1, max_length=CONTENT_MAX)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=CONTENT_MAX)
    type: MemoryType | None = None
    expiresAt: datetime | None = None
    clearExpires: bool = False


class SettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class OrganizeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaceId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]{1,64}$")


class WorkspaceSettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: bool


def build_routers(ctx, service: MemoryService | None = None) -> list[APIRouter]:
    """装配记忆路由；service 参数给测试注入替身用（不传按真实依赖构建）。"""
    service = service or MemoryService(ctx.global_sessionmaker, ctx.access)
    router = APIRouter(prefix="/api/memory", tags=["记忆"])
    workspace_router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["记忆"])
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    # 固定路径都排在 /{memory_id} 之前，FastAPI 按声明顺序匹配

    @router.get("")
    async def list_memories(who: Who, status: MemoryStatus = "active", limit: int = 50,
                            offset: int = 0) -> dict:
        """按状态分页的个人记忆列表"""
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)
        items, total = await _call(service.page(who.user_id, status=status, limit=limit, offset=offset))
        return {"items": items, "total": total}

    @router.get("/counts")
    async def memory_counts(who: Who) -> dict:
        """各状态的条数与生效上限（页签角标、200 上限用）"""
        return await _call(service.counts(who.user_id))

    @router.post("", status_code=201)
    async def add_memory(body: AddBody, who: Who) -> dict:
        """手动添加一条记忆，直接生效"""
        return await _call(service.add(who.user_id, type=body.type, content=body.content))

    @router.post("/organize", status_code=202)
    async def organize(body: OrganizeBody, who: Who) -> dict:
        """立即整理：合并说法重复的、很久没提的降权、到期归档；每天也会自动整理一次（M3 接上）"""
        job_id = await _call(service.organize(who.user_id, body.workspaceId))
        return {"jobId": job_id}

    @router.get("/settings")
    async def get_settings(who: Who) -> dict:
        """个人记忆开关（没有记录 = 开启）"""
        return await _call(service.setting(who.user_id))

    @router.put("/settings")
    async def put_settings(body: SettingBody, who: Who) -> dict:
        """开关个人记忆：关掉后助手不再读取或新增，数据保留"""
        result = await _call(service.set_setting(who.user_id, body.enabled))
        annotate(action="memory.setting.update", resource_type="memory", resource_id=who.user_id,
                 enabled=body.enabled)
        return result

    @router.get("/export")
    async def export_memories(who: Who) -> list[dict]:
        """导出全部状态的个人记忆（数据可携带）"""
        return await _call(service.export(who.user_id))

    @router.post("/clear", status_code=204)
    async def clear_memories(who: Who) -> Response:
        """物理清空本人全部记忆（各状态都删，不可恢复）"""
        deleted = await _call(service.clear(who.user_id))
        annotate(action="memory.clear", resource_type="memory", resource_id=who.user_id, deleted=deleted)
        return Response(status_code=204)

    @router.patch("/{memory_id}")
    async def edit_memory(memory_id: MemoryId, body: EditBody, who: Who) -> dict:
        """编辑内容/类型/期限（编辑内容后旧向量作废，整理时重算）"""
        return await _call(service.edit(who.user_id, memory_id, content=body.content, type=body.type,
                                        expires_at=body.expiresAt, clear_expires=body.clearExpires))

    @router.delete("/{memory_id}", status_code=204)
    async def delete_memory(memory_id: MemoryId, who: Who) -> Response:
        """删除一条记忆（物理删除，任何状态都可以删）"""
        await _call(service.delete(who.user_id, memory_id))
        return Response(status_code=204)

    @router.post("/{memory_id}/confirm")
    async def confirm_memory(memory_id: MemoryId, who: Who) -> dict:
        """待确认 → 生效中"""
        return await _call(service.confirm(who.user_id, memory_id))

    @router.post("/{memory_id}/dismiss", status_code=204)
    async def dismiss_memory(memory_id: MemoryId, who: Who) -> Response:
        """待确认忽略 = 物理删除"""
        await _call(service.dismiss(who.user_id, memory_id))
        return Response(status_code=204)

    @router.post("/{memory_id}/restore")
    async def restore_memory(memory_id: MemoryId, who: Who) -> dict:
        """已归档 / 已被更新 → 生效中"""
        return await _call(service.restore(who.user_id, memory_id))

    # 空间开关（成员可读、管理员改）：路径落在 workspace 前缀下，与开源的空间路由同族
    @workspace_router.get("/memory-setting")
    async def get_workspace_setting(workspace_id: WorkspacePath, who: Who) -> dict:
        """本空间的记忆开关（没有记录 = 启用；成员可读，页面提示用）"""
        return await _call(service.workspace_setting(who.user_id, workspace_id))

    @workspace_router.put("/memory-setting")
    async def put_workspace_setting(workspace_id: WorkspacePath, body: WorkspaceSettingBody,
                                    who: Who) -> dict:
        """空间管理员停用/启用本空间的记忆：停用后注入、提取、记住工具全停，个人数据保留"""
        result = await _call(service.set_workspace_setting(who.user_id, workspace_id, body.disabled))
        annotate(action="memory.workspace_setting.update", resource_type="memory",
                 resource_id=workspace_id, disabled=body.disabled)
        return result

    # 空间开关路由有独立前缀，不能 include 进带 prefix 的 router（会被组合成
    # /api/memory/api/workspaces/...）：作为兄弟路由一起返回，setup_api 本来就收列表
    return [router, workspace_router]
