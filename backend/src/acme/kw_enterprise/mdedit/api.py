"""Markdown 人机双改的路由：编辑器页 + 读/存/摘要三条 JSON（docs/Markdown人机双改设计.md §2）。

读写、版本、对话归属全走内核的 ctx.file_editing（documents/editing.py，v0.1.2 为扩展编辑开的正式口子）；
扩展只决定哪些格式能编辑（.md/.markdown/.mkd，≤1 MB，UTF-8）。鉴权同记忆路由口径：
会话身份 principal(None) + access.check(workspace:use)；locate 里再判对话归本人、附件目录只读。
"""

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from knowwork_server.auth.principal import Principal
from pydantic import BaseModel, ConfigDict, Field

from .editor_page import PAGE

MAX_MD_BYTES = 1024 * 1024
EDITABLE_SUFFIXES = (".md", ".markdown", ".mkd")
_FORMAT_NOTE = "现在只有 Markdown（.md/.markdown/.mkd）能在网页里改"


async def _call(awaitable) -> Any:
    """file_editing 的 EditRefused → HTTP（status 由内核给，消息直接给人看）。"""
    from knowwork_server.documents.editing import EditRefused

    try:
        return await awaitable
    except EditRefused as error:
        raise HTTPException(status_code=error.status,
                            detail={"code": "mdedit_refused", "message": error.message}) from error


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    content: str
    base_digest: str = Field(min_length=8, max_length=128)
    force: bool = False  # 冲突时人选了「保留我的」：覆盖 AI 的中间版，成为最新一版


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_router(ctx) -> APIRouter:
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    async def _member(who: Principal, workspace_id: str) -> None:
        if ctx.access is None or not ctx.access.check(who.user_id, workspace_id, "workspace", "use"):
            raise HTTPException(status_code=403,
                                detail={"code": "mdedit_denied", "message": "只有空间成员能编辑这里的文件"})

    def _editing():
        if ctx.file_editing is None:
            raise HTTPException(status_code=503,
                                detail={"code": "mdedit_unavailable", "message": "这套服务没开文件编辑"})
        return ctx.file_editing

    def _editable(path: str, data: bytes | str | None = None) -> None:
        if not path.lower().endswith(EDITABLE_SUFFIXES):
            raise HTTPException(status_code=415, detail={"code": "mdedit_format", "message": _FORMAT_NOTE})
        if isinstance(data, bytes) and len(data) > MAX_MD_BYTES:
            raise HTTPException(status_code=413,
                                detail={"code": "mdedit_too_large", "message": "文件超过 1 MB，改用本机编辑"})

    router = APIRouter(prefix="/api/workspaces/{workspace_id}/md-editor", tags=["文件编辑"])

    @router.get("")
    async def page(request: Request, who: Who) -> Response:
        """编辑器页（同源 iframe 里跑；no-store：每次打开都拿最新的页面）"""
        await _member(who, request.path_params["workspace_id"])
        return Response(PAGE, media_type="text/html",
                        headers={"cache-control": "private, no-store"})

    @router.get("/file")
    async def read_file(workspace_id: str, conversation_id: str, path: str, who: Who) -> dict:
        """这个路径最新的一版 + 它的摘要（编辑器据此做冲突比对）"""
        await _member(who, workspace_id)
        _editable(path)
        editing = _editing()
        file = await _call(editing.locate(workspace_id=workspace_id, conversation_id=conversation_id,
                                          user_id=who.user_id, path=path))
        data = await _call(editing.read(file))
        _editable(path, data)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=415,
                                detail={"code": "mdedit_format",
                                        "message": "不是 UTF-8 文本，不能当 Markdown 编辑"}) from error
        return {"name": file.name, "content": content, "digest": await _call(editing.latest_digest(file))}

    @router.put("/file")
    async def save_file(workspace_id: str, body: SaveBody, who: Who) -> dict:
        """存成对话里这个路径的新一版。base_digest 与最新版不一致且非 force → 409，带最新内容给人选"""
        await _member(who, workspace_id)
        _editable(body.path)
        data = body.content.encode("utf-8")
        _editable(body.path, data)
        editing = _editing()
        file = await _call(editing.locate(workspace_id=workspace_id, conversation_id=body.conversation_id,
                                          user_id=who.user_id, path=body.path))
        latest = await _call(editing.latest_digest(file))
        if latest != body.base_digest and not body.force:
            conflict = await _call(editing.read(file))
            return Response(
                json.dumps({"latest_digest": latest, "content": conflict.decode("utf-8", errors="replace")},
                           ensure_ascii=False),
                status_code=409, media_type="application/json")
        saved = await _call(editing.save(file, data))
        return {"changed": saved.changed, "artifactId": saved.artifact_id, "synced": saved.synced,
                "digest": await _call(editing.latest_digest(file))}

    @router.get("/digest")
    async def file_digest(workspace_id: str, conversation_id: str,
                          path: Annotated[str, Query(min_length=1, max_length=4096)], who: Who) -> dict:
        """这个路径当前最新版的摘要（编辑器与外层卡片轮询「AI 改没改」用）"""
        await _member(who, workspace_id)
        _editable(path)
        editing = _editing()
        file = await _call(editing.locate(workspace_id=workspace_id, conversation_id=conversation_id,
                                          user_id=who.user_id, path=path))
        return {"digest": await _call(editing.latest_digest(file))}

    return router
