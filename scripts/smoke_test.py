"""扩展包功能的验收冒烟（对真实运行环境发 HTTP，覆盖设计文档的验收清单）。

用法：python3 smoke_test.py [--token TOKEN] [base_url] [email] [password]
    base_url  默认 http://127.0.0.1:8100
    --token   直接用现成会话 Cookie（kw_session 值），跳过登录——联调环境用后门造会话时方便
环境不被破坏的保证：只新建「smoke-」前缀的空间/技能；记忆段自添加自删除、
开关做完恢复原状、不调清空（clear 会删掉本人真实记忆）。

链路：
 1. 登录（Cookie 会话；--token 跳过）
 2. 空间管理：列表 → 新建 smoke-ws → 列表可见 → 改名 → （收尾）关闭
 3. 企业技能：上传 ZIP → 列表可见 → 发布 → 发放 [smoke-ws, default] → 详情/版本 → （收尾）删除
 4. 权限：未登录访问 → 401（隐含于每步 Cookie）
 5. 收尾清理
 6. 我的记忆：counts → 手动添加 → 列表/编辑/导出 → 删除 → 双开关（做完恢复） → 整理入队
"""

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from typing import Any

parser = argparse.ArgumentParser()
parser.add_argument("base", nargs="?", default="http://127.0.0.1:8100")
parser.add_argument("email", nargs="?")
parser.add_argument("password", nargs="?")
parser.add_argument("--token", default=None)
args = parser.parse_args()
BASE = args.base
EMAIL, PASSWORD, TOKEN = args.email, args.password, args.token

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

# ---- 1. 登录（或用现成 token） ----
if TOKEN:
    COOKIE = f"kw_session={TOKEN}"
    ok("1. 使用 --token 会话", True)
else:
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

# ---- 6. 我的记忆（自清理：添加的删除，开关做完恢复；不调 clear——会删本人真实记忆） ----
status, before_counts = call("GET", "/api/memory/counts")
ok("6.1 counts", status == 200 and isinstance(before_counts, dict) and "cap" in before_counts,
   f"status={status} body={before_counts}")

status, added = call("POST", "/api/memory", {"type": "preference", "content": "冒烟：回答用表格"})
ok("6.2 手动添加（201，直接生效）",
   status == 201 and added.get("status") == "active" and added.get("source") == "user",
   f"status={status} body={added}")
MEM = added.get("id") if isinstance(added, dict) else None

status, after_counts = call("GET", "/api/memory/counts")
ok("6.3 counts.active +1",
   isinstance(after_counts, dict) and after_counts.get("active") == (before_counts or {}).get("active", 0) + 1,
   f"before={before_counts} after={after_counts}")

status, listing = call("GET", "/api/memory?status=active&limit=50")
ids = [item.get("id") for item in (listing or {}).get("items", [])]
ok("6.4 列表含新记忆", MEM in ids, f"ids={ids[:5]}")

status, edited = call("PATCH", f"/api/memory/{MEM}", {"content": "冒烟：回答改用表格"})
ok("6.5 编辑", status == 200 and edited.get("content") == "冒烟：回答改用表格", f"status={status}")

status, export = call("GET", "/api/memory/export")
export_ids = [item.get("id") for item in (export or [])]
ok("6.6 导出含该记忆（全字段可携带）", MEM in export_ids and all(
    k in (export or [{}])[0] for k in ("type", "content", "source", "status")), f"status={status}")

status, org = call("POST", "/api/memory/organize", {"workspaceId": "default"})
ok("6.7 立即整理入队（202）", status == 202 and (org or {}).get("jobId"), f"status={status} body={org}")

status, setting = call("GET", "/api/memory/settings")
ok("6.8 开关读取（默认开启）", status == 200 and setting.get("enabled") is True, f"body={setting}")

status, _ = call("PUT", "/api/memory/settings", {"enabled": False})
status, setting_off = call("GET", "/api/memory/settings")
ok("6.9 个人开关关闭", status == 200 and setting_off.get("enabled") is False, f"body={setting_off}")
status, denied = call("POST", "/api/memory", {"type": "fact", "content": "关着不该能加"})
ok("6.10 关闭后添加被拦（409）", status == 409, f"status={status} body={denied}")
status, _ = call("PUT", "/api/memory/settings", {"enabled": True})  # 恢复原状
status, setting_back = call("GET", "/api/memory/settings")
ok("6.11 开关恢复开启", setting_back.get("enabled") is True, f"body={setting_back}")

status, ws_setting = call("GET", "/api/workspaces/default/memory-setting")
ok("6.12 空间开关读取（默认启用）", status == 200 and ws_setting.get("disabled") is False,
   f"status={status} body={ws_setting}")
status, _ = call("PUT", "/api/workspaces/default/memory-setting", {"disabled": True})
status, ws_off = call("GET", "/api/workspaces/default/memory-setting")
ok("6.13 空间管理员停用", ws_off.get("disabled") is True, f"body={ws_off}")
status, _ = call("PUT", "/api/workspaces/default/memory-setting", {"disabled": False})  # 恢复
status, ws_back = call("GET", "/api/workspaces/default/memory-setting")
ok("6.14 空间开关恢复启用", ws_back.get("disabled") is False, f"body={ws_back}")

status, _ = call("DELETE", f"/api/memory/{MEM}")
ok("6.15 删除自添加的记忆（204）", status == 204, f"status={status}")
status, final_counts = call("GET", "/api/memory/counts")
ok("6.16 counts 恢复",
   isinstance(final_counts, dict) and final_counts.get("active") == (before_counts or {}).get("active", 0),
   f"before={before_counts} after={final_counts}")

print(f"\n合计 {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
