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
    """工具的执行体：渠道闸 → 判重（一次 embed 同时用于判重与落库）→ 落库。"""

    def __init__(self, service: MemoryService, models: ModelClient | None, profiles=None) -> None:
        self._service = service
        self._models = models
        self._profiles = profiles

    async def handle(self, ctx: ToolContext, arguments: dict[str, Any]) -> str:
        content = str(arguments.get("content", "")).strip()
        kind = arguments.get("type") or PREFERENCE
        if not 1 <= len(content) <= 200:
            raise ToolFailure("要记的内容得是一句 1~200 字的话")
        if kind not in _ALLOWED_TYPES:
            kind = PREFERENCE
        if await self._is_channel(ctx.user_id):
            raise ToolFailure("渠道会话没有可记忆的个人身份")
        if not (await self._service.setting(ctx.user_id))["enabled"]:
            raise ToolFailure("你的个人记忆开关是关闭的，先到 设置 → 我的记忆 打开再记")
        if (await self._service.workspace_disabled(ctx.workspace_id))["disabled"]:
            raise ToolFailure("本空间已停用记忆功能")

        # 一次 embed 同时用于判重与落库（别花两次交互额度）
        alias, vector = await self._embed(ctx.workspace_id, content)
        target = await self._near_duplicate(ctx.user_id, content, alias, vector)
        try:
            if target is not None:
                await self._service.remember_update(ctx.user_id, target["id"], content, kind)
                return f"已记住（更新了原来的说法）：{content}。可在 设置 → 我的记忆 中管理。"

            await self._service.remember_insert(ctx.user_id, ctx.workspace_id, content, kind,
                                                embedding=vector, embedding_model=alias)
        except MemoryError as error:
            raise ToolFailure(str(error)) from error  # 满额等业务错误原样给用户
        return f"已记住：{content}。可在 设置 → 我的记忆 中管理。"

    async def _is_channel(self, user_id: str) -> bool:
        """渠道账号没有「本人」（设计 §6/§10 的显式闸）；当前靠成员判定不可达，这里是第二道。"""
        if self._profiles is None:
            return False
        try:
            found = await self._profiles((user_id,))
            return getattr(found.get(user_id), "kind", "person") == "channel"
        except Exception:
            return False

    async def _near_duplicate(self, user_id: str, content: str, alias: str | None,
                              vector: list[float] | None) -> dict[str, Any] | None:
        """签名精确重复 → 那一条；向量近重复（用户亲口说的，新表述为准）→ 那一条。"""
        pool = await self._service.dedup_pool(user_id)
        for row in pool:
            if row["status"] == ACTIVE and signature(row["content"]) == signature(content):
                return row
        if vector is None:
            return None
        for row in pool:
            if row["status"] == ACTIVE and row["embedding"] and \
                    cosine(vector, row["embedding"]) >= SIMILAR_UPDATE_THRESHOLD:
                return row
        return None

    async def _embed(self, workspace_id: str, content: str) -> tuple[str | None, list[float] | None]:
        """向量 best-effort：没配嵌入模型或调用失败都返回 (None, None)，不影响记忆生效。"""
        if self._models is None:
            return None, None
        try:
            alias = await resolve_model(self._models, workspace_id, Capability.EMBEDDING,
                                        EMBED_MODEL_ENV)
            if alias is None:
                return None, None
            [vector] = await self._models.embed(workspace_id, alias, [content], background=True)
            return alias, vector
        except Exception:
            log.warning("remember embedding skipped (best-effort)", exc_info=True)
            return None, None

    async def _embed_alias(self, workspace_id: str) -> str | None:
        if self._models is None:
            return None
        try:
            return await resolve_model(self._models, workspace_id, Capability.EMBEDDING,
                                       EMBED_MODEL_ENV)
        except Exception:
            log.warning("remember embed alias lookup failed", exc_info=True)
            return None


def build_remember_tool(service: MemoryService, models: ModelClient | None,
                        profiles=None) -> tuple[BuiltinTool, Any]:
    """装配：available 查双开关（关了不给会话这个工具，宿主每会话拉一次清单，
    会话中途关掉也执行不了——tools/provided.py 的语义现成）。"""

    tool = RememberTool(service, models, profiles)

    async def available(ctx: ToolContext) -> bool:
        try:
            if await tool._is_channel(ctx.user_id):  # noqa: SLF001 同包内复用渠道闸
                return False
            if not (await service.setting(ctx.user_id))["enabled"]:
                return False
            if (await service.workspace_disabled(ctx.workspace_id))["disabled"]:
                return False
            return True
        except Exception:
            log.warning("remember tool availability check failed; hiding tool", exc_info=True)
            return False

    return BuiltinTool(spec=_spec(), handler=tool.handle), available
