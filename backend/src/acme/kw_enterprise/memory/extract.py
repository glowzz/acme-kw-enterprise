"""提取链路的 worker 侧（设计 §5）：守卫 → 结构化抽取 → 纯规则前置 → 相似度 → LLM 判定 → 落库。

编排在这里，事务在 service.store_extract：本模块不碰 sessionmaker，只调 service 原语与 ModelClient。
LLM 预算封顶每轮 2 次（提取 1 + 判定 1，octop-memory 的每候选至多 1 次升级的纪律，候选合并进一次调用）。
"""

import json
import logging
import os
from typing import Any

from knowwork_server.jobs.context import JobContext
from knowwork_server.jobs.kinds import JobError
from knowwork_server.models.client import ModelCallError, ModelClient
from knowwork_server.models.schemas import Capability

from .dedup import cosine, negation_conflict, signature
from .models import ACTIVE_CONFIDENCE, FACT, FOCUS, PREFERENCE, QUOTE_MAX, TASK
from .service import MemoryService

log = logging.getLogger(__name__)

CHAT_MODEL_ENV = "ACME_MEMORY_CHAT_MODEL"    # 钉提取用的对话模型别名；不设取该空间第一个对话模型
EMBED_MODEL_ENV = "ACME_MEMORY_EMBED_MODEL"  # 钉向量模型别名；不设取该空间第一个嵌入模型
MIN_TEXT = 40                                # question+answer 合计下限：寒暄不进提取
MAX_CANDIDATES = 5                           # 单轮最多收几条候选：宁缺毋滥
SIMILAR_THRESHOLD = 0.82                     # 与既有记忆算相似度的门槛
SIMILAR_TOP = 3                              # 每条候选最多带几个相似项进 LLM 判定
CONTENT_MAX_CHARS = 100                      # 候选陈述的长度上限（提示词同口径）

EXTRACT_SCHEMA = {"type": "json_schema", "json_schema": {
    "name": "memory_extraction", "strict": True,
    "schema": {
        "type": "object",
        "properties": {"memories": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": [PREFERENCE, FOCUS, FACT, TASK]},
                "content": {"type": "string",
                            "description": f"一句第三人称中文陈述，不超过 {CONTENT_MAX_CHARS} 字"},
                "quote": {"type": "string",
                          "description": f"支撑这条记忆的用户原话，逐字摘录，不超过 {QUOTE_MAX} 字"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["type", "content", "quote", "confidence"],
        }}},
        "required": ["memories"],
    }}}

JUDGE_SCHEMA = {"type": "json_schema", "json_schema": {
    "name": "memory_judgement", "strict": True,
    "schema": {
        "type": "object",
        "properties": {"decisions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "候选在清单里的序号（从 0 起）"},
                "target": {"type": "string", "description": "判定的目标记忆 id"},
                "action": {"type": "string", "enum": ["noop", "update", "replace"]},
                "content": {"type": "string",
                            "description": "update/replace 时的完整新表述（不得丢失否定与限定词）"},
            },
            "required": ["index", "target", "action", "content"],
        }}},
        "required": ["decisions"],
    }}}

_EXTRACT_PROMPT = (
    "你从一段工作助手的对话里提取值得长期记住的用户记忆。只收用户明确表达的、跨会话仍有用的内容，"
    "分四类：preference（回答格式/语言/详略等偏好）、focus（长期关注的事）、fact（关于用户的事实）、"
    "task（正在办、有期限的事）。不收寒暄、一次性细节、助手自己的话、只在本轮有效的中间结论；宁缺毋滥，"
    "没有值得记的就返回空数组。content 用一句第三人称中文陈述；quote 从用户原话里逐字摘录支撑依据，"
    "否定与限定词必须原样保留（「以后不用表格」丢不得「不」字）；confidence 是这句话确实是用户意图的把握。"
)
_JUDGE_PROMPT = (
    "下面是用户的一条新候选记忆，以及他已有记忆里语义相近的几条。判定候选该怎么处理："
    "noop（同义重复，丢弃候选）、update（改写已有那条为新表述，id 不变）、replace（已有那条过时了，"
    "由候选取代）。改写不得丢失否定与限定词（「不用表格」不能改成「用表格」），拿不准就按原话直录。"
    "一个候选只给一条判定。"
)


async def resolve_model(models: ModelClient, workspace_id: str, capability: str, env_var: str) -> str | None:
    """提取/召回用的模型别名：环境变量钉死优先，否则取该空间该能力的第一个。没配返回 None。"""
    alias = os.getenv(env_var)
    if alias:
        return alias
    try:
        capable = await models.capable(workspace_id, capability)
    except Exception:
        log.warning("capable(%s) lookup failed in %s", capability, workspace_id, exc_info=True)
        return None
    return capable[0].alias if capable else None


def _job_error(error: ModelCallError) -> JobError:
    """模型调用的失败分类照抄 ModelCallError：可重试走 service 退避表，不可重试直接给终局。"""
    return JobError(f"memory_model_{error.code}", error.message, error_class="service",
                    retryable=error.retryable)


def _parse_candidates(raw: str) -> list[dict[str, Any]]:
    """模型输出 → 受控候选清单：类型白名单、长度截断、置信度钳位、条数封顶。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    memories = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(memories, list):
        return []
    allowed = {PREFERENCE, FOCUS, FACT, TASK}
    candidates: list[dict[str, Any]] = []
    for item in memories[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        kind, content, quote = item.get("type"), item.get("content"), item.get("quote")
        confidence = item.get("confidence")
        if kind not in allowed or not isinstance(content, str) or not content.strip():
            continue
        try:
            confidence = min(max(float(confidence), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        candidates.append({"type": kind,
                           # 双倍宽限：落库前 service 会按 CONTENT_MAX 再校
                           "content": content.strip()[:CONTENT_MAX_CHARS * 2],
                           "quote": quote.strip()[:QUOTE_MAX] if isinstance(quote, str) else "",
                           "confidence": confidence})
    return candidates


def _similar_targets(candidate_content: str, embedding: list[float] | None,
                     pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """与既有记忆（active+pending、有向量）算余弦，取 ≥0.82 的前 3 条。"""
    if embedding is None:
        return []
    scored = []
    for row in pool:
        score = cosine(embedding, row["embedding"])
        if score >= SIMILAR_THRESHOLD:
            scored.append({"id": row["id"], "content": row["content"], "score": score})
    scored.sort(key=lambda item: -item["score"])
    return scored[:SIMILAR_TOP]


async def extract_from_turn(service: MemoryService, models: ModelClient, user_id: str,
                            workspace_id: str, question: str, answer: str) -> dict[str, int] | None:
    """一轮对话的完整提取流程；返回落库计数（facts 用），本轮无可提取返回 None。"""
    if len(question) + len(answer) < MIN_TEXT:
        return None
    if (await service.workspace_disabled(workspace_id))["disabled"]:
        return None  # 入队后才停用的存量任务：不再跑模型（设计 §5 的复查闸）
    setting = await service.setting(user_id)
    if not setting["enabled"]:
        return None

    chat_alias = await resolve_model(models, workspace_id, Capability.CHAT, CHAT_MODEL_ENV)
    if chat_alias is None:
        log.info("memory extract skipped: no chat model in %s", workspace_id)
        return None
    try:
        raw = await models.chat(workspace_id, chat_alias,
                                [{"role": "system", "content": _EXTRACT_PROMPT},
                                 {"role": "user",
                                  "content": f"【提问】\n{question}\n\n【助手的回答】\n{answer}"}],
                                response_format=EXTRACT_SCHEMA, max_tokens=1024, background=True)
    except ModelCallError as error:
        raise _job_error(error) from error
    candidates = _parse_candidates(raw)
    if not candidates:
        return None

    pool = await service.dedup_pool(user_id)
    signatures = {signature(row["content"]) for row in pool}

    # 纯规则前置（不花 LLM）：签名重复丢弃；否定极性冲突强制待确认
    kept: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if signature(candidate["content"]) in signatures:
            continue
        conflict = any(negation_conflict(candidate["content"], row["content"]) for row in pool)
        if conflict:
            candidate["confidence"] = min(candidate["confidence"], ACTIVE_CONFIDENCE - 0.01)
        candidate["_index"] = index
        kept.append(candidate)
    if not kept:
        return {"active": 0, "pending": 0, "updated": 0, "replaced": 0, "dropped": 0}

    embed_alias = await resolve_model(models, workspace_id, Capability.EMBEDDING, EMBED_MODEL_ENV)
    if embed_alias is None:
        log.info("memory extract: no embedding model in %s; candidates go to pending", workspace_id)
    else:
        try:
            vectors = await models.embed(workspace_id, embed_alias,
                                         [candidate["content"] for candidate in kept],
                                         background=True)
        except ModelCallError as error:
            raise _job_error(error) from error
        for candidate, vector in zip(kept, vectors, strict=True):
            candidate["embedding"] = vector
            candidate["embedding_model"] = embed_alias

    # 相似命中才进 LLM 判定（合并成一次调用，预算封顶）
    for candidate in kept:
        candidate["similar"] = _similar_targets(candidate["content"], candidate.get("embedding"), pool)
    judged = [candidate for candidate in kept if candidate["similar"]]
    if judged and embed_alias is not None:
        listing = "\n\n".join(
            f"候选 {candidate['_index']}：{candidate['content']}\n相近的已有记忆：\n" + "\n".join(
                f"- {hit['id']}：{hit['content']}" for hit in candidate["similar"])
            for candidate in judged)
        try:
            raw = await models.chat(workspace_id, chat_alias,
                                    [{"role": "system", "content": _JUDGE_PROMPT},
                                     {"role": "user", "content": listing}],
                                    response_format=JUDGE_SCHEMA, max_tokens=1024, background=True)
        except ModelCallError as error:
            raise _job_error(error) from error  # 判定失败按可重试走：宁重试不静默写错
        decisions = _parse_decisions(raw)
        actions = {decision["index"]: decision for decision in decisions}
        for candidate in kept:
            decision = actions.get(candidate["_index"])
            if decision is not None:
                candidate["action"] = decision["action"]
                candidate["target"] = decision["target"]
                if decision["action"] in ("update", "replace") and decision.get("content"):
                    candidate["content"] = decision["content"][:CONTENT_MAX_CHARS * 2]

    return await service.store_extract(user_id, workspace_id, kept)


def _parse_decisions(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    decisions = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, dict) or item.get("action") not in ("noop", "update", "replace"):
            continue
        parsed.append({"index": item.get("index"), "target": item.get("target"),
                       "action": item["action"], "content": item.get("content") or ""})
    return parsed


def build_extract_handler(service: MemoryService, models: ModelClient):
    """worker 的 memory.extract handler：守卫与守卫间的差异都收敛到 extract_from_turn。"""

    async def handle(job_ctx: JobContext) -> dict[str, int] | None:
        payload = job_ctx.payload
        result = await extract_from_turn(service, models, payload["user_id"], job_ctx.workspace_id,
                                         payload.get("question", ""), payload.get("answer", ""))
        if result:
            log.info("memory extract for %s turn %s: %s", payload.get("conversation_id"),
                     payload.get("turn"), result)
        return result

    return handle


async def log_give_up(session, claim, failure) -> None:
    """终局收尾：只记日志——注入不依赖提取，失败的轮次用户无感。"""
    log.warning("memory extract gave up for %s (%s): %s", claim.subject_id,
                failure.code, failure.message)


def build_contributor(service: MemoryService, profiles):
    """on_turn_ended 的贡献者（api 进程）：守卫过后排提取任务，返回 facts（运行记录可按它筛选）。
    任何异常都吞掉返回 None——观察者链上别人不该被记忆拖累。"""

    async def contribute(ended) -> dict[str, Any] | None:
        try:
            if ended.user_id is None or await _is_channel(profiles, ended.user_id):
                return None
            if (await service.workspace_disabled(ended.workspace_id))["disabled"]:
                return None
            if not (await service.setting(ended.user_id))["enabled"]:
                return None
            await service.enqueue_extract(ended.user_id, ended.workspace_id, ended.conversation_id,
                                          ended.turn, ended.question, ended.answer)
            return {"queued": True}
        except Exception:
            log.warning("memory extract enqueue for %s failed", ended.conversation_id, exc_info=True)
            return None

    return contribute


async def _is_channel(profiles, user_id: str) -> bool:
    """与 context.MemoryContext._is_channel 同口径：渠道账号没有「本人」，不提取。"""
    if profiles is None:
        return False
    try:
        found = await profiles((user_id,))
        return getattr(found.get(user_id), "kind", "person") == "channel"
    except Exception:
        return False
