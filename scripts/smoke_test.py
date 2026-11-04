"""一期功能的验收冒烟（对真实运行环境发 HTTP，覆盖设计文档第 5 节的验收清单）。

用法：python3 smoke_test.py <base_url> <email> <password>
    base_url  默认 http://127.0.0.1:8100
环境不被破坏的保证：只新建「smoke-」前缀的空间/技能，收尾全部删除或关闭。

链路：
 1. 登录（Cookie 会话）
 2. 空间管理：列表 → 新建 smoke-ws → 列表可见 → 改名 → （收尾）关闭
 3. 企业技能：上传 ZIP → 列表可见 → 发布 → 发放 [smoke-ws, default] → 详情/版本 → （收尾）删除
 4. 权限：未登录访问 → 401（隐含于每步 Cookie）
"""

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from typing import Any

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
EMAIL, PASSWORD = sys.argv[2], sys.argv[3]

PASS, FAIL = 0, 0


def ok(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "[PASS]" if condition else "[FAIL]"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    print(f"{mark} {name}{(' - ' + detail) if detail and not condition else ''}")


def call(method: str, path: str, body: Any = None, raw: bytes | None = None,
         content_type: str | None = None):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("Cookie", COOKIE)
    data = None
    if raw is not None:
        req.add_header("Content-Type", content_type or "application/zip")
        data = raw
    elif body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            text = resp.read().decode()
            return resp.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        try:
            return error.code, json.loads(text)
        except json.JSONDecodeError:
            return error.code, text


COOKIE = ""

# ---- 1. 登录 ----
req = urllib.request.Request(f"{BASE}/api/auth/login", method="POST",
                             data=json.dumps({"email": EMAIL, "password": PASSWORD}).encode())
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req) as resp:
    set_cookie = resp.headers.get_all("Set-Cookie") or []
    COOKIE = "; ".join(c.split(";")[0] for c in set_cookie)
ok("1. 登录", bool(COOKIE), "没拿到 Cookie")

# ---- 2. 空间管理 ----
status, workspaces = call("GET", "/api/system/workspaces")
ok("2. 空间列表", status == 200 and isinstance(workspaces, list), f"status={status}")
names = {w.get("name") for w in (workspaces or [])}
ok("2.1 用户建的 test 空间在列表里", "test" in names, f"现有: {names}")

status, created = call("POST", "/api/system/workspaces", {"name": "smoke-ws", "ownerUserId": None})
# ownerUserId 必填，先拿一个真实用户 id
status, whoami = call("GET", "/api/auth/me")
owner_id = whoami.get("id") if isinstance(whoami, dict) else None
status, created = call("POST", "/api/system/workspaces", {"name": "smoke-ws", "ownerUserId": owner_id})
ok("2.2 新建 smoke-ws", status == 201 and created.get("id"), f"status={status} body={created}")
WS = created.get("id") if isinstance(created, dict) else None

status, view = call("GET", "/api/system/workspaces")
ws_entry = next((w for w in (view or []) if w.get("id") == WS), None)
ok("2.3 新空间出现在列表", ws_entry is not None)
ok("2.4 isDefault 标记", ws_entry is not None and ws_entry.get("isDefault") is False)

status, _ = call("PUT", f"/api/system/workspaces/{WS}", {"name": "smoke-ws-renamed"})
ok("2.5 改名", status == 200, f"status={status}")

status, err = call("DELETE", "/api/system/workspaces/default")
ok("2.6 默认空间不能关（409）", status == 409, f"status={status}")

# ---- 3. 企业技能 ----
skill_zip = io.BytesIO()
with zipfile.ZipFile(skill_zip, "w") as z:
    z.writestr("SKILL.md", "---\nname: smoke-skill\ndescription: 冒烟测试技能\n---\n# 冒烟\n")
status, skill = call("POST", "/api/platform-skills", raw=skill_zip.getvalue())
ok("3. 上传企业技能", status == 201 and skill.get("id"), f"status={status} body={skill}")
SKILL = skill.get("id") if isinstance(skill, dict) else None

status, listing = call("GET", "/api/platform-skills")
ids = [s.get("id") for s in (listing or {}).get("skills", [])]
ok("3.1 列表含新技能（{skills:[...]} 形状）", SKILL in ids, f"ids={ids}")

status, view = call("GET", f"/api/platform-skills/{SKILL}")
ok("3.2 详情", status == 200 and view.get("id") == SKILL, f"status={status}")
revision = view.get("draftRevision")

status, release = call("POST", f"/api/platform-skills/{SKILL}/publish", {})
ok("3.3 发布", status == 201 and (release or {}).get("id"), f"status={status} body={release}")

status, updated = call("PUT", f"/api/platform-skills/{SKILL}/audience", {"workspaces": [WS, "default"]})
ok("3.4 发放到 smoke-ws + default", status == 200 and sorted(updated.get("audience") or []) == sorted([WS, "default"]),
   f"status={status} audience={updated.get('audience') if isinstance(updated, dict) else updated}")

status, detail = call("GET", f"/api/platform-skills/{SKILL}")
ok("3.5 发布后有版本历史", len(detail.get("releases") or []) == 1, f"releases={detail.get('releases')}")

# ---- 4. 权限负例（未登录） ----
bare = urllib.request.Request(f"{BASE}/api/platform-skills")
try:
    urllib.request.urlopen(bare)
    ok("4.1 未登录访问企业技能被拒", False, "居然 200")
except urllib.error.HTTPError as error:
    ok("4.1 未登录访问企业技能被拒", error.code == 401, f"code={error.code}")
bare = urllib.request.Request(f"{BASE}/api/system/workspaces")
try:
    urllib.request.urlopen(bare)
    ok("4.2 未登录访问空间管理被拒", False, "居然 200")
except urllib.error.HTTPError as error:
    ok("4.2 未登录访问空间管理被拒", error.code == 401, f"code={error.code}")

# ---- 收尾 ----
status, _ = call("DELETE", f"/api/platform-skills/{SKILL}")
ok("5.1 清理企业技能", status == 204, f"status={status}")
status, _ = call("DELETE", f"/api/system/workspaces/{WS}")
ok("5.2 关闭 smoke-ws", status == 204, f"status={status}")
status, view = call("GET", "/api/system/workspaces")
ids_after = {w.get("id") for w in (view or [])}
ok("5.3 关闭后从列表消失", WS not in ids_after, f"ids={ids_after}")

print(f"\n合计 {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
