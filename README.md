# acme-knowwork-enterprise

KnowWork 自研企业扩展包：**空间管理、企业技能管理、企业 MCP 管理**。实现 KnowWork 开源的扩展合同
（`knowwork_server.extensions.contract`），不建表、无迁移、无后台任务，是"开源服务层 + 管理路由/页面"的胶水。

设计与决策见 KnowWork 仓库的《企业扩展包设计文档.md》；本仓库不改 KnowWork 的任何文件。

## 结构

```
backend/    Python 包（入口点 knowwork.license + knowwork.extensions），装进 KnowWork 的 server venv
frontend/   TS 入口（导出 install(register)），web 构建时经 KNOWWORK_WEB_EXTENSIONS 注入
```

## 开发（对照设计文档 10.3）

```sh
# KnowWork-stable 是钉在 v0.0.6 的 git worktree，扩展联调都在它里面跑
cd ../KnowWork-stable/server
uv pip install -e ../../acme-kw-enterprise/backend          # 后端：入口点即注册；改 pyproject 后要重跑

cd ../KnowWork-stable/web
echo 'KNOWWORK_WEB_EXTENSIONS=../../acme-kw-enterprise/frontend/src/install.tsx' > .env.local

# 本机 8000 被另一套部署占用时，用错开端口的启动脚本（api 8100/8101、宿主 7310、前端 5180）：
cd ../KnowWork-stable/server && uv run python ../../acme-kw-enterprise/scripts/dev_stack_ext.py
```

- 后端测试（不需要整套环境）：
  ```sh
  cd ../KnowWork-stable/server && uv run pytest ../../acme-kw-enterprise/backend/tests -v
  ```
- 前端类型检查（扩展自己的 tsconfig，把 @knowwork/web 指回 worktree 的 src）：
  ```sh
  cd ../acme-kw-enterprise/frontend && ../node_modules/.bin/tsc -p tsconfig.json
  ```
- 改 pyproject.toml（入口点）后必须重跑 `uv pip install -e`，改 .py/.tsx 文件不用（editable + vite 直生效）。
- `frontend/node_modules` 不单独装：靠仓库根的软链指到 worktree web 的 node_modules（解析 react 等依赖）。

## 已实现（一期）

| 功能 | 后端 | 前端 |
|---|---|---|
| 空间管理 | `GET/POST /api/system/workspaces`、`PUT/DELETE /api/system/workspaces/{id}` | `/admin/workspaces` 页面 + 侧栏入口 |
| 企业技能 | `/api/platform-skills`（列表/上传/草稿/发布/启停/环境变量/发放/删除/收编） | 技能页「企业」页签的管理面板 |
| 企业 MCP | `/api/enterprise-mcp`（列表/探测/登记/改/删/同步/工具三档/发放/收编） | 工具页「企业」页签的管理面板（复用开源 ServerList） |

权限：全部只给系统管理员（判定在开源服务层内部）；API Key 走 skills:read/write 范围（同空间技能）。
审计：路由未在开源 ActionSpec 里登记的，按上游 `_derived` 自动生成动作名。
