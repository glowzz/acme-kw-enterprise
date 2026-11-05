"""记忆层 M1 的测试：服务层状态机（真实 PG）、迁移分支升降级。

服务层用嵌入式 PG：每个测试一个全新数据库，按当前 metadata 建表（表结构与迁移的一致性由
迁移测试单独保证）；迁移测试照上游 fresh_url 的办法：全新空库，alembic 升到 heads
（主线 0001→0039 + acme_0001）再退回主线头。在 KnowWork 的 server venv 里跑：
    cd KnowWork-stable/server
    uv run pytest ../../acme-kw-enterprise/backend/tests
"""

import shutil
import tempfile
import uuid
from datetime import UTC, datetime

import pgserver
import pytest
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
from acme.kw_enterprise.memory.service import Conflict, Denied, Invalid, MemoryService, NotFound

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
    return MemoryService(sessionmaker)


class FakeAccess:
    """AccessService 的假件：check(user, workspace, resource, action)。"""

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


async def test_delete_is_scoped_to_owner(sessionmaker, service):
    added = await service.add("u1", type=FACT, content="查询年平均净利润")
    with pytest.raises(NotFound):
        await service.delete("u2", added["id"])
    await service.delete("u1", added["id"])
    with pytest.raises(NotFound):
        await service.delete("u1", added["id"])


# ---- 状态机 ----


async def _insert(sessionmaker, user_id: str, **kwargs) -> str:
    memory_id = f"mem_{uuid.uuid4().hex}"
    async with sessionmaker() as session, session.begin():
        session.add(Memory(id=memory_id, user_id=user_id, type=FACT, content="占位内容", **kwargs))
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
    """满额 200：第 201 条进不来（判定与写入同一事务，并发也顶不破——这里只验行为）。"""
    async with sessionmaker() as session, session.begin():
        session.add_all(Memory(id=f"mem_{uuid.uuid4().hex}", user_id="u1", type=FACT,
                               content=f"批量记忆 {index}", status=ACTIVE)
                        for index in range(ACTIVE_CAP))
    with pytest.raises(Conflict, match="200 条"):
        await service.add("u1", type=FACT, content="第 201 条")
    pending = await _insert(sessionmaker, "u1", status=PENDING)
    with pytest.raises(Conflict, match="200 条"):
        await service.confirm("u1", pending)


# ---- 开关 ----


async def test_personal_setting_defaults_to_enabled(sessionmaker, service):
    assert await service.setting("u1") == {"enabled": True}   # 没有行 = 开启
    assert await service.set_setting("u1", False) == {"enabled": False}
    assert await service.setting("u1") == {"enabled": False}
    assert await service.set_setting("u1", True) == {"enabled": True}


async def test_workspace_setting_requires_admin(sessionmaker):
    access = FakeAccess(allowed=False)
    service = MemoryService(sessionmaker, access)
    assert await service.workspace_setting("ws1") == {"disabled": False}  # 没有行 = 启用，任何成员可读

    with pytest.raises(Denied):
        await service.set_workspace_setting("u1", "ws1", True)
    assert access.calls == [("u1", "ws1", "workspace", "edit")]  # 判定在服务层、只判 workspace:edit

    access.allowed = True
    assert await service.set_workspace_setting("u1", "ws1", True) == {"disabled": True}
    assert await service.workspace_setting("ws1") == {"disabled": True}

    bare = MemoryService(sessionmaker)  # 没给 access：判不了宁可 403 不放过
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


async def test_export_and_clear(sessionmaker, service):
    await service.add("u1", type=PREFERENCE, content="回答用表格")
    await _insert(sessionmaker, "u1", status=ARCHIVED)
    await _insert(sessionmaker, "u2", status=ACTIVE, source="user")

    exported = await service.export("u1")
    assert len(exported) == 2 and {item["status"] for item in exported} == {ACTIVE, ARCHIVED}

    assert await service.clear("u1") == 2
    assert await service.export("u1") == []
    assert len(await service.export("u2")) == 1              # 只清本人的
    assert await service.setting("u1") == {"enabled": True}  # 个人开关一并复位

    async with sessionmaker() as session:
        remaining = await session.scalar(select(func.count()).select_from(MemorySetting))
    assert int(remaining) == 0


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
