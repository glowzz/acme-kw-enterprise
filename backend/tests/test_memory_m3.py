"""M3 的测试：「记住」工具（判重/落库/开关）与整理六步（过期/合并/降权/腾位/补向量/标记）。

跑法同 test_memory.py：cd KnowWork-stable/server && uv run pytest ../../acme-kw-enterprise/backend/tests
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from knowwork_server.identity.models import User
from knowwork_server.tools.registry import ToolFailure
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acme.kw_enterprise.memory import tool as memory_tool
from acme.kw_enterprise.memory.models import (
    ACTIVE,
    ACTIVE_CAP,
    BY_USER,
    EXTRACTED,
    FACT,
    PENDING,
    PREFERENCE,
    TASK,
    Memory,
)
from acme.kw_enterprise.memory.organize import organize_user
from acme.kw_enterprise.memory.service import MemoryService
from acme.kw_enterprise.memory.tool import build_remember_tool

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def pg_url():
    import shutil
    import tempfile

    import pgserver

    data_dir = tempfile.mkdtemp(prefix="kw-mem3-pg-")
    server = pgserver.get_server(data_dir)
    socket_dir = server.get_uri().split("host=")[1]
    yield f"postgresql+asyncpg://postgres@/postgres?host={socket_dir}"
    server.cleanup()
    shutil.rmtree(data_dir, ignore_errors=True)


async def _create_database(pg_url: str, name: str) -> str:
    from sqlalchemy import text

    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    return pg_url.replace("/postgres?", f"/{name}?")


async def _drop_database(pg_url: str, name: str) -> None:
    from sqlalchemy import text

    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest.fixture
async def sessionmaker(pg_url):
    import knowwork_server.jobs.models  # noqa: F401
    from knowwork_server.db.base import Base
    from test_memory import _create_database, _drop_database

    name = f"mem3_{uuid.uuid4().hex[:12]}"
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


class FakeAccess:
    def check(self, user_id: str, workspace_id: str, resource: str, action: str) -> bool:
        return True


class FakeModels:
    def __init__(self, embed_dim: int = 4) -> None:
        self.embed_dim = embed_dim

    async def capable(self, workspace_id: str, capability: str) -> tuple:
        return (type("M", (), {"alias": f"{capability}-alias"}),)

    async def chat(self, workspace_id: str, alias: str, messages, **kwargs) -> str:
        return '{"decisions": []}'

    async def embed(self, workspace_id: str, alias: str, texts, *, background: bool = True):
        return [[float(text.count(chr(ord("a") + i)) % 7 + 1) for i in range(self.embed_dim)]
                for text in texts]


@pytest.fixture
def service(sessionmaker):
    return MemoryService(sessionmaker, FakeAccess())


class _Ctx:
    def __init__(self, user_id="u1", workspace_id="ws1"):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_id = "s1"
        self.agent_session_id = None


async def _seed(sessionmaker, user_id: str, *, type: str = FACT, content: str = "占位内容",
                **kwargs) -> str:
    memory_id = f"mem_{uuid.uuid4().hex}"
    async with sessionmaker() as session, session.begin():
        session.add(Memory(id=memory_id, user_id=user_id, type=type, content=content, **kwargs))
    return memory_id


# ---- remember 工具 ----


async def test_remember_inserts_active(sessionmaker, service):
    tool, _ = build_remember_tool(service, FakeModels())
    result = await tool.handler(_Ctx(), {"content": "回答用表格", "type": "preference"})
    assert result.startswith("已记住：")
    items, _ = await service.page("u1", status=ACTIVE)
    assert items[0]["content"] == "回答用表格" and items[0]["source"] == "tool"
    assert items[0]["originWorkspaceId"] == "ws1"


async def test_remember_signature_duplicate_updates(sessionmaker, service):
    tool, _ = build_remember_tool(service, FakeModels())
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    result = await tool.handler(_Ctx(), {"content": "回答 用表格"})  # 归一化同签名
    assert result.startswith("已记住（更新了原来的说法）")
    items, total = await service.page("u1", status=ACTIVE)
    assert total == 1 and items[0]["content"] == "回答 用表格"  # 新表述为准，不重复落库


async def test_remember_vector_duplicate_updates(sessionmaker, service):
    tool, _ = build_remember_tool(service, FakeModels())
    await _seed(sessionmaker, "u1", status=ACTIVE, content="回答用表格",
                embedding=[6.0, 0.0, 0.0, 0.0])
    await tool.handler(_Ctx(), {"content": "aaaaa 回答都用表格"})  # 假向量 [6,1,1,1] ≈0.96 近重复
    items, total = await service.page("u1", status=ACTIVE)
    assert total == 1 and items[0]["content"] == "aaaaa 回答都用表格"


async def test_remember_validates_content_and_gates(sessionmaker, service):
    tool, _ = build_remember_tool(service, FakeModels())
    with pytest.raises(ToolFailure, match="1~200"):
        await tool.handler(_Ctx(), {"content": "超" * 201})
    await service.set_setting("u1", False)
    with pytest.raises(ToolFailure, match="开关"):
        await tool.handler(_Ctx(), {"content": "回答用表格"})
    await service.set_setting("u1", True)
    await service.set_workspace_setting("u1", "ws1", True)
    with pytest.raises(ToolFailure, match="停用"):
        await tool.handler(_Ctx(), {"content": "回答用表格"})


async def test_remember_capacity_full_reports(sessionmaker, service):
    async with sessionmaker() as session, session.begin():
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"凑 {i}", status=ACTIVE, source=EXTRACTED)
                        for i in range(ACTIVE_CAP))
    tool, _ = build_remember_tool(service, FakeModels())
    with pytest.raises(ToolFailure, match="200"):
        await tool.handler(_Ctx(), {"content": "回答用表格"})


async def test_remember_availability_gates(sessionmaker, service):
    tool, available = build_remember_tool(service, None)
    assert await available(_Ctx()) is True
    await service.set_setting("u1", False)
    assert await available(_Ctx()) is False
    await service.set_setting("u1", True)
    await service.set_workspace_setting("u1", "ws1", True)
    assert await available(_Ctx()) is False


# ---- 整理六步 ----


async def test_organize_expires_pending_and_tasks(sessionmaker, service):

    old_pending = await _seed(sessionmaker, "u1", status=PENDING,
                              created_at=datetime.now(UTC) - timedelta(days=40))
    expired_task = await _seed(sessionmaker, "u1", status=ACTIVE, type=TASK, content="过期在办",
                               expires_at=datetime.now(UTC) - timedelta(days=1))
    fresh_pending = await _seed(sessionmaker, "u1", status=PENDING)
    counts = await organize_user(service, None, "u1", "ws1")
    assert counts["expired"] == 2
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[old_pending] == "archived" and statuses[expired_task] == "archived"
    assert statuses[fresh_pending] == "pending"


async def test_organize_merges_near_duplicates(sessionmaker, service):
    a = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED,
                    content="用户的团队每周一开例会", embedding=[6.0, 0.0, 0.0, 0.0])
    b = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED,
                    content="用户团队例会固定在每周一", embedding=[6.0, 0.0, 0.0, 0.0])
    manual = await _seed(sessionmaker, "u1", status=ACTIVE, source="user",
                         content="回答用表格", embedding=[6.0, 0.0, 0.0, 0.0])

    import json

    class _MergeModels(FakeModels):
        async def chat(self, workspace_id, alias, messages, **kwargs):
            return json.dumps({"decisions": [{"pair": 0, "action": "merge",
                                              "content": "用户的团队例会固定在每周一"}]})

    counts = await organize_user(service, _MergeModels(), "u1", "ws1")
    assert counts["pairs"] == 1 and counts["merged"] == 1
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[a] == "active" and statuses[b] == "archived"  # a 改写合并表述、b 归档
    assert statuses[manual] == "active"  # 人工优先：手动行永不参与合并
    async with sessionmaker() as session:
        kept = await session.get(Memory, a)
        assert kept is not None and kept.content == "用户的团队例会固定在每周一"
        assert kept.embedding is None  # 合并表述是新内容，向量待步骤 5 补算


async def test_organize_decays_only_extracted_fact_focus(sessionmaker, service):

    old = datetime.now(UTC) - timedelta(days=120)  # 4 个月：0.85^4 ≈ 0.52
    stale = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED, type=FACT,
                        content="很久没提的事实", score=0.2, last_mentioned_at=old)
    pref = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED, type=PREFERENCE,
                        content="很久没提的偏好", score=0.2, last_mentioned_at=old)
    manual = await _seed(sessionmaker, "u1", status=ACTIVE, source="user", type=FACT,
                         content="很久没提的手动", score=0.2, last_mentioned_at=old)
    await organize_user(service, None, "u1", "ws1")
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[stale] == "archived"  # 0.2×0.52≈0.104 <0.15 → 归档
    assert statuses[pref] == "active"  # preference 不衰减
    assert statuses[manual] == "active"  # 手动行不衰减


async def test_organize_trims_extracted_only(sessionmaker, service):
    # 手动 190 条 + 提取 15 条 = 205 > 200：只腾 extracted，保 190 手动 + 10 提取
    async with sessionmaker() as session, session.begin():
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"手动 {i}", status=ACTIVE, source=BY_USER, score=1.0)
                        for i in range(190))
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"提取 {i}", status=ACTIVE, source=EXTRACTED, score=0.3 + i * 0.01)
                        for i in range(15))
    counts = await organize_user(service, None, "u1", "ws1")
    assert counts["trimmed"] == 5
    async with sessionmaker() as session:
        manual = await session.scalar(select(func.count()).select_from(Memory).where(
            Memory.source == BY_USER, Memory.status == ACTIVE))
        extracted = await session.scalar(select(func.count()).select_from(Memory).where(
            Memory.source == EXTRACTED, Memory.status == ACTIVE))
    assert manual == 190 and extracted == 10


async def test_organize_refreshes_vectors_and_marks(sessionmaker, service):
    row_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="aaaa 没有向量的行")
    counts = await organize_user(service, FakeModels(), "u1", "ws1")
    assert counts["revectored"] == 1
    async with sessionmaker() as session:
        row = await session.get(Memory, row_id)
        assert row is not None and row.embedding is not None and row.embedding_model == "embedding-alias"
    assert (await service.last_organized_on("u1")) is not None  # 标记写上（organize handler 自己写）


async def test_organize_skips_when_disabled(sessionmaker, service):
    await service.set_setting("u1", False)
    counts = await organize_user(service, None, "u1", "ws1")
    assert counts == {"skipped": "personal_disabled"}


# ---- M3-R1 审查意见的回归用例 ----


class FakeProfiles:
    def __init__(self, kinds: dict[str, str] | None = None) -> None:
        self.kinds = kinds or {}

    async def __call__(self, user_ids):
        return {uid: type("P", (), {"kind": self.kinds.get(uid, "person")})() for uid in user_ids}


async def test_decay_is_incremental_not_reapplied(sessionmaker, service):
    """P1：衰减按增量——连跑两次整理，第二次不再对全量 gap 重复乘（否则两周清空 stale 行）。"""
    from datetime import timedelta

    row_id = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED, type=FACT,
                         content="两个月没提的事实", score=1.0,
                         last_mentioned_at=datetime.now(UTC) - timedelta(days=65))
    await organize_user(service, None, "u1", "ws1")  # 首次：全量 gap（65d//30=2）
    async with sessionmaker() as session:
        score_after_first = (await session.get(Memory, row_id)).score
    assert score_after_first == pytest.approx(1.0 * 0.85 ** 2)
    # 第二次整理：last_organized_at 刚写，增量月数为 0
    second = await organize_user(service, None, "u1", "ws1")
    async with sessionmaker() as session:
        score_after_second = (await session.get(Memory, row_id)).score
    assert score_after_second == score_after_first  # 不再衰减
    assert second["decayed_archived"] == 0


async def test_store_vectors_skips_rewritten_rows(sessionmaker, service):
    """P1：清单读在锁外，期间被改写的行不能被旧向量覆写（且别名相同永不自愈）。"""
    row_id = await _seed(sessionmaker, "u1", status=ACTIVE, content="原内容 aaaa")
    stale = await service.stale_vector_rows("u1", "embedding-alias")
    assert [item["id"] for item in stale] == [row_id]
    await service.edit("u1", row_id, content="改写后的新内容")  # 清单读取与回写之间被改写
    stored = await service.store_vectors("u1", "embedding-alias",
                                         [{"id": item["id"], "content": item["content"],
                                           "embedding": [1.0, 0.0, 0.0, 0.0]} for item in stale])
    assert stored == 0
    async with sessionmaker() as session:
        row = await session.get(Memory, row_id)
        assert row is not None and row.embedding is None  # 新内容行不被旧向量污染


async def test_remember_insert_pairs_embedding_with_model(sessionmaker, service):
    """P2：向量与别名成对写——否则 stale_vector_rows 的「别名不一致」永远不命中，行永不补算。"""
    await service.remember_insert("u1", "ws1", "回答用表格", PREFERENCE,
                                  embedding=[1.0, 0.0, 0.0, 0.0], embedding_model="embedding-alias")
    stale = await service.stale_vector_rows("u1", "embedding-alias")
    assert stale == []  # 别名一致：不在补算清单
    stale_after_switch = await service.stale_vector_rows("u1", "new-embed-alias")
    assert len(stale_after_switch) == 1  # 换了模型：进补算清单


async def test_remember_update_maintains_expiry_invariant(sessionmaker, service):
    """P2：工具改类型的期限不变式与 edit 一致——task 补默认期限，非 task 摘掉。"""

    tool, _ = memory_tool.build_remember_tool(service, None)
    fact_id = await _seed(sessionmaker, "u1", status=ACTIVE, type=FACT, content="一条事实")
    task_view = await tool.handle(_Ctx(), {"id": fact_id}) if False else \
        await service.remember_update("u1", fact_id, "改成在办事项", TASK)
    assert task_view["expiresAt"] is not None  # 转成 task：补默认期限
    back = await service.remember_update("u1", fact_id, "改回事实", FACT)
    assert back["expiresAt"] is None  # 改回非 task：摘期限


async def test_organize_skips_blank_merge_content(sessionmaker, service):
    """P3：模型给出全空白的合并表述 → 跳过该对，不炸整批。"""
    import json as jsonlib

    a = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED,
                    content="用户每周一开例会", embedding=[6.0, 0.0, 0.0, 0.0])
    b = await _seed(sessionmaker, "u1", status=ACTIVE, source=EXTRACTED,
                    content="用户例会在每周一", embedding=[6.0, 0.0, 0.0, 0.0])

    class _BlankMerge(FakeModels):
        async def chat(self, workspace_id, alias, messages, **kwargs):
            return jsonlib.dumps({"decisions": [{"pair": 0, "action": "merge", "content": "   "}]})

    counts = await organize_user(service, _BlankMerge(), "u1", "ws1")
    assert counts["pairs"] == 1 and counts["merged"] == 0  # 跳过而非抛错
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[a] == "active" and statuses[b] == "active"  # 两行原样保留


async def test_remember_rejects_channel_accounts(sessionmaker, service):
    """P3：渠道账号没有「本人」，工具入口显式拒绝（设计 §6/§10）。"""
    tool, available = memory_tool.build_remember_tool(service, None, FakeProfiles({"bot": "channel"}))
    with pytest.raises(ToolFailure, match="渠道"):
        await tool.handler(_Ctx(user_id="bot"), {"content": "回答用表格"})
    assert await available(_Ctx(user_id="bot")) is False
