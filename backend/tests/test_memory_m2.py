"""M2 的测试：提取链路（假 ModelClient 脚本化返回 + 真 PG）、注入提供者（守卫/降级/确定性格式）。

假 ModelClient 只实现 extract/context 用到的方法：capable、chat、embed。
跑法同 test_memory.py：cd KnowWork-stable/server && uv run pytest ../../acme-kw-enterprise/backend/tests
"""

import uuid
from datetime import datetime

import pytest
from knowwork_server.identity.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acme.kw_enterprise.memory.context import MemoryContext, format_standing, format_turn
from acme.kw_enterprise.memory.extract import build_extract_handler, extract_from_turn
from acme.kw_enterprise.memory.models import ACTIVE, FACT, PENDING, PREFERENCE, TASK, Memory
from acme.kw_enterprise.memory.service import MemoryService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- 假件 ----


class FakeModels:
    """ModelClient 的假件：chat 按队列弹响应，embed 返回可预测向量，capable 报告别名。"""

    def __init__(self, chat_responses: list[str], embed_dim: int = 4) -> None:
        self.chat_responses = list(chat_responses)
        self.embed_dim = embed_dim
        self.chats: list[dict] = []
        self.embeds: list[list[str]] = []

    async def capable(self, workspace_id: str, capability: str) -> tuple:
        return (type("M", (), {"alias": f"{capability}-alias"}),)

    async def chat(self, workspace_id: str, alias: str, messages, *, response_format=None,
                   max_tokens: int = 1024, background: bool = True, **kwargs) -> str:
        self.chats.append({"messages": messages})
        if not self.chat_responses:
            raise AssertionError("chat 调用次数超出脚本")
        return self.chat_responses.pop(0)

    async def embed(self, workspace_id: str, alias: str, texts, *, background: bool = True):
        self.embeds.append(list(texts))
        # 可预测向量：文本里不同字母决定不同维度，同文本同向量
        return [[float(text.count(chr(ord("a") + i)) % 7 + 1) for i in range(self.embed_dim)]
                for text in texts]


class FakeAccess:
    async def check(self, user_id: str, workspace_id: str, resource: str, action: str) -> bool:
        return True


class Profile:
    def __init__(self, kind: str = "person") -> None:
        self.kind = kind


class FakeProfiles:
    def __init__(self, kinds: dict[str, str] | None = None) -> None:
        self.kinds = kinds or {}

    async def __call__(self, user_ids):
        return {uid: Profile(self.kinds.get(uid, "person")) for uid in user_ids}


# ---- 纯格式函数（无 DB） ----


def test_format_standing_is_deterministic_and_ordered():
    rows = [{"id": "mem_b", "type": FACT, "content": "事实乙", "score": 0.9, "expiresAt": None},
            {"id": "mem_a", "type": FACT, "content": "事实甲", "score": 0.9, "expiresAt": None},
            {"id": "mem_c", "type": PREFERENCE, "content": "回答用表格", "score": 1.0, "expiresAt": None},
            {"id": "mem_d", "type": TASK, "content": "核对 Q3 净收入", "score": 1.0,
             "expiresAt": datetime(2026, 12, 1)}]
    text = format_standing(rows)
    assert text.startswith("以下是关于这位用户的长期记忆")
    assert text.index("回答用表格") < text.index("核对 Q3"), "偏好先于在办"
    assert text.index("事实甲") < text.index("事实乙"), "组内 score 相同按 id 升序，两次生成完全一致"
    assert "（12-01 前）" in text and format_standing(rows) == text


def test_format_standing_budget_drops_whole_lines():
    rows = [{"id": f"mem_{i}", "type": FACT, "content": "很" * 300, "score": 1.0 - i * 0.01,
             "expiresAt": None} for i in range(20)]
    text = format_standing(rows)
    assert len(text) <= 2500 and text.count("【事实】") < 20, "超预算丢整行不截半句"


def test_format_turn_empty_when_no_hits():
    assert format_turn([]) == "" and format_turn([{"content": "x"}]).startswith("【相关记忆】")


# ---- 提取链路（真 PG + 假模型） ----


@pytest.fixture(scope="module")
def pg_url():
    import shutil
    import tempfile

    import pgserver

    data_dir = tempfile.mkdtemp(prefix="kw-mem2-pg-")
    server = pgserver.get_server(data_dir)
    socket_dir = server.get_uri().split("host=")[1]
    yield f"postgresql+asyncpg://postgres@/postgres?host={socket_dir}"
    server.cleanup()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
async def sessionmaker(pg_url):
    import knowwork_server.jobs.models  # noqa: F401  job 表（顺手整理入队要用）
    from knowwork_server.db.base import Base
    from test_memory import _create_database, _drop_database  # 复用 M1 的建库/删库工具

    name = f"mem2_{uuid.uuid4().hex[:12]}"
    url = await _create_database(pg_url, name)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(User(id="u1", email="u1@example.com"))
    yield maker
    await engine.dispose()
    await _drop_database(pg_url, name)


async def _seed(sessionmaker, user_id: str, *, type: str = FACT, content: str = "占位内容",
                **kwargs) -> str:
    memory_id = f"mem_{uuid.uuid4().hex}"
    async with sessionmaker() as session, session.begin():
        session.add(Memory(id=memory_id, user_id=user_id, type=type, content=content, **kwargs))
    return memory_id


def _extract_reply(*items: dict) -> str:
    import json
    return json.dumps({"memories": [dict({"type": FACT, "quote": "原话", "confidence": 0.9}, **item)
                                    for item in items]})


async def test_extract_happy_path_inserts_active(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    models = FakeModels([_extract_reply({"content": "回答用表格"})])
    counts = await extract_from_turn(service, models, "u1", "ws1", "以后回答都用表格形式",
                                     "好的，我会用表格回答" + "，内容补足长度" * 5)
    assert counts == {"active": 1, "pending": 0, "updated": 0, "replaced": 0, "dropped": 0}
    items, _ = await service.page("u1", status=ACTIVE)
    assert items[0]["content"] == "回答用表格" and items[0]["quote"] == "原话"
    assert items[0]["source"] == "extracted" and items[0]["originWorkspaceId"] == "ws1"
    assert len(models.embeds) == 1  # 候选算过向量


async def test_extract_signature_dedup_drops_duplicate(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    models = FakeModels([_extract_reply({"content": "回答 用表格"})])  # 归一化后同签名
    counts = await extract_from_turn(service, models, "u1", "ws1", "回答用表格" + "，请记住了" * 4,
                                     "好的" + "，我会照做" * 5)
    assert counts == {"active": 0, "pending": 0, "updated": 0, "replaced": 0,
                      "dropped": 1}  # 同义重复计入 dropped


async def test_extract_negation_conflict_goes_pending(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=PREFERENCE, content="回答用表格输出")
    models = FakeModels([_extract_reply({"content": "回答不用表格输出", "confidence": 0.95})])
    counts = await extract_from_turn(service, models, "u1", "ws1", "以后不要用表格输出" + "，注意" * 5,
                                     "明白" + "，我会改" * 5)
    assert counts["pending"] == 1 and counts["active"] == 0  # 眼见为实的矛盾不直接生效


async def test_extract_low_confidence_without_quote_goes_pending(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    models = FakeModels([_extract_reply({"content": "喜欢简洁回答", "quote": "", "confidence": 0.9})])
    counts = await extract_from_turn(service, models, "u1", "ws1", "我喜欢简洁一点" + "的回答" * 5,
                                     "好的" + "，我会简洁" * 5)
    assert counts["pending"] == 1  # 证据不全不直接生效，也不丢弃


async def test_extract_judge_updates_existing(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    old_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格",
                         embedding=[6.0, 0.0, 0.0, 0.0])
    # 候选内容带 5 个 'a'：假 embed 映成 [6,1,1,1]，与旧向量余弦 ≈0.96 过相似门槛 → 进 LLM 判定
    import json
    candidate = {"type": PREFERENCE, "content": "回答用表格呈现 aaaaa", "quote": "原话", "confidence": 0.9}
    models = FakeModels([json.dumps({"memories": [candidate]}),
                         json.dumps({"decisions": [{"index": 0, "target": old_id,
                                                    "action": "update",
                                                    "content": "回答统一用表格，不要长段落"}]})])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答用表格 aaaaa" + "，保持这样" * 5, "好的" + "，明白" * 5)
    assert counts["updated"] == 1
    async with sessionmaker() as session:
        row = await session.get(Memory, old_id)
        assert row is not None and row.content == "回答统一用表格，不要长段落"
        assert row.embedding is not None and row.mention_count == 1  # 向量按候选换、记账 +1


async def test_extract_skips_guarded_and_short(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    models = FakeModels([])
    assert await extract_from_turn(service, models, "u1", "ws1", "你好", "你好呀") is None  # 太短

    class _NoModels(FakeModels):  # 空间没配对话模型
        async def capable(self, *a, **k):
            return ()

    long_q, long_a = "问题" + "。" * 50, "回答" + "。" * 50
    assert await extract_from_turn(service, _NoModels([]), "u1", "ws1", long_q, long_a) is None
    await service.set_setting("u1", False)
    assert await extract_from_turn(service, models, "u1", "ws1", long_q, long_a) is None  # 开关关
    assert not models.chats


async def test_worker_handler_reads_payload(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    handler = build_extract_handler(service, FakeModels([_extract_reply({"content": "喜欢深色主题"})]))
    # M3 起 handler 返回 None（Done 契约）：落库结果以记忆表为准
    result = await handler(_JobContext({"user_id": "u1", "conversation_id": "c1", "turn": 3,
                                        "question": "换成深色主题吧，" + "界面太刺眼了，" * 6,
                                        "answer": "已切换到深色主题，" + "后续回答都会按它来，" * 6}))
    assert result is None
    items, _ = await service.page("u1", status="active")
    assert any(item["content"] == "喜欢深色主题" for item in items)


class _JobContext:
    """JobContext 的假件：handler 只用 workspace_id 与 payload。"""

    def __init__(self, payload: dict) -> None:
        self.workspace_id = "ws1"
        self.payload = payload


# ---- 提供者（守卫/降级/turn 召回） ----


class _Request:
    def __init__(self, user_id="u1", workspace_id="ws1", text="下一轮的提问", conversation_id="c1"):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.text = text
        self.conversation_id = conversation_id


async def test_provider_builds_standing(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    context = MemoryContext(service, None, None)
    turn = await context.provider(_Request())
    assert "回答用表格" in turn.standing and turn.turn == ""  # 没配模型：turn 段空但不报错


async def test_provider_guards(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    context = MemoryContext(service, None, FakeProfiles({"bot": "channel"}))
    assert (await context.provider(_Request(user_id="bot"))).standing == ""  # 渠道账号不带
    await service.set_setting("u1", False)
    assert (await context.provider(_Request())).standing == ""  # 个人开关关：空（宿主会发「不再适用」）
    await service.set_setting("u1", True)
    await service.set_workspace_setting("u1", "ws1", True)
    assert (await context.provider(_Request())).standing == ""  # 空间停用整条链路停


async def test_provider_degrades_on_model_error(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=PREFERENCE, content="回答用表格")

    class _Boom(FakeModels):
        async def embed(self, *a, **k):
            raise RuntimeError("embed 服务挂了")

    context = MemoryContext(service, _Boom([]), None)
    turn = await context.provider(_Request(text="问" * 40))
    assert "回答用表格" in turn.standing and turn.turn == ""  # 自己降级：standing 照常、turn 放弃


async def test_turn_segment_recalls_and_tracks(sessionmaker):
    service = MemoryService(sessionmaker, FakeAccess())
    # 目标是 fact 且要被挤出 standing（事实只取前 20）：先造 20 条高分的凑数，再放目标（score 0.5）
    for index in range(20):
        await _seed(sessionmaker, "u1", status=ACTIVE, content=f"凑数 {index:02d}", score=1.0)
    row_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="aaaa 核对 Q3 净收入",
                         score=0.5, embedding=[6.0, 0.0, 0.0, 0.0])
    models = FakeModels([], embed_dim=4)
    context = MemoryContext(service, models, None)
    # 提问的假向量是 'a' 计数：文本里 18 个 'a' → [5,1,1,1]，与目标 [6,0,0,0] 余弦 ≈0.94
    turn = await context.provider(_Request(text="aaaaaaaaaaaaaaaaaa"))
    assert turn.turn != "" and "核对 Q3" in turn.turn and len(turn.turn) <= 1500
    await context.drain()  # 记账是后台任务：等它收尾再查
    async with sessionmaker() as session:
        row = await session.get(Memory, row_id)
        assert row is not None and row.mention_count == 1 and row.last_mentioned_at is not None


async def test_organize_enqueued_when_due(sessionmaker):
    from knowwork_server.jobs.models import Job

    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=FACT, content="占位" + "内容" * 5)
    context = MemoryContext(service, None, None)
    await context.provider(_Request())
    await context.drain()  # 顺手整理也是后台任务
    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job).where(Job.kind == "memory.organize"))).scalars().all()
    assert len(jobs) == 1 and jobs[0].payload["user_id"] == "u1"  # 当天没整理过：顺手排一条


# ---- R2 审查意见的回归用例 ----


async def test_organize_enqueued_once_per_day(sessionmaker):
    """H1：日期比较是真比较——当天第二轮回不再排任务（入队成功即写标记）。"""
    service = MemoryService(sessionmaker, FakeAccess())
    await service.add("u1", type=FACT, content="占位" + "内容" * 5)
    context = MemoryContext(service, None, None)
    await context.provider(_Request())
    await context.drain()
    await context.provider(_Request())
    await context.drain()
    from knowwork_server.jobs.models import Job

    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job).where(Job.kind == "memory.organize"))).scalars().all()
    assert len(jobs) == 1
    assert (await service.last_organized_on("u1")) is not None  # 标记已写，当天不再触发


async def test_judge_slots_map_by_display_order(sessionmaker):
    """H2：两个候选进判定，模型按清单槽位回——判定必须套回各自的候选，不许错位。"""
    service = MemoryService(sessionmaker, FakeAccess())
    old_a = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格",
                        embedding=[6.0, 0.0, 0.0, 0.0])
    old_b = await _seed(sessionmaker, "u1", status=ACTIVE, content="称呼用中文姓名",
                        embedding=[0.0, 6.0, 0.0, 0.0])
    # 候选 1（'a' 计数 5 → [6,1,1,1]）相似 old_a；候选 2（'b' 计数 5 → [1,6,1,1]）相似 old_b
    import json
    models = FakeModels([
        json.dumps({"memories": [
            {"type": PREFERENCE, "content": "回答用表格呈现 aaaaa", "quote": "原话A", "confidence": 0.9},
            {"type": PREFERENCE, "content": "称呼用中文姓名 bbbbb", "quote": "原话B", "confidence": 0.9},
        ]}),
        json.dumps({"decisions": [
            {"index": 1, "target": old_b, "action": "update", "content": "称呼一律用中文姓名"},
            {"index": 0, "target": old_a, "action": "update", "content": "回答统一用表格，不要长段落"},
        ]}),  # 刻意反着回：错位的话 old_a 会被改成「称呼」那句
    ])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答用表格 aaaaa，称呼用中文 bbbbb" + "，记住了" * 5,
                                     "好的" + "，都记下了" * 5)
    assert counts["updated"] == 2
    async with sessionmaker() as session:
        a, b = await session.get(Memory, old_a), await session.get(Memory, old_b)
        assert a is not None and b is not None
        assert a.content == "回答统一用表格，不要长段落" and b.content == "称呼一律用中文姓名"


async def test_replace_with_low_confidence_keeps_old_active(sessionmaker):
    """H3：replace 但候选没生效（极性冲突钳位）→ 旧行不动、新行待确认并暂存对手指针；
    确认新行时旧行才降 SUPERSEDED——矛盾对不会从注入里凭空消失。"""
    service = MemoryService(sessionmaker, FakeAccess())
    old_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格输出",
                         embedding=[6.0, 0.0, 0.0, 0.0])
    import json
    models = FakeModels([
        json.dumps({"memories": [
            {"type": PREFERENCE, "content": "回答不用表格输出 aaaaa", "quote": "原话", "confidence": 0.95}]}),
        json.dumps({"decisions": [{"index": 0, "target": old_id, "action": "replace",
                                   "content": "回答不用表格输出"}]}),  # 判定 replace：但候选被钳位落 pending
    ])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "以后不要用表格输出 aaaaa" + "，改掉" * 5, "明白" + "，改" * 5)
    assert counts["pending"] == 1 and counts["replaced"] == 0
    async with sessionmaker() as session:
        old = await session.get(Memory, old_id)
        assert old is not None and old.status == ACTIVE  # 旧行还生效
        pending = (await session.execute(select(Memory).where(Memory.status == PENDING))).scalar_one()
        assert pending.superseded_by == old_id  # 对手指针暂存
    await service.confirm("u1", pending.id)  # 确认时才真正取代
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[pending.id] == ACTIVE and statuses[old_id] == "superseded"


async def test_overlong_candidate_dropped_without_killing_batch(sessionmaker):
    """H4：一条超长候选丢弃本行，同批正常候选照常落库——不许炸事务进重试。"""
    service = MemoryService(sessionmaker, FakeAccess())
    models = FakeModels([_extract_reply({"content": "正常的一条偏好", "confidence": 0.9},
                                        {"content": "超" * 600, "confidence": 0.9})])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "我喜欢简洁回答，" + "就这样保持，" * 5,
                                     "好的，我会简洁" + "，不再啰嗦" * 5)
    assert counts["dropped"] == 1 and counts["active"] == 1  # 好候选没被连累


async def test_judge_failure_degrades_to_pending(sessionmaker):
    """H5：判定失败不重试（重付提取），带相似命中的候选直接进待确认——宁待确认不静默写错。"""
    service = MemoryService(sessionmaker, FakeAccess())
    await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格", embedding=[6.0, 0.0, 0.0, 0.0])

    class _JudgeBoom(FakeModels):
        async def chat(self, workspace_id, alias, messages, **kwargs):
            schema = (kwargs.get("response_format") or {}).get("json_schema") or {}
            if schema.get("name") == "memory_judgement":
                from knowwork_server.models.client import ModelCallError

                raise ModelCallError("model_rate_limited", "限流", retryable=True)
            return await super().chat(workspace_id, alias, messages, **kwargs)

    import json
    models = _JudgeBoom([json.dumps({"memories": [
        {"type": PREFERENCE, "content": "回答用表格呈现 aaaaa", "quote": "原话", "confidence": 0.9}]})])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答用表格 aaaaa" + "，保持" * 5, "好的" + "，明白" * 5)
    assert counts["pending"] == 1 and counts["active"] == 0  # 有相似命中的候选降档进待确认


async def test_update_target_user_source_forced_noop(sessionmaker):
    """M4：人工优先——手动添加的记忆不被系统改写：判定 update 也强制 noop（候选丢弃）。"""
    service = MemoryService(sessionmaker, FakeAccess())
    old_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格",
                         embedding=[6.0, 0.0, 0.0, 0.0], source="user")
    import json
    models = FakeModels([json.dumps({"memories": [
        {"type": PREFERENCE, "content": "回答用表格呈现 aaaaa", "quote": "原话", "confidence": 0.9}]}),
        json.dumps({"decisions": [{"index": 0, "target": old_id, "action": "update",
                                   "content": "改写手动添加的那条"}]})])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答用表格 aaaaa" + "，保持" * 5, "好的" + "，明白" * 5)
    assert counts == {"active": 0, "pending": 0, "updated": 0, "replaced": 0,
                      "dropped": 1}  # 对手动行的同义重复：候选丢弃且计数可见
    async with sessionmaker() as session:
        row = await session.get(Memory, old_id)
        assert row is not None and row.content == "回答用表格"  # 手动行原样


async def test_extract_score_records_confidence(sessionmaker):
    """M6：score 记提取置信度——注入排序与整理衰减才有真实起点。"""
    service = MemoryService(sessionmaker, FakeAccess())
    models = FakeModels([_extract_reply({"content": "喜欢简洁", "confidence": 0.85})])
    await extract_from_turn(service, models, "u1", "ws1", "我喜欢简洁的回答，" + "别写长段落，" * 5,
                            "好的，我会简洁" + "，不写长段落" * 5)
    items, _ = await service.page("u1", status=ACTIVE)
    assert items[0]["score"] == 0.85


async def test_contributor_skips_failed_turns(sessionmaker):
    """M5：失败/中断的轮次不进提取——残缺文本是错记忆的直接来源。"""
    service = MemoryService(sessionmaker, FakeAccess())
    from acme.kw_enterprise.memory.extract import build_contributor

    contributor = build_contributor(service, None)
    assert await contributor(_TurnEnded("failed")) is None
    assert await contributor(_TurnEnded("completed")) == {"queued": True}


class _TurnEnded:
    def __init__(self, status: str) -> None:
        self.workspace_id = "ws1"
        self.conversation_id = "c1"
        self.turn = 1
        self.question = "问题" + "。" * 40
        self.answer = "回答" + "。" * 40
        self.status = status
        self.user_id = "u1"


async def test_provider_degrades_real_embed_failure(sessionmaker):
    """M2：embed 炸（任何异常）只丢 turn 段，standing 照常——假通过的老测试换成真形状。"""
    service = MemoryService(sessionmaker, FakeAccess())
    for index in range(12):  # >10 条且有向量：让 embed 真被调用
        await _seed(sessionmaker, "u1", status=ACTIVE, content=f"凑数 {index:02d} aaaa",
                    embedding=[1.0, 1.0, 1.0, 1.0])
    context = MemoryContext(service, None, None)
    turn = await context.provider(_Request())
    assert turn.standing != ""  # 没配模型：standing 保底

    class _Boom(FakeModels):
        async def embed(self, *a, **k):
            raise RuntimeError("embed 服务挂了")

    boom = MemoryContext(service, _Boom([], embed_dim=4), None)
    turn = await boom.provider(_Request())
    assert turn.standing != "" and turn.turn == ""  # 自己降级：standing 保底、turn 放弃


# ---- R3 确认轮：R2 新增三条判定分支的回归 ----


async def test_judge_noop_drops_duplicate(sessionmaker):
    """noop 判定＝丢弃候选：同义重复不许以高置信落 ACTIVE（R3 之前判重闭环被打穿）。"""
    service = MemoryService(sessionmaker, FakeAccess())
    await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格", embedding=[6.0, 0.0, 0.0, 0.0])
    import json
    models = FakeModels([
        json.dumps({"memories": [
            {"type": PREFERENCE, "content": "回答用表格呈现 aaaaa", "quote": "原话", "confidence": 0.9}]}),
        json.dumps({"decisions": [{"index": 0, "target": "占位", "action": "noop",
                                   "content": "回答用表格呈现 aaaaa"}]}),
    ])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答用表格 aaaaa" + "，保持" * 5, "好的" + "，明白" * 5)
    assert counts == {"active": 0, "pending": 0, "updated": 0, "replaced": 0, "dropped": 1}


async def test_conflict_clamped_candidate_rejects_update(sessionmaker):
    """矛盾候选只禁 update：判定给 update 也打回 insert——极性不许被原地改写翻转。"""
    service = MemoryService(sessionmaker, FakeAccess())
    old_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格输出",
                         embedding=[6.0, 0.0, 0.0, 0.0])
    import json
    models = FakeModels([
        json.dumps({"memories": [
            {"type": PREFERENCE, "content": "回答不用表格输出 aaaaa", "quote": "原话", "confidence": 0.9}]}),
        json.dumps({"decisions": [{"index": 0, "target": old_id, "action": "update",
                                   "content": "回答用表格输出"}]}),  # 判定居然说改写成肯定句：必须被拦下
    ])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "以后不要用表格输出 aaaaa" + "，改掉" * 5, "明白" + "，改" * 5)
    assert counts["updated"] == 0 and counts["pending"] == 1  # 候选落待确认，旧行原样
    async with sessionmaker() as session:
        row = await session.get(Memory, old_id)
        assert row is not None and row.content == "回答用表格输出" and row.status == ACTIVE
        pending = (await session.execute(select(Memory).where(Memory.status == PENDING))).scalar_one()
        assert "不用" in pending.content  # pending 行保住候选原话的极性，不是判定给的翻转文本


async def test_diluted_rewrite_reverts_to_pending(sessionmaker):
    """防稀释复核：旧句带否定、改写丢了否定（「不用表格」→「用表格」）→ 撤销改写转待确认。"""
    service = MemoryService(sessionmaker, FakeAccess())
    old_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="回答不用表格输出",
                         embedding=[6.0, 0.0, 0.0, 0.0])
    import json
    models = FakeModels([
        json.dumps({"memories": [
            {"type": PREFERENCE, "content": "回答不用表格输出 aaaaa", "quote": "原话", "confidence": 0.9}]}),
        json.dumps({"decisions": [{"index": 0, "target": old_id, "action": "update",
                                   "content": "回答用表格输出"}]}),  # 丢了「不」：稀释改写
    ])
    counts = await extract_from_turn(service, models, "u1", "ws1",
                                     "回答不用表格输出 aaaaa" + "，保持" * 5, "好的" + "，明白" * 5)
    assert counts["updated"] == 0 and counts["pending"] == 1
    async with sessionmaker() as session:
        row = await session.get(Memory, old_id)
        assert row is not None and row.content == "回答不用表格输出"  # 原句保留
        pending = (await session.execute(select(Memory).where(Memory.status == PENDING))).scalar_one()
        assert "不用" in pending.content  # 撤销改写后候选恢复原话口径，不是丢「不」的判定文本
