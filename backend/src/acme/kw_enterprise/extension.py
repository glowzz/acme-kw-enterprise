"""扩展对象：KnowWork 启动时按入口点找到它，按授权（license.py）决定加载。

合同见 knowwork_server.extensions.contract。一期是纯胶水：不建表（model_modules 为空）、
无迁移、无后台任务；三个路由组分别是空间管理、企业技能、企业 MCP。装配出错时 KnowWork
会把整个扩展去掉并记原因（contract 的约定），所以这里不做局部兜底。
"""

from typing import Any

from . import mcp_api, skills_api, workspaces_api
from .license import MODULE


class Enterprise:
    name = "acme-enterprise"
    license_module = MODULE          # 授权里要有这个模块名才加载（license.py 给的就是它）
    model_modules: tuple[str, ...] = ()   # 不建表：三个功能的存储全部在开源服务里
    rls_tables: tuple[str, ...] = ()
    migrations = None                # 无迁移分支

    def setup_api(self, ctx: Any):
        """业务服务启动时装配：返回挂到公开端口的路由。"""
        return [workspaces_api.build_router(ctx),
                skills_api.build_router(ctx),
                mcp_api.build_router(ctx)]

    def setup_internal(self, ctx: Any):
        """内部端口路由：一期没有；保留方法体为空让以后加扩展服务回调时不用动装配。"""
        return []

    def setup_worker(self, ctx: Any) -> None:
        """worker 启动时注册后台任务：一期没有（记忆的提取任务在二期）。"""

    async def start(self) -> None:
        """开始服务前调用：没有要预热的数据。"""

    async def aclose(self) -> None:
        """进程退出时释放资源：没有持有的资源。"""


# 入口点指向实例（上游 loader 只 load 不实例化，合同是对象本身）
EXTENSION = Enterprise()
