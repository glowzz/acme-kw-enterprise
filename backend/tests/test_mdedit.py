"""Markdown 人机双改路由的测试：假 file_editing 驱动 读/存/冲突 409/force/格式与大小门槛/权限。

编辑器页（HTML）只断状态码与关键标记；核心链路（产物收集器、写回沙箱）在 dev stack 冒烟里对真实服务验。
"""

import hashlib
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from knowwork_server.auth.principal import Principal

from acme.kw_enterprise.mdedit.api import build_router

pytestmark = pytest.mark.anyio
WS = "ws1"
CONV = "conv1"
PATH = "output/笔记.md"
CONTENT_V1 = "# 笔记\n\n人写的第一版。\n"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeEditing:
    """ConversationFileEditing 假件：内存按路径存最新内容；读不存在 404；内容同最新就不另存（内核口径）。"""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {PATH: CONTENT_V1.encode()}
        self.saves: list[tuple[str, bytes]] = []


    def digest(self, path: str) -> str:
        return hashlib.sha256(self.store[path]).hexdigest()

    async def locate(self, *, workspace_id: str, conversation_id: str, user_id: str, path: str):
        from dataclasses import dataclass

        @dataclass
        class File:
            relative: str
            name: str

        return File(relative=path, name=path.rsplit("/", 1)[-1])

    async def read(self, file) -> bytes:
        from knowwork_server.documents.editing import EditRefused

        if file.relative not in self.store:
            raise EditRefused("文件已经不在了", 404)
        return self.store[file.relative]

    async def latest_digest(self, file) -> str:
        from knowwork_server.documents.editing import EditRefused

        if file.relative not in self.store:
            raise EditRefused("文件已经不在了", 404)
        return self.digest(file.relative)

    async def save(self, file, data: bytes):
        from dataclasses import dataclass

        @dataclass
        class Saved:
            changed: bool
            artifact_id: str | None
            synced: bool

        if self.store.get(file.relative) == data:
            return Saved(changed=False, artifact_id=None, synced=False)
        self.store[file.relative] = data
        self.saves.append((file.relative, data))
        return Saved(changed=True, artifact_id="art_1", synced=True)


class FakeAccess:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def check(self, user_id: str, workspace_id: str, resource: str, action: str) -> bool:
        return self.allowed


class Ctx:
    def __init__(self, editing: FakeEditing, access: FakeAccess | None = None) -> None:
        self.file_editing = editing  # type: ignore[attr-defined]
        self.access = (access or FakeAccess())  # type: ignore[attr-defined]

    def principal(self, scope):
        async def who():
            return Principal(user_id="u1", auth_method="session")

        return who


@pytest.fixture
def editing():
    return FakeEditing()


@pytest.fixture
def client(editing):
    app = FastAPI()
    app.include_router(build_router(Ctx(editing)))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_page_served_with_no_store(client):
    response = await client.get(f"/api/workspaces/{WS}/md-editor")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "no-store" in response.headers["cache-control"]
    assert "genoffice:ai-edit" in response.text


async def test_membership_required(editing):
    app = FastAPI()
    app.include_router(build_router(Ctx(editing, access=FakeAccess(allowed=False))))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/workspaces/{WS}/md-editor/file?conversation_id={CONV}&path={PATH}")
    assert response.status_code == 403


async def test_read_returns_content_and_digest(client):
    response = await client.get(f"/api/workspaces/{WS}/md-editor/file?conversation_id={CONV}&path={PATH}")
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == CONTENT_V1 and body["name"] == "笔记.md"
    assert body["digest"] == hashlib.sha256(CONTENT_V1.encode()).hexdigest()


async def test_save_conflict_then_force(client, editing):
    base = hashlib.sha256(CONTENT_V1.encode()).hexdigest()

    # AI 在这轮里改了文件（模拟：读到的 base 已过期）
    editing.store[PATH] = ("# 笔记\n\nAI 改过的第二版。\n").encode()

    mine = "# 笔记\n\n人坚持自己的写法。\n"
    conflict = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": PATH, "content": mine, "base_digest": base},
    )
    assert conflict.status_code == 409
    body = conflict.json()
    assert "AI 改过的第二版" in body["content"] and body["latest_digest"] != base
    assert editing.saves == []  # 冲突时不落任何版本

    # 人选「载入 AI 改过的版本」：重新读再存就不冲突了
    theirs = await client.get(
        f"/api/workspaces/{WS}/md-editor/file?conversation_id={CONV}&path={PATH}")
    latest = theirs.json()
    saved = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": PATH, "content": latest["content"],
              "base_digest": latest["digest"]},
    )
    assert saved.status_code == 200 and saved.json()["changed"] is False  # 与最新一致：不另存

    # 人选「保留我的」：force 覆盖，成为最新一版
    forced = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": PATH, "content": mine, "base_digest": base, "force": True},
    )
    assert forced.status_code == 200 and forced.json()["changed"] is True
    assert editing.store[PATH].decode() == mine


async def test_format_and_size_gates(client, editing):
    response = await client.get(
        f"/api/workspaces/{WS}/md-editor/file?conversation_id={CONV}&path=output/报告.docx")
    assert response.status_code == 415

    big = "x" * (1024 * 1024 + 1)
    response = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": PATH, "content": big,
              "base_digest": hashlib.sha256(CONTENT_V1.encode()).hexdigest(), "force": True},
    )
    assert response.status_code == 413
    assert editing.saves == []


async def test_read_non_utf8_is_415(client, editing):
    editing.store["output/旧文件.md"] = bytes([0xff, 0xfe, 0x00]) + b"binary"
    response = await client.get(
        f"/api/workspaces/{WS}/md-editor/file?conversation_id={CONV}&path=output/旧文件.md")
    assert response.status_code == 415


async def test_digest_endpoint(client, editing):
    response = await client.get(f"/api/workspaces/{WS}/md-editor/digest?conversation_id={CONV}&path={PATH}")
    assert response.status_code == 200
    assert response.json()["digest"] == hashlib.sha256(CONTENT_V1.encode()).hexdigest()
    assert json.dumps(response.json())


async def test_save_new_file_without_prior_version(client, editing):
    """编辑器里新建一份此前不存在的笔记：没有历史版就跳过冲突检查，直接存成 v1。"""
    new_path = "output/新笔记.md"
    content = "# 新笔记\n\n从编辑器里建的。\n"
    response = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": new_path, "content": content,
              "base_digest": "0" * 64, "force": False},
    )
    assert response.status_code == 200 and response.json()["changed"] is True
    assert editing.store[new_path].decode() == content
    # 再存同样的内容：与最新一致，不另存
    again = await client.put(
        f"/api/workspaces/{WS}/md-editor/file",
        json={"conversation_id": CONV, "path": new_path, "content": content,
              "base_digest": response.json()["digest"], "force": False},
    )
    assert again.json()["changed"] is False
