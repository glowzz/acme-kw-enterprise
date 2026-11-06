"""整理链路的 worker 侧（设计 §7）：过期清理 → 合并重复 → 降权 → 腾位 → 补向量 → 写标记。

固定顺序、每步独立事务；LLM 只花在合并判定上（一次调用，≤20 对）。人工优先：合并/降权/腾位
只动 source=extracted（手动添加与「记住」写入的只能本人改删）。workspace_id 只为模型路由，
数据不按空间筛。载荷 {"user_id", "workspace_id"}。
"""

import json
import logging
from typing import Any

from knowwork_server.jobs.context import JobContext
from knowwork_server.models.client import ModelCallError, ModelClient
from knowwork_server.models.schemas import Capability

from .extract import CHAT_MODEL_ENV, EMBED_MODEL_ENV, resolve_model
from .service import MemoryService

log = logging.getLogger(__name__)

MERGE_SCHEMA = {"type": "json_schema", "json_schema": {
    "name": "memory_merge", "strict": True,
    "schema": {
        "type": "object",
        "properties": {"decisions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "pair": {"type": "integer", "description": "对子的序号（从 0 起）"},
                "action": {"type": "string", "enum": ["merge", "keep"]},
                "content": {"type": "string",
                            "description": "merge 时两句话合并成的一句第三人称陈述（不得丢失否定与限定词）"},
            },
            "required": ["pair", "action", "content"],
        }}},
        "required": ["decisions"],
    }}}

_MERGE_PROMPT = (
    "下面每对都是同一位用户的既有记忆，语义高度相近（余弦 ≥0.90）。判定每对是 merge 还是 keep："
    "merge 时给一句合并两句话信息量的第三人称中文陈述（不得丢失否定与限定词）；"
    "信息重复或只是措辞差异时也合并；信息确实互补且合并会丢细节时 keep。"
)


async def organize_user(service: MemoryService, models: ModelClient | None,
                        user_id: str, workspace_id: str) -> dict[str, Any]:
    """一个人的完整整理；返回各步计数（日志与排查用）。"""
    if (await service.workspace_disabled(workspace_id))["disabled"]:
        log.info("memory organize skipped: workspace %s disabled", workspace_id)
        return {"skipped": "workspace_disabled"}
    if not (await service.setting(user_id))["enabled"]:
        log.info("memory organize skipped: personal memory disabled for %s", user_id)
        return {"skipped": "personal_disabled"}

    counts: dict[str, Any] = {}
    counts["expired"] = await service.archive_expired(user_id)
    last_organized_at = (await service.setting(user_id))["lastOrganizedAt"]

    pairs = await service.merge_candidates(user_id)
    merges: list[dict[str, Any]] = []
    chat_alias = None
    if pairs and models is not None:
        chat_alias = await resolve_model(models, workspace_id, Capability.CHAT, CHAT_MODEL_ENV)
    if pairs and chat_alias:
        for slot, pair in enumerate(pairs):
            pair["_slot"] = slot
        listing = "\n".join(
            f"第 {pair['_slot']} 对：A(id={pair['a_id']})：{pair['a_content']}"
            f" ／ B(id={pair['b_id']})：{pair['b_content']}"
            for pair in pairs)
        try:
            raw = await models.chat(workspace_id, chat_alias,
                                    [{"role": "system", "content": _MERGE_PROMPT},
                                     {"role": "user", "content": listing}],
                                    response_format=MERGE_SCHEMA, max_tokens=1024, background=True)
            for decision in _parse_merges(raw):
                pair = next((p for p in pairs if p["_slot"] == decision["pair"]), None)
                content = (decision.get("content") or "").strip()
                if pair is None or decision["action"] != "merge":
                    continue
                if not 1 <= len(content) <= 500:
                    continue  # 空白/超长的合并表述跳过，别让 _content_of 炸整批（R2 前的 P3）
                merges.append({"keep_id": pair["a_id"], "drop_id": pair["b_id"], "content": content})
        except ModelCallError as error:
            log.warning("memory organize: merge judge failed (%s); pairs kept as-is", error.code)
    counts["merged"] = await service.apply_merges(user_id, merges) if merges else 0
    counts["pairs"] = len(pairs)

    counts["decayed_archived"] = await service.decay_scores(user_id, last_organized_at)
    counts["trimmed"] = await service.trim_active(user_id)

    embed_alias = await resolve_model(models, workspace_id, Capability.EMBEDDING, EMBED_MODEL_ENV) \
        if models is not None else None
    counts["revectored"] = 0
    if embed_alias and models is not None:
        stale = await service.stale_vector_rows(user_id, embed_alias)
        for start in range(0, len(stale), 32):  # ModelClient.embed 自带 32 条分批，这里按清单切
            batch = stale[start:start + 32]
            try:
                vectors = await models.embed(workspace_id, embed_alias,
                                             [item["content"] for item in batch], background=True)
            except ModelCallError as error:
                log.warning("memory organize: revector failed (%s); lazy retry next day", error.code)
                break
            await service.store_vectors(user_id, embed_alias,
                                        [{"id": item["id"], "content": item["content"],
                                          "embedding": vector}
                                         for item, vector in zip(batch, vectors, strict=True)])
            counts["revectored"] += len(batch)

    await service.mark_organized(user_id)
    return counts


def _parse_merges(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    decisions = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, dict) or item.get("action") not in ("merge", "keep"):
            continue
        parsed.append({"pair": item.get("pair"), "action": item["action"],
                       "content": item.get("content") or ""})
    return parsed


def build_organize_handler(service: MemoryService, models: ModelClient | None):
    async def handle(job_ctx: JobContext) -> None:
        payload = job_ctx.payload
        result = await organize_user(service, models, payload["user_id"], job_ctx.workspace_id)
        log.info("memory organize for %s via %s: %s", payload["user_id"], job_ctx.workspace_id, result)

    return handle


async def log_give_up(session, claim, failure) -> None:
    log.warning("memory organize gave up for %s (%s): %s", claim.subject_id,
                failure.code, failure.message)

