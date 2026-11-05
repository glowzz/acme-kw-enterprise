"""扩展对象：KnowWork 启动时按入口点找到它，按授权（license.py）决定加载。

合同见 knowwork_server.extensions.contract。一期是纯胶水：三个路由组（空间管理、企业技能、
企业 MCP）的存储全部在开源服务里。M1 起带记忆层：三张自己的表（memory.models，全局表不进
RLS）、一条自己的迁移分支、路由挂 /api/memory 与空间开关；提取与注入在 M2/M3 接
（turn_context / on_turn_ended / setup_worker 的接缝留给那时用）。
装配出错时 KnowWork 会把整个扩展去掉并记原因（contract 的约定），所以这里不做局部兜底。
"""

from pathlib import Path
from typing import Any

from . import mcp_api, skills_api, workspaces_api
from .license import MODULE
from .memory import api as memory_api

# 记忆层的迁移分支：装包就升级，不看授权（contract 的约定）
MEMORY_MIGRATIONS = Path(__file__).with_name("memory_migrations")


class Enterprise:
    name = "acme-enterprise"
    license_module = MODULE          # 授权里要有这个模块名才加载（license.py 给的就是它）
    model_modules = ("acme.kw_enterprise.memory.models",)  # M1 起：记忆的三张全局表
    rls_tables: tuple[str, ...] = ()                       # 记忆跟着人走、跨空间，不进 RLS
    migrations = MEMORY_MIGRATIONS

    def setup_api(self, ctx: Any):
        """业务服务启动时装配：返回挂到公开端口的路由。"""
        return [workspaces_api.build_router(ctx),
                skills_api.build_router(ctx),
                mcp_api.build_router(ctx),
                memory_api.build_router(ctx)]

    def setup_internal(self, ctx: Any):
        """内部端口路由：没有；保留方法体让以后加扩展服务回调时不用动装配。"""
        return []

    def setup_worker(self, ctx: Any) -> None:
        """worker 启动时注册后台任务：记忆的提取/整理 handler 在 M2/M3 注册（任务类型已在
        memory.jobs 钉死口径，api 侧已可入队）。"""

    async def start(self) -> None:
        """开始服务前调用：没有要预热的数据。"""

    async def aclose(self) -> None:
        """进程退出时释放资源：没有持有的资源。"""


# 入口点指向实例（上游 loader 只 load 不实例化，合同是对象本身）
EXTENSION = Enterprise()
