"""企业技能管理路由（/api/platform-skills）：平台层的技能 CRUD、发布、发放、收编，给系统管理员。

服务层是开源的 PlatformSkillAdmin（skills/platform.py，ApiContext.platform_skills 已装配），
判定系统管理员在它的 _actor 里；错误映射与 ZIP 读取、If-Match 修订号直接复用开源的
skills/api.py（call_service / read_archive / revision_of / PublishBody / EnvBody），
不复制逻辑，避免两处漂移。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from knowwork_server.auth.principal import Principal
from knowwork_server.skills.api import EnvBody, PublishBody, call_service, read_archive, revision_of
from pydantic import BaseModel, ConfigDict, Field

IdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]


class AudienceBody(BaseModel):
    """发给哪些空间：["*"] 是全部空间（以后新建的也有），否则是空间 id 列表。
    元素在路由层先按 id 的口径把关（服务层再验存在性），混入任意超长串直接 422。"""

    model_config = ConfigDict(extra="forbid")

    workspaces: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\-*]{1,64}$")]] = Field(max_length=500)


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(validation_alias="workspaceId", min_length=1, max_length=64)
    skill_id: str = Field(validation_alias="skillId", min_length=1, max_length=64)


_SCOPE_403 = {"code": "forbidden_scope", "message": "API Key 缺少所需范围"}


def _read(who: Principal) -> None:
    """API Key 的范围与空间技能接口同款（上游 skills/api.py 的 actor_of 也是直接 403）：
    读要 skills:read，写要 skills:write；会话身份不受限。"""
    if not who.allows("skills:read"):
        raise HTTPException(status_code=403, detail=_SCOPE_403)


def _write(who: Principal) -> None:
    if not who.allows("skills:write"):
        raise HTTPException(status_code=403, detail=_SCOPE_403)


def build_router(ctx) -> APIRouter:
    admin = ctx.platform_skills  # PlatformSkillAdmin；app.py 装配时恒给，缺失说明上游改了装配，尽快暴露
    router = APIRouter(prefix="/api/platform-skills", tags=["skills"])
    Who = Annotated[Principal, Depends(ctx.principal(None))]

    @router.get("")
    async def list_skills(who: Who) -> dict:
        """企业技能列表（{"skills": [...]}，同空间技能列表的响应形状）"""
        _read(who)
        views = await call_service(admin.skills(who.user_id))
        return {"skills": [view.to_json() for view in views]}

    @router.get("/promotable")
    async def promotable(who: Who) -> list[dict]:
        """可以收编为企业的空间技能（全部空间里已发布的）"""
        _read(who)
        found = await call_service(admin.promotable(who.user_id))
        return [item.to_json() for item in found]

    @router.post("", status_code=201)
    async def install(request: Request, who: Who) -> dict:
        """上传技能包（ZIP）"""
        _write(who)
        archive = await read_archive(request, admin.limits.max_archive_bytes)
        return (await call_service(admin.install(who.user_id, archive))).to_json()

    @router.get("/{skill_id}")
    async def detail(skill_id: IdPath, who: Who) -> dict:
        """一个企业技能的详情与版本历史"""
        _read(who)
        view, releases = await call_service(admin.get(who.user_id, skill_id))
        return {**view.to_json(), "releases": [release.to_json() for release in releases]}

    @router.put("/{skill_id}/draft")
    async def replace_draft(skill_id: IdPath, request: Request, who: Who,
                            if_match: Annotated[str | None, Header()] = None) -> dict:
        """保存企业技能草稿（If-Match 带草稿修订号）"""
        _write(who)
        archive = await read_archive(request, admin.limits.max_archive_bytes)
        view = await call_service(admin.replace_draft(who.user_id, skill_id, archive,
                                                      expected_revision=revision_of(if_match)))
        return view.to_json()

    @router.post("/{skill_id}/publish", status_code=201)
    async def publish(skill_id: IdPath, body: PublishBody, who: Who,
                      if_match: Annotated[str | None, Header()] = None) -> dict:
        """发布企业技能（If-Match 带草稿修订号，防并发覆盖）"""
        _write(who)
        release = await call_service(admin.publish(who.user_id, skill_id, version=body.version,
                                                   expected_revision=revision_of(if_match)))
        return release.to_json()

    @router.post("/{skill_id}/enable")
    async def enable(skill_id: IdPath, who: Who) -> dict:
        """启用企业技能"""
        _write(who)
        return (await call_service(admin.set_enabled(who.user_id, skill_id, True))).to_json()

    @router.post("/{skill_id}/disable")
    async def disable(skill_id: IdPath, who: Who) -> dict:
        """停用企业技能"""
        _write(who)
        return (await call_service(admin.set_enabled(who.user_id, skill_id, False))).to_json()

    @router.put("/{skill_id}/env")
    async def set_env(skill_id: IdPath, body: EnvBody, who: Who) -> dict:
        """填写企业技能的环境变量（值只写不读，只回填了哪些名字）"""
        _write(who)
        return (await call_service(admin.set_env(who.user_id, skill_id, body.values))).to_json()

    @router.put("/{skill_id}/audience")
    async def set_audience(skill_id: IdPath, body: AudienceBody, who: Who) -> dict:
        """改发给哪些空间"""
        _write(who)
        return (await call_service(admin.set_audience(who.user_id, skill_id, body.workspaces))).to_json()

    @router.delete("/{skill_id}", status_code=204)
    async def delete(skill_id: IdPath, who: Who) -> Response:
        """删除企业技能"""
        _write(who)
        await call_service(admin.delete(who.user_id, skill_id))
        return Response(status_code=204)

    @router.post("/promote")
    async def promote(body: PromoteBody, who: Who) -> dict:
        """把空间技能收为企业技能（第一次新建，再收是它的新草稿）"""
        _write(who)
        return (await call_service(admin.promote(who.user_id, body.workspace_id, body.skill_id))).to_json()

    return router
