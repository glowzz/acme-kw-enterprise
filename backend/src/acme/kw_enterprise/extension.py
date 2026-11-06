"""扩展对象：KnowWork 启动时按入口点找到它，按授权（license.py）决定加载。

合同见 knowwork_server.extensions.contract。一期是纯胶水：三个路由组（空间管理、企业技能、
企业 MCP）的存储全部在开源服务里。M1 起带记忆层：三张自己的表（memory.models，全局表不进
RLS）、一条自己的迁移分支、路由挂 /api/memory 与空间开关。M2 起接注入与提取：
turn_context.add（v0.0.6 起多提供者，按登记顺序拼）登记提供者，on_turn_ended 排提取任务，
setup_worker 注册 memory.extract 的 handler。
装配出错时 KnowWork 会把整个扩展去掉并记原因（contract 的约定），所以这里不做局部兜底。
"""

from pathlib import Path
from typing import Any

from knowwork_server.tools.provided import ProvidedTool

from . import mcp_api, skills_api, workspaces_api
from .license import MODULE
from .memory import api as memory_api
from .memory import extract as memory_extract
from .memory import organize as memory_organize
from .memory import tool as memory_tool
from .memory.context import MemoryContext
from .memory.jobs import EXTRACT, ORGANIZE
from .memory.service import MemoryService

# 记忆层的迁移分支：装包就升级，不看授权（contract 的约定）
MEMORY_MIGRATIONS = Path(__file__).with_name("memory_migrations")


class Enterprise:
    name = "acme-enterprise"
    license_module = MODULE          # 授权里要有这个模块名才加载（license.py 给的就是它）
    model_modules = ("acme.kw_enterprise.memory.models",)  # M1 起：记忆的三张全局表
    rls_tables: tuple[str, ...] = ()                       # 记忆跟着人走、跨空间，不进 RLS
    migrations = MEMORY_MIGRATIONS

    def setup_api(self, ctx: Any):
        """业务服务启动时装配：路由 + 每轮上下文提供者 + 回合结束的提取入队 + 「记住」工具。
        MemoryService 共用一份（路由 / 提供者 / 贡献者 / 工具），装配期构造一次。"""
        service = MemoryService(ctx.global_sessionmaker, ctx.access)
        context = MemoryContext(service, ctx.models, ctx.profiles)
        ctx.turn_context.add(context.provider)  # v0.0.6：多提供者按登记顺序拼，出错整轮不带
        ctx.on_turn_ended("memory", memory_extract.build_contributor(service, ctx.profiles))
        remember, available = memory_tool.build_remember_tool(service, ctx.models, ctx.profiles)
        ctx.tools.add(ProvidedTool(tool=remember, available=available))
        return [workspaces_api.build_router(ctx),
                skills_api.build_router(ctx),
                mcp_api.build_router(ctx),
                *memory_api.build_routers(ctx, service=service)]

    def setup_internal(self, ctx: Any):
        """内部端口路由：没有；保留方法体让以后加扩展服务回调时不用动装配。"""
        return []

    def setup_worker(self, ctx: Any) -> None:
        """worker 注册 memory.extract（core 池）与 memory.organize（maintenance 池）：
        模型别名按空间解析（环境变量可钉），没配模型时 handler 记日志收尾（注入不依赖提取）。
        WorkerContext.sessionmaker 是声明空间的工厂——memory 三张表是全局表、无 RLS 策略，
        读写不受影响；worker 侧不 enqueue（提取在 api 入队），所以也不需要 set_workspace。"""
        service = MemoryService(ctx.sessionmaker)
        ctx.runner.register(EXTRACT, memory_extract.build_extract_handler(service, ctx.models),
                            on_give_up=memory_extract.log_give_up)
        ctx.runner.register(ORGANIZE, memory_organize.build_organize_handler(service, ctx.models),
                            on_give_up=memory_organize.log_give_up)

    async def start(self) -> None:
        """开始服务前调用：没有要预热的数据。"""

    async def aclose(self) -> None:
        """进程退出时释放资源：没有持有的资源。"""


# 入口点指向实例（上游 loader 只 load 不实例化，合同是对象本身）
EXTENSION = Enterprise()
