"""扩展包自身的测试：入口点发现、授权、合同属性、路由清单。

服务层行为不在这里测——KnowWork 开源仓库已覆盖（tests/mcp/test_enterprise.py、技能测试等）；
这里只测"胶水"接对了没有：装进环境后能被发现、路由都能构建、路径与设计文档一致。
在 KnowWork 的 server venv 里跑：cd KnowWork-stable/server
# uv run pytest ../../acme-kw-enterprise/backend/tests
"""

from fastapi import FastAPI
from fastapi.routing import APIRoute
from knowwork_server.extensions import installed_extensions


def test_license_and_extension_discovered() -> None:
    extensions = installed_extensions()
    assert extensions.license.status == "valid"
    assert extensions.license.modules == frozenset({"enterprise"})
    assert [e.name for e in extensions.active] == ["acme-enterprise"]
    assert extensions.skipped == ()


def test_contract_attributes() -> None:
    extension = installed_extensions().active[0]
    assert extension.license_module == "enterprise"
    assert extension.model_modules == (), "一期不建表"
    assert extension.rls_tables == ()
    assert extension.migrations is None, "无迁移分支：三个功能的存储都在开源服务里"


def _build_app() -> FastAPI:
    extension = installed_extensions().active[0]

    class Ctx:  # 最小上下文：只够装配路由（请求时的对象由真实运行时提供）
        def principal(self, scope):
            async def who():
                from knowwork_server.auth.principal import Principal

                return Principal(user_id="admin", auth_method="session")

            return who

    ctx = Ctx()
    ctx.platform_skills = None  # type: ignore[attr-defined]
    ctx.enterprise_mcp = None  # type: ignore[attr-defined]
    ctx.access = None  # type: ignore[attr-defined]

    app = FastAPI()
    for router in extension.setup_api(ctx):
        app.include_router(router)
    return app


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()

    def walk(items) -> None:
        for item in items:
            if isinstance(item, APIRoute):
                for method in item.methods or ():
                    found.add((method, item.path))
                continue
            # 这个 FastAPI 版本把 include 的路由包成 _IncludedRouter，真实路由在 original_router 里
            original = getattr(item, "original_router", None)
            if original is not None:
                walk(original.routes)
                continue
            nested = getattr(item, "routes", None)
            if nested:
                walk(nested)

    walk(app.router.routes)
    return found


def test_workspace_routes() -> None:
    routes = _routes(_build_app())
    assert {path for method, path in routes if "/api/system/workspaces" in path} >= {
        "/api/system/workspaces",
        "/api/system/workspaces/{workspace_id}",
    }
    assert ("POST", "/api/system/workspaces") in routes
    assert ("DELETE", "/api/system/workspaces/{workspace_id}") in routes


def test_platform_skill_routes() -> None:
    routes = _routes(_build_app())
    paths = {path for _, path in routes if "/api/platform-skills" in path}
    assert "/api/platform-skills" in paths
    assert "/api/platform-skills/promotable" in paths
    assert "/api/platform-skills/promote" in paths
    assert "/api/platform-skills/{skill_id}/audience" in paths
    assert ("PUT", "/api/platform-skills/{skill_id}/draft") in routes


def test_enterprise_mcp_routes() -> None:
    routes = _routes(_build_app())
    paths = {path for _, path in routes if "/api/enterprise-mcp" in path}
    assert "/api/enterprise-mcp" in paths
    assert "/api/enterprise-mcp/probe" in paths
    assert "/api/enterprise-mcp/promotable" in paths
    assert "/api/enterprise-mcp/promote" in paths
    assert "/api/enterprise-mcp/{server_id}/tools/{tool_name}" in paths
    assert ("PUT", "/api/enterprise-mcp/{server_id}/audience") in routes
