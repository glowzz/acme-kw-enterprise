# Markdown 人机双改设计（acme 扩展，基于开源 v0.1.2 接口）

> 背景：官方 v0.1.2 的「人机双写」（网页 Office 编辑器）属于商业版，我们的部署按结论不拉 `knowwork-office` 镜像。
> 但 v0.1.2 开源侧把做这件事的全部口子都开了：`ctx.file_editing`（读最新一份、摘要比对、存成新的一版并写回会话沙箱）、
> 前端插槽 `chat.files.server`（给云端文件的卡片/预览补上「编辑」）、GenOffice 补丁的 `genoffice:*` 消息协议（抽屉与
> 编辑器 iframe 之间的标准桥）。本设计先用 Markdown 落地同一套体验：人和 AI 改同一份文件，谁改了哪里分得清，冲突由人拍板。

## 1. 体验（对齐官方文章的口径）

- 对话里交付/生成的 `.md` 文件，点开预览抽屉**直接是编辑器**（不再是只读预览）：左边编辑、右边渲染预览。
- 人改完点保存（或 ⌘S/Ctrl+S）→ **存成对话里这个路径的新一版**，下一轮模型、交付卡片、预览看到的就是这版；沙箱还在就写回工作目录，AI 接着改的是人刚改过的那份。
- AI 在沙箱里改了同一份文件 → 编辑器自己发现（摘要变了）：没动过就静默载入 AI 的新版；**人也有没保存的改动时出横幅**「AI 改了这个文件，这里还有没保存的改动」，两个按钮「载入 AI 改过的版本」/「保留我的」——**AI 不会悄悄盖掉人的改动**。
- **选中一段 → 「AI 修改」**：选中文字、写要求、发送/放入输入框——照 GenOffice 补丁 6 的 `genoffice:ai-edit` 协议发给外层页面，走抽屉现成的桥进对话；AI 开改时外层发回 `genoffice:ai-busy` 的块区间，编辑器把那几段高亮成「AI 正在改」。
- 附件目录（`input/`）里的文件不给编辑（内核约定只读，让 AI 另存一份再改）。

## 2. 架构

```
预览抽屉 ──iframe──▶ 编辑器页（后端直出的独立 HTML，同源、带会话 Cookie）
   │  genoffice:ai-edit / :ready / :ai-busy（postMessage，照 GenOffice 补丁 6）
   ▼
chat.files.server 插槽（acme 前端 install.tsx 注册）：
   edit()      → .md/.markdown 返回编辑器页地址，别的格式返回 null（不动 Office/预览的现状）
   editorChanges() → 每 5 秒轮询摘要，变了就 listener({path, changed:true, unsaved:false})
                     （抽屉按它更新文件卡片；编辑器页自己也轮询，两边独立）
   ── 同源 fetch（Cookie 鉴权）──▶
acme 后端 mdedit 路由（/api/workspaces/{ws}/md-editor/...）：
   页面        GET  → 独立 HTML（无外部依赖、no-store）
   读          GET  …/file   → ctx.file_editing: locate(判对话归本人/附件只读) → read → {content, digest}
   存          PUT  …/file   → latest_digest 比对 base_digest（不一致且非 force → 409 带最新内容）
                              → save()（产物收集器存新版本 + 写回沙箱）→ {changed, digest, synced}
   摘要        GET  …/digest → {digest}（轮询用）
```

- **核心不碰 KnowWork**：读写、版本、写回沙箱、对话归属判定全部走 `ctx.file_editing`（`documents/editing.py`，v0.1.2 为扩展编辑开的正式口子）；扩展只决定「哪些格式能编辑」（`.md/.markdown/.mkd`，≤1 MB，UTF-8）。
- **鉴权**：会话身份 `principal(None)`（同记忆路由口径）+ `access.check(workspace:use)` 判空间成员；对话归属由 locate 判。
- **编辑器页是后端直出的独立 HTML**（vanilla JS，无外部依赖、无构建步骤），iframe 同源加载：不进 SPA 路由（避免应用外壳）、不受网关多端口问题影响（同 8090）。

## 3. 冲突与轮末语义（对齐官方）

- 编辑器页持有 `base_digest`（上次载入/保存时的摘要）与未保存标记；每 4 秒轮询 `latest_digest`。
- 变了且自己没改 → 静默重载（AI 的新版直接进来）；变了且自己有未保存改动 → 横幅二选一：「载入 AI 改过的版本」（丢弃自己的，载入新版）、「保留我的」（`force=true` 存盘，成为最新一版，AI 的中间版被覆盖——由人拍板）。
- 保存成功回 `genoffice:agent-edited`？不——那是 AI 改动落文档用的；人的保存直接更新本地状态即可。
- 后台任务到点、容器回收前做事这些 M2 再接（v0.1.2 的口子已在）。

## 4. 里程碑

**M1（本次）**：编辑器页（编辑+渲染预览+保存+冲突横幅+选中 AI 修改+ai-busy 高亮）、插槽注册（edit/editorChanges）、后端四条路由、单元测试（假 file_editing）、dev stack 冒烟（真内核链路：建会话→存 v1→读→冲突 409→force）。
**M2（后续可选）**：`saved`/`discardEdits` 语义对齐（编辑器内自动保存节奏）、Excel 类 CSV 的表格编辑、版本历史面板（同一路径多版对比）、交付卡片的「编辑」直达入口。

## 5. 非目标

- 富文本所见即所得（M1 是 Markdown 源码编辑 + 渲染预览）。
- 多人同时编辑同一份（对话文件是单人的；冲突只发生在人机之间）。
- Office 格式（等商业版或自研编辑器桥决策）。
