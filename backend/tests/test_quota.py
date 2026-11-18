"""额度层 M1 的测试：解析规则、判定（拦截/只提醒/访客/空间总额）、临时加额的月界、管理与权限、迁移分支。

跑法同 test_memory：在 KnowWork 的 server venv 里
    cd <v0.1.1 的 server> && uv run pytest ../../acme-kw-enterprise/backend/tests/test_quota.py -v
"""

import shutil
import tempfile
import uuid
from datetime import date
from decimal import Decimal

import alembic.command
import pgserver
import pytest
from knowwork_server.access.workspaces import WorkspaceView
from knowwork_server.conversations.quota import QuotaExceeded, TurnStart
from knowwork_server.db.base import Base
from knowwork_server.identity.models import User
from knowwork_server.models.recorder import GroupBy, UsageSummary
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acme.kw_enterprise.extension import MIGRATIONS
from acme.kw_enterprise.quota.models import BLOCK, WARN, QuotaChangeLog, QuotaGrant
from acme.kw_enterprise.quota.service import Denied, Invalid, QuotaService

pytestmark = pytest.mark.anyio

TODAY = date(2026, 12, 15)          # 钉住「今天」：12 月中旬，月界好造
WS = "ws1"
WANT = 5_000_000                    # 官方示例的个人默认额度：500 万


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def pg_url():
    data_dir = tempfile.mkdtemp(prefix="kw-quota-pg-")
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


async def _drop_database(pg_url: str, name: str) -> str:
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest.fixture
async def sessionmaker(pg_url):
    """每个测试一个全新数据库：按当前 metadata 建全部表（user、usage_daily 等都来自内核 models）。"""
    import knowwork_server.access.models  # noqa: F401  workspace_member（overview 要 join）
    import knowwork_server.jobs.models  # noqa: F401

    name = f"quota_{uuid.uuid4().hex[:12]}"
    url = await _create_database(pg_url, name)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(User(id="u1", email="u1@example.com", name="系统管理员", kind="person"))
        session.add(User(id="u2", email="u2@example.com", name="成员二", kind="person"))
        session.add(User(id="u3", email="bot@example.com", name="IM 机器人", kind="channel"))
        session.add(User(id="u4", email="guest@example.com", name="嵌入访客", kind="external"))
        from knowwork_server.access.models import Workspace, WorkspaceMember
        session.add(Workspace(id=WS, name="销售部"))
        session.add(WorkspaceMember(workspace_id=WS, user_id="u1", role="admin"))
        session.add(WorkspaceMember(workspace_id=WS, user_id="u2", role="member"))
        session.add(WorkspaceMember(workspace_id=WS, user_id="u3", role="member"))
    yield maker
    await engine.dispose()
    await _drop_database(pg_url, name)


class FakeAccess:
    """AccessService 假件：check / is_system_admin / my_workspaces（me 用）。"""

    def __init__(self, allowed: bool = True, system_admin: str | None = "u1",
                 workspaces: tuple[WorkspaceView, ...] = ()) -> None:
        self.allowed = allowed
        self.system_admin = system_admin
        self.workspaces = workspaces
        self.calls: list[tuple[str, str, str, str]] = []

    def check(self, user_id: str, workspace_id: str, resource: str, action: str) -> bool:
        self.calls.append((user_id, workspace_id, resource, action))
        return self.allowed

    def is_system_admin(self, user_id: str) -> bool:
        return self.system_admin is not None and user_id == self.system_admin

    async def my_workspaces(self, user_id: str) -> tuple[WorkspaceView, ...]:
        return self.workspaces


class FakeUsage:
    """UsageRecorder.summary 假件：按 (workspace, user) 记本月已用，忽略日期参数（范围由服务算）。"""

    def __init__(self, used: dict[tuple[str, str], int] | None = None) -> None:
        self.used = dict(used or {})

    async def summary(self, workspace_id: str, start: date, end: date, group_by: GroupBy, *,
                      user_id: str | None = None) -> tuple[UsageSummary, ...]:
        assert group_by is GroupBy.USER
        ids = [user_id] if user_id is not None else sorted(
            {key[1] for key in self.used if key[0] == workspace_id})
        return tuple(UsageSummary(key=key, calls=0, input_tokens=0, output_tokens=0,
                                  total_tokens=self.used[(workspace_id, key)], cost=Decimal(0))
                     for key in ids if (workspace_id, key) in self.used)


def make_service(sessionmaker, access=None, used=None, today=TODAY) -> QuotaService:
    return QuotaService(sessionmaker, access or FakeAccess(), FakeUsage(used), today=lambda: today)


def turn(user_id: str, workspace_id: str = WS) -> TurnStart:
    return TurnStart(workspace_id=workspace_id, user_id=user_id, conversation_id="c1")


def seed_usage(used: dict, workspace_id: str, user_id: str, tokens: int) -> None:
    """种进 FakeUsage：这个人当月已用 tokens（服务经 usage.summary 读，与统计页同源的假件）。"""
    used[(workspace_id, user_id)] = tokens


async def seed_workspace_setting(sessionmaker, monthly_total=None, member_default=None, mode=BLOCK):
    from acme.kw_enterprise.quota.models import QuotaWorkspaceSetting
    async with sessionmaker() as session, session.begin():
        session.add(QuotaWorkspaceSetting(workspace_id=WS, monthly_total_tokens=monthly_total,
                                          member_default_tokens=member_default, mode=mode,
                                          updated_by="u1"))


# ---- 解析 ----


async def test_no_config_means_unlimited(sessionmaker):
    service = make_service(sessionmaker)
    resolved = await service.resolve(WS, "u2")
    assert resolved == {"tokens": None, "source": "none", "mode": BLOCK, "grants": 0, "kind": "person"}


async def test_member_default_then_member_override(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    service = make_service(sessionmaker)
    fallback = await service.resolve(WS, "u2")
    assert (fallback["tokens"], fallback["source"]) == (WANT, "default")

    await service.set_member_setting("u1", WS, "u2", 2_000_000, BLOCK)
    overridden = await service.resolve(WS, "u2")
    assert (overridden["tokens"], overridden["source"]) == (2_000_000, "member")
    # 没设个人额度的 u1 自己仍是默认
    assert (await service.resolve(WS, "u1"))["source"] == "default"


async def test_channel_and_external_have_no_personal_layer(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    service = make_service(sessionmaker)
    for user in ("u3", "u4"):
        resolved = await service.resolve(WS, user)
        assert resolved["tokens"] is None and resolved["kind"] in ("channel", "external")


# ---- 判定 ----


async def test_check_passes_within_quota(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    used = {(WS, "u2"): 4_248_000}  # 424.8 万：85% 接近上限
    await make_service(sessionmaker, used=used).check(turn("u2"))  # 还没用完：不拦


async def test_check_blocks_when_exhausted(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    used = {(WS, "u2"): WANT}
    with pytest.raises(QuotaExceeded) as caught:
        await make_service(sessionmaker, used=used).check(turn("u2"))
    assert caught.value.scope == "personal"
    assert caught.value.resets_on == date(2027, 1, 1)
    assert "4,999," in caught.value.message or "5,000,000" in caught.value.message
    assert "每月 1 日恢复" in caught.value.message


async def test_check_warn_mode_does_not_block(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT, mode=WARN)
    await make_service(sessionmaker, used={(WS, "u2"): WANT}).check(turn("u2"))  # 只提醒：放行


async def test_visitors_judged_by_space_total_only(sessionmaker):
    await seed_workspace_setting(sessionmaker, monthly_total=100)
    used = {(WS, "u3"): 100}  # 机器人把空间总额用满
    service = make_service(sessionmaker, used=used)
    with pytest.raises(QuotaExceeded) as caught:
        await service.check(turn("u3"))
    assert caught.value.scope == "workspace"
    with pytest.raises(QuotaExceeded):
        await service.check(turn("u4"))  # 嵌入访客同样只看空间总额


async def test_space_total_blocks_person_even_within_personal(sessionmaker):
    await seed_workspace_setting(sessionmaker, monthly_total=100, member_default=WANT)
    used = {(WS, "u2"): 100}
    with pytest.raises(QuotaExceeded) as caught:
        await make_service(sessionmaker, used=used).check(turn("u2"))
    assert caught.value.scope == "workspace"  # 个人额度没满，但空间总额满了


async def test_grant_raises_effective_and_expires_by_month(sessionmaker):
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    used = {(WS, "u2"): WANT}
    service = make_service(sessionmaker, used=used)
    with pytest.raises(QuotaExceeded):
        await service.check(turn("u2"))

    # 上个月的加额不算数（月底自动失效的口径）：插一行上月的大额记录
    async with sessionmaker() as session, session.begin():
        session.add(QuotaGrant(id="qgr_" + "a" * 32, workspace_id=WS, user_id="u2",
                               month=date(2026, 11, 1), extra_tokens=99_999_999,
                               granted_by="u1"))
    with pytest.raises(QuotaExceeded):
        await service.check(turn("u2"))  # 上月那笔 1 亿不算，仍然超额

    await service.add_grant("u1", WS, "u2", 2_000_000, reason="季度汇报要用")
    await service.check(turn("u2"))  # 本月的 200 万生效：7 百万 > 5 百万，放行


async def test_check_survives_usage_errors(sessionmaker):
    """判定链路自身出错（usage 抛异常）：放行并记日志，不挡住所有人。"""
    class Broken:
        async def summary(self, *args, **kwargs):
            raise RuntimeError("数据库抖了一下")

    service = QuotaService(sessionmaker, FakeAccess(), Broken(), today=lambda: TODAY)
    await seed_workspace_setting(sessionmaker, member_default=WANT)
    await service.check(turn("u2"))  # 不抛


# ---- 管理与权限 ----


async def test_workspace_setting_requires_system_admin_for_total(sessionmaker):
    service = make_service(sessionmaker, access=FakeAccess(system_admin=None))  # 没有系统管理员身份
    with pytest.raises(Denied):
        await service.set_workspace_setting("u1", WS, monthly_total_tokens=7_000_000,
                                            member_default_tokens=WANT, mode=BLOCK)
    # 空间管理员（check 放行）可以只设个人默认
    await service.set_workspace_setting("u1", WS, monthly_total_tokens=None,
                                        member_default_tokens=WANT, mode=BLOCK)


async def test_settings_validate_input(sessionmaker):
    service = make_service(sessionmaker)
    with pytest.raises(Invalid):
        await service.set_workspace_setting("u1", WS, monthly_total_tokens=0,
                                            member_default_tokens=None, mode=BLOCK)
    with pytest.raises(Invalid):
        await service.set_member_setting("u1", WS, "u2", -1, BLOCK)
    with pytest.raises(Invalid):
        await service.set_member_setting("u1", WS, "u2", 100, "gentle")
    with pytest.raises(Invalid):
        await service.add_grant("u1", WS, "u2", 0)


async def test_change_log_written(sessionmaker):
    service = make_service(sessionmaker)
    await service.set_workspace_setting("u1", WS, monthly_total_tokens=7_000_000,
                                        member_default_tokens=WANT, mode=BLOCK)
    await service.set_member_setting("u1", WS, "u2", 2_000_000, BLOCK)
    await service.set_member_setting("u1", WS, "u2", 3_000_000, BLOCK)
    await service.add_grant("u1", WS, "u2", 100_000)
    await service.set_member_setting("u1", WS, "u2", None, BLOCK)  # 删除也记
    async with sessionmaker() as session:
        rows = (await session.scalars(select(QuotaChangeLog).order_by(QuotaChangeLog.id))).all()
    assert [(row.target, row.changed_by) for row in rows] == [
        ("workspace", "u1"), ("member", "u1"), ("member", "u1"), ("grant", "u1"), ("member", "u1")]
    assert rows[2].before == {"monthly_tokens": 2_000_000, "mode": BLOCK}
    assert rows[2].after == {"monthly_tokens": 3_000_000, "mode": BLOCK}
    assert rows[4].after is None and rows[4].before == {"monthly_tokens": 3_000_000, "mode": BLOCK}


async def test_overview_states_and_sources(sessionmaker):
    await seed_workspace_setting(sessionmaker, monthly_total=7_000_000, member_default=WANT)
    await service_member_setting(sessionmaker, "u1", 5_000_000)  # 系统管理员自己的个人额度
    used = {(WS, "u1"): 4_300_000,   # 86%：接近上限（85% 线）
            (WS, "u2"): WANT,        # 100%：用完
            (WS, "u3"): 500}         # 机器人：个人层不适用
    service = make_service(sessionmaker, used=used)
    overview = await service.overview("u1", WS)
    assert overview["monthlyTotalTokens"] == 7_000_000 and overview["memberDefaultTokens"] == WANT
    assert overview["resetsOn"] == "2027-01-01"
    rows = {row["id"]: row for row in overview["members"]}
    assert {row["id"] for row in overview["members"]} == {"u1", "u2", "u3"}
    assert rows["u1"]["status"] == "near" and rows["u1"]["source"] == "member"
    assert rows["u2"]["status"] == "exhausted" and rows["u2"]["source"] == "default"
    assert rows["u3"]["tokens"] is None and rows["u3"]["status"] is None
    assert [row["id"] for row in overview["members"]] == ["u2", "u1", "u3"]  # 按已用降序
    assert overview["workspaceUsed"] == 4_300_000 + WANT + 500


async def service_member_setting(sessionmaker, user_id, tokens):
    service = make_service(sessionmaker)
    await service.set_member_setting("u1", WS, user_id, tokens, BLOCK)
    return service


async def test_overview_requires_space_admin(sessionmaker):
    service = make_service(sessionmaker, access=FakeAccess(allowed=False))
    with pytest.raises(Denied):
        await service.overview("u2", WS)


async def test_me_lists_only_quota_configured_workspaces(sessionmaker):
    await seed_workspace_setting(sessionmaker, monthly_total=7_000_000, member_default=WANT)
    access = FakeAccess(workspaces=(WorkspaceView(id=WS, name="销售部", role="admin"),
                                    WorkspaceView(id="ws_other", name="没配额度的", role="admin")))
    states = await make_service(sessionmaker, access=access).me("u1")
    assert [state["workspaceId"] for state in states] == [WS]
    assert states[0]["workspaceTotalTokens"] == 7_000_000
    assert states[0]["workspaceName"] == "销售部"


# ---- 迁移分支 ----


async def test_migration_branch_up_and_down(pg_url):
    """全新空库：升到全部 heads（主线 0054 + acme_0004），五张表都在、能写一行；退回主线头表消失。"""
    import knowwork_server.extensions.contract  # noqa: F401  装配 Branches 前先让合同模块就位
    from knowwork_server.db.migrate import Branches, alembic_config

    name = f"quota_mig_{uuid.uuid4().hex[:12]}"
    url = await _create_database(pg_url, name)
    branches = Branches(locations=(MIGRATIONS,),
                        model_modules=("acme.kw_enterprise.memory.models",
                                       "acme.kw_enterprise.quota.models",))

    def _run(action):
        def step(sync_connection):
            config = alembic_config(url, branches)
            config.attributes["connection"] = sync_connection
            action(config)
        return step

    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_run(lambda config: alembic.command.upgrade(config, "heads")))
        async with engine.begin() as connection:
            tables = {row[0] for row in await connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'quota%'"))}
        assert tables == {"quota_workspace_setting", "quota_member_setting",
                          "quota_group_setting", "quota_grant", "quota_change_log"}
        async with engine.begin() as connection:
            rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
            versions = {row[0] for row in rows}
        # 主线头跟仓库走（v0.1.1=0054、mineru 分支=0055）；断言 acme 到 0004、主线只有一个头
        assert "acme_0004" in versions and len(versions) == 2
        assert all(v.startswith("00") for v in versions - {"acme_0004"})
        async with engine.begin() as connection:
            # 分支语法退掉 acme 自己（主线 0054 不动）
            await connection.run_sync(_run(lambda config: alembic.command.downgrade(config, "acme@base")))
        async with engine.begin() as connection:
            tables = {row[0] for row in await connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'quota%'"))}
        assert tables == set()  # 退掉 acme 分支后五张表都不在，主线照旧
    finally:
        await engine.dispose()
        await _drop_database(pg_url, name)
