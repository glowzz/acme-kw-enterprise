"""每轮补上下文的提供者（设计 §4）：standing（常驻）+ turn（按本轮提问召回）。

纪律：**提供者自己吞掉一切异常、降级交回空**——v0.0.6 起一个提供者出错整轮上下文都不带
（turn_context.collect 是全有全无），记忆挂了不能连累别的提供者，更不能挡回答。
3 秒预算内只做：一次全局表读 + 至多一次 embed（交互额度）+ 两个 fire-and-forget 记账任务。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from knowwork_server.conversations.turn_context import TurnContextRequest
from knowwork_server.kernel.contract import TurnContext
from knowwork_server.models.client import ModelCallError, ModelClient
from knowwork_server.models.schemas import Capability

from .dedup import cosine
from .models import FACT, FOCUS, PREFERENCE, TASK
from .service import MemoryService

log = logging.getLogger(__name__)

STANDING_BUDGET = 2500   # 自限，总量由 TurnContext 的 8000 兜底；多提供者拼接后不挤爆
TURN_BUDGET = 1500
FACT_FOCUS_TOP = 20      # 事实/长期关注按 score 取前几条进 standing（偏好与在办全进）
RECALL_THRESHOLD = 0.35  # turn 段与本轮提问的余弦门槛
RECALL_TOP = 10
GROUP_LABELS = {PREFERENCE: "偏好", TASK: "在办", FACT: "事实", FOCUS: "关注"}
STANDING_ORDER = (PREFERENCE, TASK, FACT, FOCUS)

Profiles = Callable[[Any], Awaitable[dict[Any, Any]]]


def standing_rows(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """standing 段选中的行（与 format_standing 同一来源）：偏好 → 在办 → 事实 → 关注，
    组内 score 降序、id 升序；事实/关注取前 20。turn 段的查重也用它——第 21 条事实不在
    standing 里，就该能被 turn 段召回（R 之前把「全部 active」错当「已注入」）。"""
    rows: list[dict[str, Any]] = []
    for group in STANDING_ORDER:
        group_rows = sorted((row for row in memories if row["type"] == group),
                            key=lambda row: (-row["score"], row["id"]))
        if group in (FACT, FOCUS):
            group_rows = group_rows[:FACT_FOCUS_TOP]
        rows.extend(group_rows)
    return rows


def format_standing(memories: list[dict[str, Any]]) -> str:
    """确定性生成（同输入同输出，宿主按内容摘要去重、前缀缓存才不动）；超预算整行丢弃。"""
    lines: list[str] = []
    for row in standing_rows(memories):
        line = f"【{GROUP_LABELS[row['type']]}】{row['content']}"
        if row["type"] == TASK and row["expiresAt"] is not None:
            line += f"（{row['expiresAt']:%m-%d} 前）"
        lines.append(line)
    body = "\n".join(lines)
    header = "以下是关于这位用户的长期记忆，回答时参考；与当前提问冲突时以提问为准。"
    while len(body) > STANDING_BUDGET and lines:
        lines.pop()  # 从优先级最低的组尾开始丢整行，不截半句
        body = "\n".join(lines)
    return f"{header}\n{body}" if body else ""


def format_turn(hits: list[dict[str, Any]]) -> str:
    body = "\n".join(f"- {hit['content']}" for hit in hits)
    return f"【相关记忆】\n{body}" if body else ""


class MemoryContext:
    """提供者的宿主：守卫链、standing/turn 的生成、记账与顺手整理的后台任务都在这里。"""

    def __init__(self, service: MemoryService, models: ModelClient | None,
                 profiles: Profiles | None) -> None:
        self._service = service
        self._models = models
        self._profiles = profiles
        self._tasks: set[asyncio.Task] = set()  # 照 RunSummaries 的办法：测试可 drain

    async def drain(self) -> None:
        """等全部后台任务（记账、顺手整理）收尾；测试用它消除竞态。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def provider(self, request: TurnContextRequest) -> TurnContext:
        """v0.0.6 的全有全无语义：这里绝不让异常逃出去，挂了就交空（本轮不带记忆）。"""
        try:
            return await self._build(request)
        except Exception:
            log.warning("memory context for conversation %s unavailable", request.conversation_id,
                        exc_info=True)
            return TurnContext(standing="", turn="")

    async def _build(self, request: TurnContextRequest) -> TurnContext:
        if await self._is_channel(request.user_id):
            return TurnContext(standing="", turn="")
        if (await self._service.workspace_disabled(request.workspace_id))["disabled"]:
            return TurnContext(standing="", turn="")
        setting = await self._service.setting(request.user_id)
        if not setting["enabled"]:
            return TurnContext(standing="", turn="")  # 宿主会追加「不再适用」，把旧记忆摘出上下文

        memories = await self._service.active_snapshot(request.user_id)
        standing = format_standing(memories)
        turn_text, hit_ids = await self._turn_segment(request, memories)
        if hit_ids:
            self._spawn(self._service.track_mentions(request.user_id, hit_ids))
        if datetime.now().date().isoformat() != setting["lastOrganizedAt"] and memories:
            self._spawn(self._organize_later(request))  # 每天第一次见这个人：顺手排一条整理
        return TurnContext(standing=standing, turn=turn_text)

    async def _is_channel(self, user_id: str) -> bool:
        """渠道账号没有「本人」：不注入、不提取（设计 §10）。查不到或查询失败按真人放行。"""
        if self._profiles is None:
            return False
        try:
            found = await self._profiles((user_id,))
            return getattr(found.get(user_id), "kind", "person") == "channel"
        except Exception:
            log.warning("profile lookup for %s failed; treating as person", user_id, exc_info=True)
            return False

    async def _turn_segment(self, request: TurnContextRequest,
                            memories: list[dict[str, Any]]) -> tuple[str, list[str]]:
        """本轮提问 embed 一次（交互额度）+ 余弦 ≥0.35 的前 10 条。embed 失败/没配向量模型就空着：
        standing 已保底，缺的只是这轮的精准召回。active ≤10 条时全部已在 standing 里，跳过。"""
        with_vectors = [row for row in memories if row["embedding"]]
        if self._models is None or len(memories) <= RECALL_TOP or not with_vectors:
            return "", []
        embed_alias = await _resolve(self._models, request.workspace_id)
        if embed_alias is None:
            return "", []
        try:
            [vector] = await self._models.embed(request.workspace_id, embed_alias, [request.text],
                                                background=False)
        except ModelCallError:
            log.warning("memory recall embed failed for %s", request.conversation_id, exc_info=True)
            return "", []
        scored = sorted(((cosine(vector, row["embedding"]), row) for row in with_vectors),
                        key=lambda pair: -pair[0])
        in_standing = {row["content"] for row in standing_rows(memories)}
        hits = [row for score, row in scored[:RECALL_TOP] if score >= RECALL_THRESHOLD
                and row["content"] not in in_standing]
        text = format_turn(hits)
        return (text[:TURN_BUDGET], [row["id"] for row in hits]) if text else ("", [])

    def _spawn(self, work) -> None:
        """记账与顺手整理：后台任务、吞异常，绝不拖慢也不影响本轮。"""
        async def _safe() -> None:
            try:
                await work
            except Exception:
                log.warning("memory background task failed", exc_info=True)
        task = asyncio.create_task(_safe())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _organize_later(self, request: TurnContextRequest) -> None:
        await self._service.organize(request.user_id, request.workspace_id)


async def _resolve(models: ModelClient, workspace_id: str) -> str | None:
    from .extract import EMBED_MODEL_ENV, resolve_model
    return await resolve_model(models, workspace_id, Capability.EMBEDDING, EMBED_MODEL_ENV)
