"""企业 MCP 管理路由（/api/enterprise-mcp）：平台层的服务登记、工具三档、发放、收编，给系统管理员。

服务层是开源的 EnterpriseMcpAdmin（tools/mcp/enterprise.py，ApiContext.enterprise_mcp 已装配），
系统管理员判定在它内部；各方法与空间服务同名，只是不用传空间。错误映射与视图照空间路由
（tools/mcp/api.py）的形态：McpError → HTTPException，McpServerView.of 带回调地址、
response_model_by_alias 输出 camelCase。响应永不回显请求头、令牌、client secret 明文（服务层保证）。
空间侧的到达/停用/收窄（/api/workspaces/{ws}/enterprise-mcp）与成员连接开源已有，这里不重复。
"""

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from knowwork_server.access.errors import AccessDenied, AccessError, Conflict, NotFound
from knowwork_server.auth.principal import Principal
from knowwork_server.tools.mcp.errors import McpError
from knowwork_server.tools.mcp.schemas import (
    McpServerCreate,
    McpServerPatch,
    McpServerView,
    McpToolPatch,
    ProbeBody,
    ProbeView,
)
from pydantic import BaseModel, ConfigDict, Field

IdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]
ToolPath = Annotated[str, Path(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]


class AudienceBody(BaseModel):
    """发给哪些空间：["*"] 是全部空间（以后新建的也有），否则是空间 id 列表。
    元素在路由层先按 id 的口径把关（服务层再验存在性），混入任意超长串直接 422。"""

    model_config = ConfigDict(extra="forbid")

    workspaces: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\-*]{1,64}$")]] = Field(max_length=500)


class PromoteBody(BaseModel):
    """收编：空间服务的 id + 系统管理员重填的连接信息（凭据不带过来）。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(validation_alias="workspaceId", min_length=1, max_length=64)
    server_id: str = Field(validation_alias="serverId", min_length=1, max_length=64)
    server: McpServerCreate


_ACCESS_STATUS = {AccessDenied: 403, NotFound: 404, Conflict: 409}


async def _call(awaitable) -> Any:
    """McpError 照上游映射；发放/收编会调 access 层列空间（all_workspaces），那里的异常也要接住，
    不然非管理员摸到这些路由时 403 会变 500。"""
    try:
        return await awaitable
    except McpError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    except AccessError as error:
        status = next((code for base, code in _ACCESS_STATUS.items() if isinstance(error, base)), 400)
        detail = {"code": "workspace_error", "message": str(error)}
        raise HTTPException(status_code=status, detail=detail) from error


def build_router(ctx) -> APIRouter:
    admin = ctx.enterprise_mcp  # EnterpriseMcpAdmin；app.py 装配时恒给
    router = APIRouter(prefix="/api/enterprise-mcp", tags=["tools"])
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    async def _view(awaitable) -> McpServerView:
        record = await _call(awaitable)
        return McpServerView.of(record, admin.redirect_uri)

    @router.get("")
    async def list_servers(who: Who) -> list[McpServerView]:
        """企业 MCP 服务列表（含工具清单快照）"""
        records = await _call(admin.list_servers(who.user_id))
        return [McpServerView.of(record, admin.redirect_uri) for record in records]

    @router.post("/probe")
    async def probe(body: ProbeBody, who: Who) -> ProbeView:
        """探测 MCP 服务的登录方式（是不是 OAuth、要不要凭据、回调地址）"""
        found = await _call(admin.probe(who.user_id, body.url))
        return ProbeView(oauth=found.oauth, needs_credentials=found.needs_credentials,
                         dynamic_registration=found.dynamic_registration, scope=found.scope,
                         redirect_uri=found.redirect_uri)

    @router.get("/promotable")
    async def promotable(who: Who) -> list[dict]:
        """可以收编为企业 MCP 的空间服务（全部空间里登记的）"""
        found = await _call(admin.promotable(who.user_id))
        return [{"workspace": asdict(workspace), "server": McpServerView.of(server, admin.redirect_uri)}
                for workspace, server in found]

    @router.post("/promote")
    async def promote(body: PromoteBody, who: Who) -> McpServerView:
        """把空间 MCP 收为企业 MCP（工具三档跟着上来，凭据用 body 里重填的，刚建时没发给任何空间）"""
        record = await _call(admin.promote(who.user_id, body.workspace_id, body.server_id, body.server))
        return McpServerView.of(record, admin.redirect_uri)

    @router.post("", status_code=201)
    async def create(body: McpServerCreate, who: Who) -> McpServerView:
        """接入企业 MCP 服务（先连一次、拉工具清单，连不上不保存）"""
        return await _view(admin.create(who.user_id, body))

    @router.get("/{server_id}")
    async def detail(server_id: IdPath, who: Who) -> McpServerView:
        """一个企业 MCP 服务的配置与工具"""
        return await _view(admin.get(who.user_id, server_id))

    @router.patch("/{server_id}")
    async def update(server_id: IdPath, body: McpServerPatch, who: Who) -> McpServerView:
        """改名称、地址、请求头、超时、启停"""
        return await _view(admin.update(who.user_id, server_id, body))

    @router.delete("/{server_id}", status_code=204)
    async def delete(server_id: IdPath, who: Who) -> Response:
        """删除企业 MCP（各空间的收窄授权残留无害，服务层不跨空间清）"""
        await _call(admin.delete(who.user_id, server_id))
        return Response(status_code=204)

    @router.post("/{server_id}/sync")
    async def sync(server_id: IdPath, who: Who) -> McpServerView:
        """重新拉工具清单"""
        return await _view(admin.sync(who.user_id, server_id))

    @router.patch("/{server_id}/tools/{tool_name}")
    async def configure_tool(server_id: IdPath, tool_name: ToolPath, body: McpToolPatch,
                             who: Who) -> McpServerView:
        """改单个工具的三档（允许/需确认/禁用）与分组（tool 是对外名字）"""
        return await _view(admin.configure_tool(who.user_id, server_id, tool_name, body))

    @router.put("/{server_id}/audience")
    async def set_audience(server_id: IdPath, body: AudienceBody, who: Who) -> McpServerView:
        """改发给哪些空间"""
        record = await _call(admin.set_audience(who.user_id, server_id, body.workspaces))
        return McpServerView.of(record, admin.redirect_uri)

    return router
