"""记忆层 M1 的测试：服务层状态机（真实 PG）、迁移分支升降级。

服务层用嵌入式 PG：每个测试一个全新数据库，按当前 metadata 建表（表结构与迁移的一致性由
迁移测试单独保证）；迁移测试照上游 fresh_url 的办法：全新空库，alembic 升到 heads
（主线 0001→0039 + acme_0001）再退回主线头。在 KnowWork 的 server venv 里跑：
    cd KnowWork-stable/server
    uv run pytest ../../acme-kw-enterprise/backend/tests
"""

import asyncio
import shutil
import tempfile
import uuid
from datetime import UTC, datetime

import pgserver
import pytest
from fastapi import FastAPI
from knowwork_server.db.base import Base
from knowwork_server.identity.models import User
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acme.kw_enterprise.extension import MEMORY_MIGRATIONS
from acme.kw_enterprise.memory.models import (
    ACTIVE,
    ACTIVE_CAP,
    ARCHIVED,
    FACT,
    PENDING,
    PREFERENCE,
    SUPERSEDED,
    TASK,
    Memory,
    MemorySetting,
)
from acme.kw_enterprise.memory.service import Conflict, Denied, Invalid, MemoryError, MemoryService, NotFound

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def pg_url():
    """整个模块共用一个嵌入式 PG 实例；每个测试一个自己的数据库。"""
    data_dir = tempfile.mkdtemp(prefix="kw-mem-pg-")
    server = pgserver.get_server(data_dir)
    socket_dir = server.get_uri().split("host=")[1]
    yield f"postgresql+asyncpg://postgres@/postgres?host={socket_dir}"
    server.cleanup()
    shutil.rmtree(data_dir, ignore_errors=True)


async def _create_database(pg_url: str, name: str) -> str:
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    return pg_url.replace("/postgres?", f"/{name}?")


async def _drop_database(pg_url: str, name: str) -> None:
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest.fixture
async def sessionmaker(pg_url):
    """每个测试一个全新数据库：按当前 metadata 建全部表（含 FK 目标 user 与 job），预置两个用户。"""
    import knowwork_server.jobs.models  # noqa: F401  job 表（organize 入队要用）

    name = f"mem_{uuid.uuid4().hex[:12]}"
    url = await _create_database(pg_url, name)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(User(id="u1", email="u1@example.com", name="用户一"))
        session.add(User(id="u2", email="u2@example.com", name="用户二"))
    yield maker
    await engine.dispose()
    await _drop_database(pg_url, name)


@pytest.fixture
def service(sessionmaker):
    """默认给放行的假 AccessService：organize 与空间开关读取都要判成员。"""
    return MemoryService(sessionmaker, FakeAccess())


class FakeAccess:
    """AccessService 的假件：check(user, workspace, resource, action)。

    上游契约是「输入不合法或判定出错一律为 False」（core.py:152），永远返回 bool 不抛异常，
    所以这里不复现抛错路径。"""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str, str, str]] = []

    async def check(self, user_id: str, workspace_id: str, resource: str, action: str) -> bool:
        self.calls.append((user_id, workspace_id, resource, action))
        return self.allowed


# ---- 服务层：增查改删与校验 ----


async def test_add_and_page_and_counts(sessionmaker, service):
    added = await service.add("u1", type=PREFERENCE, content="回答用表格")
    assert added["status"] == ACTIVE
    assert added["source"] == "user"
    assert added["expiresAt"] is None
    added_task = await service.add("u1", type=TASK, content="核对 Q3 各区域净收入")
    assert added_task["expiresAt"] is not None  # 在办事项默认 30 天期限

    items, total = await service.page("u1", status=ACTIVE)
    assert total == 2 and [item["id"] for item in items] == [added_task["id"], added["id"]]  # 新的在前

    counts = await service.counts("u1")
    assert counts == {"active": 2, "pending": 0, "superseded": 0, "archived": 0, "cap": 200}
    assert (await service.counts("u2"))["active"] == 0  # 别人的不串


async def test_add_validates_content(service):
    for bad in ("", "   ", "超" * 501):
        with pytest.raises(Invalid):
            await service.add("u1", type=FACT, content=bad)


async def test_edit_clears_embedding_and_sets_task_ttl(sessionmaker, service):
    added = await service.add("u1", type=FACT, content="2024 年销售费用总额 20,900 元")
    async with sessionmaker() as session, session.begin():  # 模拟 M2 写入的向量
        row = await session.get(Memory, added["id"])
        row.embedding = [0.1, 0.2]
        row.embedding_model = "embedding-alias"

    edited = await service.edit("u1", added["id"], content="2024 年销售费用总额 20900 元")
    assert edited["content"].endswith("20900 元")
    async with sessionmaker() as session:
        row = await session.get(Memory, added["id"])
        assert row.embedding is None and row.embedding_model is None  # 内容变了，向量作废

    switched = await service.edit("u1", added["id"], type=TASK)
    assert switched["expiresAt"] is not None  # 改成在办事项时补默认期限
    deadline = await service.edit("u1", added["id"], expires_at=datetime(2026, 12, 1, tzinfo=UTC))
    assert deadline["expiresAt"] == datetime(2026, 12, 1, tzinfo=UTC)
    back = await service.edit("u1", added["id"], type=FACT)
    assert back["expiresAt"] is None  # 改走别的类型就摘期限，别让整理误归档


async def test_edit_keeps_expiry_invariant(sessionmaker, service):
    """期限只在办事项有：fact + expiresAt 的组合不落期限；naive datetime 拒收（422）。"""
    added = await service.add("u1", type=FACT, content="一条事实")
    with pytest.raises(Invalid, match="时区"):
        await service.edit("u1", added["id"], expires_at=datetime(2026, 12, 1))  # 无 tzinfo
    switched = await service.edit("u1", added["id"], type=TASK)
    assert switched["expiresAt"] is not None
    back = await service.edit("u1", added["id"], type=FACT,
                              expires_at=datetime(2027, 1, 1, tzinfo=UTC))
    assert back["expiresAt"] is None  # 同时传 type=fact 和 expiresAt 也不留期限


async def test_restore_refreshes_expired_task(sessionmaker, service):
    """过期归档的在办事项恢复 = 再给一个默认期限窗口，不然下一次整理又立刻归档。"""
    expired = await _insert(sessionmaker, "u1", status=ARCHIVED, type=TASK,
                            content="过期的在办事项", expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    restored = await service.restore("u1", expired)
    assert restored["expiresAt"] is not None and restored["expiresAt"] > datetime.now(UTC)


async def test_restore_superseded_supersedes_successor(sessionmaker, service):
    """恢复「已被更新」的旧版本时，取代它的那条降为 SUPERSEDED 并指回本行：新旧互斥，不同时注入。
    （后继落 SUPERSEDED 而非 ARCHIVED：语义是「被旧版取代」，页签角标也按这个口径，R5 定。）"""
    new_id = await _insert(sessionmaker, "u1", status=ACTIVE)
    old_id = await _insert(sessionmaker, "u1", status=SUPERSEDED, superseded_by=new_id)
    restored = await service.restore("u1", old_id)
    assert restored["status"] == ACTIVE and restored["supersededBy"] is None
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[new_id] == SUPERSEDED and statuses[old_id] == ACTIVE
    async with sessionmaker() as session:  # 指针回写是往返互斥的关键：后继要能找到本行
        successor = await session.get(Memory, new_id)
        assert successor is not None and successor.superseded_by == old_id


async def test_delete_is_scoped_to_owner(sessionmaker, service):
    added = await service.add("u1", type=FACT, content="查询年平均净利润")
    with pytest.raises(NotFound):
        await service.delete("u2", added["id"])
    await service.delete("u1", added["id"])
    with pytest.raises(NotFound):
        await service.delete("u1", added["id"])


# ---- 状态机 ----


async def _insert(sessionmaker, user_id: str, *, type: str = FACT, content: str = "占位内容",
                  **kwargs) -> str:
    memory_id = f"mem_{uuid.uuid4().hex}"
    async with sessionmaker() as session, session.begin():
        session.add(Memory(id=memory_id, user_id=user_id, type=type, content=content, **kwargs))
    return memory_id


async def test_confirm_and_dismiss_only_for_pending(sessionmaker, service):
    pending = await _insert(sessionmaker, "u1", status=PENDING)
    confirmed = await service.confirm("u1", pending)
    assert confirmed["status"] == ACTIVE
    with pytest.raises(Conflict):
        await service.confirm("u1", pending)  # 已生效，不能重复确认

    active = await _insert(sessionmaker, "u1", status=ACTIVE)
    with pytest.raises(Conflict):
        await service.dismiss("u1", active)
    pending2 = await _insert(sessionmaker, "u1", status=PENDING)
    await service.dismiss("u1", pending2)  # 忽略 = 物理删除
    with pytest.raises(NotFound):
        await service.delete("u1", pending2)


async def test_restore_from_archived_and_superseded(sessionmaker, service):
    archived = await _insert(sessionmaker, "u1", status=ARCHIVED)
    assert (await service.restore("u1", archived))["status"] == ACTIVE

    superseded = await _insert(sessionmaker, "u1", status=SUPERSEDED, superseded_by="mem_" + "a" * 32)
    restored = await service.restore("u1", superseded)
    assert restored["status"] == ACTIVE and restored["supersededBy"] is None

    with pytest.raises(Conflict):
        await service.restore("u1", await _insert(sessionmaker, "u1", status=ACTIVE))


async def test_capacity_is_enforced(sessionmaker, service):
    """满额 200：第 201 条进不来（advisory 事务锁 + 同事务 count，同一用户的查写被串行化）。"""
    async with sessionmaker() as session, session.begin():
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"批量记忆 {index}", status=ACTIVE)
                        for index in range(ACTIVE_CAP))
    with pytest.raises(Conflict, match="200 条"):
        await service.add("u1", type=FACT, content="第 201 条")
    pending = await _insert(sessionmaker, "u1", status=PENDING)
    with pytest.raises(Conflict, match="200 条"):
        await service.confirm("u1", pending)


async def test_views_survive_strict_sessionmaker(sessionmaker, pg_url):
    """回归：写路由的视图在事务块内构造，不依赖 expire_on_commit=False
    （真实装配是 make_sessionmaker 的 False，但服务不该耦合这个配置）。"""

    name = f"mem_strict_{uuid.uuid4().hex[:8]}"
    url = await _create_database(pg_url, name)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    strict = async_sessionmaker(engine)  # 默认 expire_on_commit=True
    async with strict() as session, session.begin():
        session.add(User(id="u9", email="u9@example.com"))
    service = MemoryService(strict, FakeAccess())
    added = await service.add("u9", type=FACT, content="严格会话下的写入")
    assert added["content"] == "严格会话下的写入"
    edited = await service.edit("u9", added["id"], content="改一句话")
    assert edited["content"] == "改一句话"
    await engine.dispose()
    await _drop_database(pg_url, name)



# ---- 开关 ----


async def test_personal_setting_defaults_to_enabled(sessionmaker, service):
    assert await service.setting("u1") == {"enabled": True}   # 没有行 = 开启
    assert await service.set_setting("u1", False) == {"enabled": False}
    assert await service.setting("u1") == {"enabled": False}
    assert await service.set_setting("u1", True) == {"enabled": True}


async def test_workspace_setting_gates(sessionmaker):
    """读要成员（use）、写要管理员（edit），判定都在服务层；没给 access 一律 403。"""
    access = FakeAccess(allowed=False)
    service = MemoryService(sessionmaker, access)
    with pytest.raises(Denied):
        await service.workspace_setting("u1", "ws1")  # 非成员读不到，别给外人探测的口子

    access.allowed = True
    assert await service.workspace_setting("u1", "ws1") == {"disabled": False}  # 没有行 = 启用

    access.allowed = False
    with pytest.raises(Denied):
        await service.set_workspace_setting("u1", "ws1", True)
    assert access.calls[-1] == ("u1", "ws1", "workspace", "edit")  # 写判 edit（admin/owner 过）

    access.allowed = True
    assert await service.set_workspace_setting("u1", "ws1", True) == {"disabled": True}
    assert await service.workspace_setting("u1", "ws1") == {"disabled": True}

    # 裸 upsert 不走 ORM 的 onupdate：显式 set updated_at，反复保存后时间戳要真的动过
    async with sessionmaker() as session:
        first_updated = (await session.execute(text(
            "SELECT updated_at FROM memory_workspace_setting"))).scalar()
    await asyncio.sleep(0.02)
    await service.set_workspace_setting("u1", "ws1", False)
    async with sessionmaker() as session:
        second_updated = (await session.execute(text(
            "SELECT updated_at FROM memory_workspace_setting"))).scalar()
    assert second_updated > first_updated  # 两次独立事务，update 的时间戳严格靠后（memory_setting 同路径）

    bare = MemoryService(sessionmaker)  # 没给 access：判不了宁可 403 不放过
    with pytest.raises(Denied):
        await bare.workspace_setting("u1", "ws1")
    with pytest.raises(Denied):
        await bare.set_workspace_setting("u1", "ws1", True)


# ---- 整理入队、导出与清空 ----


async def test_organize_enqueues_merged_job(sessionmaker, service):
    first = await service.organize("u1", "ws1")
    second = await service.organize("u1", "ws1")
    assert first and first == second  # 同 (空间, 类型, 主体) 合并排队，连点无副作用
    async with sessionmaker() as session:
        job = (await session.execute(text("SELECT kind, status, payload FROM job"))).one()
    assert job.kind == "memory.organize" and job.status == "queued"
    assert job.payload == {"user_id": "u1", "workspace_id": "ws1"}  # workspace_id 供模型路由


async def test_organize_is_gated(sessionmaker):
    """入队三道闸：成员、个人开关、空间开关——都判在服务层。"""
    access = FakeAccess(allowed=False)
    service = MemoryService(sessionmaker, access)
    with pytest.raises(Denied):
        await service.organize("u1", "ws1")  # 非成员不能往这个空间的 job 表写

    access.allowed = True
    await service.set_setting("u1", False)
    with pytest.raises(Conflict, match="个人记忆已关闭"):
        await service.organize("u1", "ws1")

    await service.set_setting("u1", True)
    await service.set_workspace_setting("u1", "ws1", True)
    with pytest.raises(Conflict, match="本空间已停用"):
        await service.organize("u1", "ws1")

    await service.set_workspace_setting("u1", "ws1", False)
    assert await service.organize("u1", "ws1")


async def test_export_and_clear(sessionmaker, service):
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    await _insert(sessionmaker, "u1", status=ARCHIVED)
    await _insert(sessionmaker, "u2", status=ACTIVE, source="user")
    await service.set_setting("u1", False)  # 开关行也要在 clear 时一并清掉（R4：原用例没造这行，断言恒真）

    exported = await service.export("u1")
    assert len(exported) == 2 and {item["status"] for item in exported} == {ACTIVE, ARCHIVED}

    assert await service.clear("u1") == 2
    assert await service.export("u1") == []
    assert len(await service.export("u2")) == 1              # 只清本人的
    assert await service.setting("u1") == {"enabled": True}  # 个人开关一并复位

    async with sessionmaker() as session:
        remaining = await session.scalar(select(func.count()).select_from(MemorySetting))
    assert int(remaining) == 0


async def test_restore_superseded_is_capacity_neutral(sessionmaker, service):
    """恢复「已被更新」净变化为零（后继降级、本行激活）：满额时不该误报 409。
    形状取系统真正会产生的正向链：SUPERSEDED 行指向 ACTIVE 后继（R5：上一版造的
    ACTIVE 带指针的形状在状态机里不可达，验证的是假想分支）。"""
    successor = await _insert(sessionmaker, "u1", status=ACTIVE)
    superseded = await _insert(sessionmaker, "u1", status=SUPERSEDED, superseded_by=successor)
    async with sessionmaker() as session, session.begin():  # 加上后继凑满 200 生效中
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"凑数 {index}", status=ACTIVE)
                        for index in range(ACTIVE_CAP - 1))
    restored = await service.restore("u1", superseded)  # 后继先降级腾出一席，恢复不该撞满额
    assert restored["status"] == ACTIVE


async def test_restore_walks_multi_hop_chain(sessionmaker, service):
    """多跳链 A SUP→B SUP→C ACT：恢复 A 整条收敛到恰好一条 ACTIVE，且链仍可导航。
    （R6：next_pointer 护栏此前零覆盖——只有这个形状才让续链真正起作用。）"""
    c = await _insert(sessionmaker, "u1", status=ACTIVE, content="C")
    b = await _insert(sessionmaker, "u1", status=SUPERSEDED, content="B", superseded_by=c)
    a = await _insert(sessionmaker, "u1", status=SUPERSEDED, content="A", superseded_by=b)
    restored = await service.restore("u1", a)
    assert restored["status"] == ACTIVE
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[a] == ACTIVE and statuses[b] == SUPERSEDED and statuses[c] == SUPERSEDED
    # 链仍可导航：恢复 C，A 要让位
    assert (await service.restore("u1", c))["status"] == ACTIVE
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[c] == ACTIVE and statuses[a] == SUPERSEDED


async def test_restore_round_trip_stays_exclusive(sessionmaker, service):
    """往返恢复后恰好一条 ACTIVE：靠的是后继降级时回写指针——后继将来恢复时靠它找到本行
    （R5 纠正归因：正向链 + 指针回写就够，不是「双向查询」）。"""
    new_id = await _insert(sessionmaker, "u1", status=ACTIVE, content="新版表述")
    old_id = await _insert(sessionmaker, "u1", status=SUPERSEDED, content="旧版表述", superseded_by=new_id)
    await service.restore("u1", old_id)   # 旧版复活、新版降为 SUPERSEDED 且指向旧版
    # 再恢复新版：正向链命中旧版（ACTIVE），旧版让位
    restored_back = await service.restore("u1", new_id)
    assert restored_back["status"] == ACTIVE
    statuses = {item["id"]: item["status"] for item in await service.export("u1")}
    assert statuses[new_id] == ACTIVE and statuses[old_id] == SUPERSEDED


# ---- 路由层：状态码映射与请求体校验（stub service，不起真实依赖） ----


def _http_app(service) -> FastAPI:
    from acme.kw_enterprise.memory import api as memory_api

    class Ctx:  # 最小上下文：身份固定 u1，其余字段只求装配通过
        def principal(self, scope):
            async def who():
                from knowwork_server.auth.principal import Principal

                return Principal(user_id="u1", auth_method="session")

            return who

        global_sessionmaker = None
        access = None

    app = FastAPI()
    for router in memory_api.build_routers(Ctx(), service=service):
        app.include_router(router)
    return app


class StubService:
    """路由层替身：error 非空时任何方法都抛它，验证 _call 的映射；为空时返回最小合法值。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def add(self, user_id, *, type, content):
        self._raise()
        return {"id": "mem_" + "0" * 32, "status": "active"}

    async def delete(self, user_id, memory_id):
        self._raise()

    async def confirm(self, user_id, memory_id):
        self._raise()
        return {}

    async def dismiss(self, user_id, memory_id):
        self._raise()

    async def restore(self, user_id, memory_id):
        self._raise()
        return {}

    async def counts(self, user_id):
        self._raise()
        return {"active": 0, "pending": 0, "superseded": 0, "archived": 0, "cap": 200}

    async def organize(self, user_id, workspace_id):
        self._raise()
        return "job_1"

    async def clear(self, user_id):
        self._raise()
        return 0

    async def setting(self, user_id):
        self._raise()
        return {"enabled": True}

    async def set_setting(self, user_id, enabled):
        self._raise()
        return {"enabled": enabled}

    async def workspace_setting(self, actor_id, workspace_id):
        self._raise()
        return {"disabled": False}

    async def set_workspace_setting(self, actor_id, workspace_id, disabled):
        self._raise()
        return {"disabled": disabled}


def test_error_status_mapping():
    """服务层异常 → HTTP：403/404/409/422，其余 MemoryError 400（不逃成 500）。"""
    from fastapi.testclient import TestClient

    cases = [(Denied("x"), 403), (NotFound("x"), 404), (Conflict("x"), 409), (Invalid("x"), 422),
             (MemoryError("x"), 400)]
    for error, status in cases:
        client = TestClient(_http_app(StubService(error)))
        response = client.post("/api/memory", json={"type": "fact", "content": "内容"})
        assert response.status_code == status, f"{type(error).__name__} → {response.status_code}"


def test_route_shapes():
    """201/202/204/200 的形状、MemoryId 路径模式与请求体校验（422）。"""
    from fastapi.testclient import TestClient

    client = TestClient(_http_app(StubService()))
    assert client.post("/api/memory", json={"type": "fact", "content": "内容"}).status_code == 201
    assert client.post("/api/memory/organize", json={"workspaceId": "ws"}).status_code == 202
    assert client.post("/api/memory/clear").status_code == 204
    assert client.post(f"/api/memory/{'mem_' + '0' * 32}/dismiss").status_code == 204
    assert client.put("/api/workspaces/ws/memory-setting", json={"disabled": True}).status_code == 200
    assert client.get("/api/workspaces/ws/memory-setting").status_code == 200
    assert client.get("/api/memory/counts").status_code == 200
    # memory_id 不合模式 → 422（路径校验），不会 404 误当「没有这条」
    assert client.patch("/api/memory/not-a-memory-id", json={"content": "x"}).status_code == 422
    assert client.post("/api/memory", json={"type": "fact", "content": "x" * 501}).status_code == 422
    # 纯空白在服务层拦（_content_of，test_add_validates_content 覆盖），路由只校验枚举与长度
    assert client.post("/api/memory", json={"type": "odd", "content": "x"}).status_code == 422
    assert client.post("/api/memory/organize", json={"workspaceId": "含空格 不行"}).status_code == 422


# ---- 迁移分支：全新空库上升降 ----


@pytest.fixture
async def fresh_url(pg_url):
    """全新空库（不建表），给迁移测试走真实的 alembic 路径。"""
    name = f"mem_mig_{uuid.uuid4().hex[:12]}"
    url = await _create_database(pg_url, name)
    yield url
    await _drop_database(pg_url, name)


async def test_migration_branch_up_and_down(fresh_url):
    from alembic import command
    from knowwork_server.db.migrate import Branches, alembic_config

    branches = Branches(locations=(MEMORY_MIGRATIONS,), model_modules=("acme.kw_enterprise.memory.models",))

    def _run(action):
        def step(sync_connection):
            config = alembic_config(fresh_url, branches)
            config.attributes["connection"] = sync_connection
            action(config)
        return step

    engine = create_async_engine(fresh_url)
    async with engine.begin() as connection:
        await connection.run_sync(_run(lambda config: command.upgrade(config, "heads")))

    # 升级后（主线 0039 + acme_0001）三张表都在、能写入一行
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(User(id="u9", email="u9@example.com"))
    async with maker() as session, session.begin():
        session.add(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u9", type=FACT, content="迁移后写入"))
    async with maker() as session:
        assert (await session.scalar(select(func.count()).select_from(Memory))) == 1

    async with engine.begin() as connection:
        # depends_on 的边不走 downgrade 的祖先链，用分支语法退掉 acme 自己（主线停在 0039 不动）
        await connection.run_sync(_run(lambda config: command.downgrade(config, "acme@base")))
    for table in ("memory", "memory_setting", "memory_workspace_setting"):
        async with engine.connect() as connection:
            exists = await connection.scalar(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {"t": table})
        assert exists is None, f"{table} 应已随降级拆除"
    await engine.dispose()
