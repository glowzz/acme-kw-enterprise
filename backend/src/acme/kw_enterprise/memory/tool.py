"""「记住」对话工具（设计 §6）：用户明确说「记住 / 以后都… / 别再…」时，模型调它写入个人记忆。

审批=never 的理由：写的是用户本人的记忆列表，风险与用户自己打一行字等价；副作用在回答下方
明示（chat.tools 的 notice），设置页随时可删。要更保守就把 ToolSpec 的 approval 行删掉，
回落到 write 的默认审批——一行改动，不影响其他设计。
"""

import logging
from typing import Any

from knowwork_server.models.client import ModelClient
from knowwork_server.models.schemas import Capability
from knowwork_server.tools.registry import BuiltinTool, ToolContext, ToolFailure, ToolSpec

from .dedup import cosine, signature
from .extract import EMBED_MODEL_ENV, resolve_model
from .models import ACTIVE, PREFERENCE, TASK
from .service import MemoryError, MemoryService

log = logging.getLogger(__name__)

SIMILAR_UPDATE_THRESHOLD = 0.82  # 与既有记忆近重复 → 改写那一条而不是重复落库
TOOL_NAME = "remember"

_ALLOWED_TYPES = (PREFERENCE, "focus", "fact", TASK)


def _spec() -> ToolSpec:
    return ToolSpec(
        name=TOOL_NAME,
        description="把用户明确要求长期记住的偏好或事实写入他的个人记忆（之后的会话都会带上）。"
                    "只在用户明确说「记住 / 以后都… / 别再…」时调用；一次一条，"
                    "改写成一句第三人称的中文陈述（否定与限定词必须保留）。",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": 200,
                            "description": "要记住的一句话，如「回答用表格」"},
                "type": {"type": "string", "enum": ["preference", "fact", "task", "focus"]},
            },
            "required": ["content"],
        },
        risk="write",
        approval="never",
        concurrent=False,
        answer_modes=frozenset({"agent", "quick"}),  # 快速问答里用户说「记住」也要能记
        guidance="用户说「记住」时才用；写完用一句话向用户确认记了什么。",
    )


class RememberTool:
    """工具的执行体：判重 → 落库 → 向量 best-effort。"""

    def __init__(self, service: MemoryService, models: ModelClient | None) -> None:
        self._service = service
        self._models = models

    async def handle(self, ctx: ToolContext, arguments: dict[str, Any]) -> str:
        content = str(arguments.get("content", "")).strip()
        kind = arguments.get("type") or PREFERENCE
        if not 1 <= len(content) <= 200:
            raise ToolFailure("要记的内容得是一句 1~200 字的话")
        if kind not in _ALLOWED_TYPES:
            kind = PREFERENCE
        if not (await self._service.setting(ctx.user_id))["enabled"]:
            raise ToolFailure("你的个人记忆开关是关闭的，先到 设置 → 我的记忆 打开再记")
        if (await self._service.workspace_disabled(ctx.workspace_id))["disabled"]:
            raise ToolFailure("本空间已停用记忆功能")

        target = await self._near_duplicate(ctx.user_id, ctx.workspace_id, content)
        try:
            if target is not None:
                await self._service.remember_update(ctx.user_id, target["id"], content, kind)
                return f"已记住（更新了原来的说法）：{content}。可在 设置 → 我的记忆 中管理。"

            vector = await self._embed(ctx.workspace_id, content)
            await self._service.remember_insert(ctx.user_id, ctx.workspace_id, content, kind,
                                                embedding=vector)
        except MemoryError as error:
            raise ToolFailure(str(error)) from error  # 满额等业务错误原样给用户
        return f"已记住：{content}。可在 设置 → 我的记忆 中管理。"

    async def _near_duplicate(self, user_id: str, workspace_id: str,
                              content: str) -> dict[str, Any] | None:
        """签名精确重复 → 那一条；向量近重复（用户亲口说的，新表述为准）→ 那一条。"""
        pool = await self._service.dedup_pool(user_id)
        for row in pool:
            if row["status"] == ACTIVE and signature(row["content"]) == signature(content):
                return row
        alias = await self._embed_alias(workspace_id)
        if alias is None or self._models is None:
            return None
        try:
            [vector] = await self._models.embed(workspace_id, alias, [content], background=True)
        except Exception:
            log.warning("remember embed failed; falling back to signature dedup", exc_info=True)
            return None
        for row in pool:
            if row["status"] == ACTIVE and row["embedding"] and \
                    cosine(vector, row["embedding"]) >= SIMILAR_UPDATE_THRESHOLD:
                return row
        return None

    async def _embed(self, workspace_id: str, content: str) -> list[float] | None:
        """向量 best-effort：没配嵌入模型或调用失败都留空，不影响记忆生效。"""
        alias = await self._embed_alias(workspace_id)
        if alias is None or self._models is None:
            return None
        try:
            [vector] = await self._models.embed(workspace_id, alias, [content], background=True)
            return vector
        except Exception:
            log.warning("remember embedding skipped (best-effort)", exc_info=True)
            return None

    async def _embed_alias(self, workspace_id: str) -> str | None:
        if self._models is None:
            return None
        try:
            return await resolve_model(self._models, workspace_id, Capability.EMBEDDING,
                                       EMBED_MODEL_ENV)
        except Exception:
            log.warning("remember embed alias lookup failed", exc_info=True)
            return None


def build_remember_tool(service: MemoryService, models: ModelClient | None) -> tuple[BuiltinTool, Any]:
    """装配：available 查双开关（关了不给会话这个工具，宿主每会话拉一次清单，
    会话中途关掉也执行不了——tools/provided.py 的语义现成）。"""

    async def available(ctx: ToolContext) -> bool:
        try:
            if not (await service.setting(ctx.user_id))["enabled"]:
                return False
            if (await service.workspace_disabled(ctx.workspace_id))["disabled"]:
                return False
            return True
        except Exception:
            log.warning("remember tool availability check failed; hiding tool", exc_info=True)
            return False

    return BuiltinTool(spec=_spec(), handler=RememberTool(service, models).handle), available
