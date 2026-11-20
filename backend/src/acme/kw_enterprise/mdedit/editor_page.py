"""Markdown 编辑器页：后端直出的独立 HTML（同源 iframe 里跑，带会话 Cookie）。

无外部依赖、无构建步骤； spoken 协议照 GenOffice 补丁 6（editorBridge.ts）：
- 发 genoffice:ready（载入/重载后），外层会把「AI 正在改」的块区间再发一次；
- 发 genoffice:ai-edit（选中一段 → AI 修改），外层交给对话输入框；
- 收 genoffice:ai-busy（块区间），渲染窗格里那几段高亮「AI 正在改」。
不真实现 genoffice:saved（保存直接走本扩展的 PUT 存回对话，成为新的一版）。
"""

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Markdown 编辑</title>
<style>
  :root { --line: #e5e5e5; --muted: #8a8a8a; --accent: #1a7f37; --warn: #b45309; --danger: #b91c1c; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: #222;
         display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid var(--line); }
  header .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  header .status { color: var(--muted); font-size: 12px; }
  header .status.dirty { color: var(--warn); }
  header .spacer { flex: 1; }
  button { font: inherit; padding: 5px 14px; border-radius: 6px; border: 1px solid var(--line);
           background: #fff; cursor: pointer; }
  button.primary { background: #222; color: #fff; border-color: #222; }
  button:disabled { opacity: .5; cursor: default; }
  #banner { display: none; padding: 10px 14px; background: #fffbeb; border-bottom: 1px solid #fde68a;
            color: var(--warn); }
  #banner button { margin-left: 8px; }
  main { flex: 1; display: flex; min-height: 0; }
  #editor, #preview { flex: 1; min-width: 0; overflow: auto; }
  #editor { border-right: 1px solid var(--line); padding: 16px 18px; border: none; resize: none;
            font: 13px/1.7 ui-monospace, "SF Mono", Consolas, monospace; outline: none; }
  #preview { padding: 16px 22px; background: #fafafa; }
  #preview .block { border-radius: 4px; padding: 0 6px; margin: 0 -6px; transition: background .3s; }
  #preview .block.busy { background: #fef3c7; outline: 1px solid #f59e0b; }
  #preview h1, #preview h2, #preview h3 { line-height: 1.3; }
  #preview pre { background: #f0f0f0; padding: 10px 12px; border-radius: 6px; overflow: auto; }
  #preview code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }
  #preview pre code { background: none; padding: 0; }
  #preview blockquote { margin: 0; padding: 2px 12px; border-left: 3px solid var(--line); color: #555; }
  #ai-form { display: none; gap: 8px; padding: 8px 14px; border-bottom: 1px solid var(--line); background: #f6fef8; }
  #ai-form.open { display: flex; }
  #ai-form input { flex: 1; font: inherit; padding: 5px 10px; border: 1px solid var(--line); border-radius: 6px; }
  #ai-form label { display: flex; align-items: center; gap: 4px; color: var(--muted); font-size: 13px; white-space: nowrap; }
  footer { padding: 6px 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px;
           display: flex; gap: 14px; }
</style>
</head>
<body>
<header>
  <span class="name" id="name"></span>
  <span class="status" id="status">载入中…</span>
  <span class="spacer"></span>
  <button id="ai-btn" title="选中一段，交给这段对话的智能体改">AI 修改</button>
  <button id="save-btn" class="primary">保存</button>
</header>
<div id="ai-form">
  <input id="ai-instruction" placeholder="要让 AI 改选中的这段的哪里？（例如：更简洁、数字不动）">
  <label><input type="checkbox" id="ai-send"> 立即发送</label>
  <button id="ai-ok">交给 AI</button>
  <button id="ai-cancel">取消</button>
</div>
<div id="banner">
  <span id="banner-text">AI 改了这个文件，这里还有没保存的改动</span>
  <button id="banner-mine">保留我的</button>
  <button id="banner-theirs" class="primary">载入 AI 改过的版本</button>
</div>
<main>
  <textarea id="editor" spellcheck="false" placeholder="载入中…"></textarea>
  <div id="preview"></div>
</main>
<footer>
  <span>人改的和 AI 改的都在这一份里；保存即成为对话里的新一版</span>
  <span id="busy" style="color: var(--warn)"></span>
</footer>
<script>
'use strict';
const params = new URLSearchParams(location.search);
const conversationId = params.get('conversation_id') || '';
const path = params.get('path') || '';
const WS = location.pathname.split('/')[3];
const BASE = `/api/workspaces/${WS}/md-editor`;
const editor = document.getElementById('editor');
const preview = document.getElementById('preview');
const statusEl = document.getElementById('status');
const banner = document.getElementById('banner');
const busyEl = document.getElementById('busy');

let baseDigest = '';       // 上次载入/保存时的摘要
let busyRanges = [];       // AI 正在改的块区间（外层发来）
let pollTimer = null;
let saving = false;

const api = (suffix, options) => fetch(`${BASE}${suffix}`, options).then(async (response) => {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.message || '请求失败');
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
});

/* ---- Markdown 渲染（最小集）：按块渲染并标 data-block，供 ai-busy 高亮与选区换算 ---- */
function splitBlocks(text) {
  const lines = text.split('\\n');
  const blocks = [];
  let current = [];
  let fence = false;
  for (const line of lines) {
    if (/^\\s*```/.test(line)) fence = !fence;
    if (!fence && line.trim() === '' && current.length) { blocks.push(current.join('\\n')); current = []; continue; }
    current.push(line);
  }
  if (current.length) blocks.push(current.join('\\n'));
  return blocks;
}
function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function inline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
    .replace(/\\*([^*]+)\\*/g, '<em>$1</em>')
    .replace(/\\[([^\\]]+)\\]\\(([^)\\s]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function renderBlock(block) {
  if (/^\\s*```/.test(block)) {
    const lines = block.split('\\n').slice(1);
    if (lines.length && /^\\s*```/.test(lines[lines.length - 1])) lines.pop();
    return `<pre><code>${escapeHtml(lines.join('\\n'))}</code></pre>`;
  }
  const heading = block.match(/^(#{1,6})\\s+(.*)$/);
  if (heading) return `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
  if (/^(---|\\*\\*\\*|___)\\s*$/.test(block)) return '<hr>';
  if (/^\\s*&gt;/.test(escapeHtml(block))) {
    const quoted = block.split('\\n').map((line) => line.replace(/^\\s*>\\s?/, '')).join('\\n');
    return `<blockquote>${inline(quoted)}</blockquote>`;
  }
  if (/^\\s*([-*+]|\\d+\\.)\\s+/.test(block)) {
    const ordered = /^\\s*\\d+\\./.test(block);
    const items = block.split('\\n').map((line) => line.replace(/^\\s*([-*+]|\\d+\\.)\\s+/, ''));
    const tag = ordered ? 'ol' : 'ul';
    return `<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join('')}</${tag}>`;
  }
  return block.split('\\n').map((line) => `<p>${inline(line) || '<br>'}</p>`).join('');
}
function render(content) {
  const blocks = splitBlocks(content);
  preview.innerHTML = blocks.map((block, index) =>
    `<div class="block" data-block="${index}">${renderBlock(block)}</div>`).join('');
  markBusy();
}

/* ---- 选区 → 块区间（照 GenOffice 补丁 6 的 Word 形态：isRange + start/endBlockIndex + text） ---- */
function selectionBlocks() {
  const start = editor.selectionStart;
  const end = editor.selectionEnd;
  if (start === end) return null;
  const starts = splitBlocks(editor.value.slice(0, start));
  const selected = editor.value.slice(start, end);
  const total = splitBlocks(editor.value.slice(0, end)).length;
  const startBlock = starts.length - 1;
  const endBlock = Math.max(startBlock, total - 1);
  return { text: selected, startBlockIndex: Math.max(0, startBlock), endBlockIndex: endBlock };
}

/* ---- genoffice 桥（editorBridge.ts 的协议）---- */
function post(message) { window.parent.postMessage(message, '*'); }
window.addEventListener('message', (event) => {
  const message = event.data;
  if (typeof message !== 'object' || message === null) return;
  if (message.type === 'genoffice:ai-busy') {
    busyRanges = Array.isArray(message.ranges) ? message.ranges : [];
    busyEl.textContent = busyRanges.length ? `AI 正在改第 ${busyRanges.map((r) => r.start + 1).join('、')} 段…` : '';
    markBusy();
  }
});
function markBusy() {
  const first = busyRanges.length ? busyRanges[0].start : null;
  preview.querySelectorAll('.block').forEach((node) => {
    const index = Number(node.dataset.block);
    const hit = busyRanges.some((range) => index >= range.start && index <= range.end);
    node.classList.toggle('busy', hit);
    if (hit && index === first) node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });
}

/* ---- 「AI 修改」表单 ---- */
const aiForm = document.getElementById('ai-form');
const aiInput = document.getElementById('ai-instruction');
document.getElementById('ai-btn').addEventListener('click', () => {
  const chosen = selectionBlocks();
  if (!chosen) { status('先选中要改的那段'); return; }
  aiForm.classList.add('open');
  aiInput.focus();
});
document.getElementById('ai-cancel').addEventListener('click', () => aiForm.classList.remove('open'));
document.getElementById('ai-ok').addEventListener('click', () => {
  const chosen = selectionBlocks();
  const instruction = aiInput.value.trim();
  if (!chosen || !instruction) { status('选中一段、写一句要求'); return; }
  post({ type: 'genoffice:ai-edit', instruction, send: document.getElementById('ai-send').checked,
         selection: { text: chosen.text, isRange: true,
                      startBlockIndex: chosen.startBlockIndex, endBlockIndex: chosen.endBlockIndex } });
  aiForm.classList.remove('open');
  aiInput.value = '';
  status('已交给这段对话的智能体');
});

/* ---- 载入 / 保存 / 冲突 ---- */
function status(text, dirty = null) {
  statusEl.textContent = text;
  if (dirty !== null) statusEl.classList.toggle('dirty', dirty);
}
async function load() {
  const file = await api(`/file?conversation_id=${encodeURIComponent(conversationId)}&path=${encodeURIComponent(path)}`);
  document.getElementById('name').textContent = file.name;
  editor.value = file.content;
  baseDigest = file.digest;
  render(file.content);
  status('已载入最新版', false);
  post({ type: 'genoffice:ready' });
}
async function save(force) {
  if (saving) return;
  saving = true;
  document.getElementById('save-btn').disabled = true;
  status('保存中…', true);
  try {
    const result = await api('/file', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId, path, content: editor.value,
                             base_digest: baseDigest, force: force === true }) });
    baseDigest = result.digest;
    banner.style.display = 'none';
    status(result.changed ? '已存成对话里的新一版' : '内容没变，没有另存', false);
  } catch (error) {
    if (error.status === 409) {
      banner.style.display = 'block';
      status('与 AI 的版本冲突', true);
    } else {
      status(error.message || '没能保存', true);
    }
  } finally {
    saving = false;
    document.getElementById('save-btn').disabled = false;
  }
}
document.getElementById('save-btn').addEventListener('click', () => save(false));
document.getElementById('banner-theirs').addEventListener('click', async () => {
  banner.style.display = 'none';
  await load();
  status('已载入 AI 改过的版本（你未保存的改动被丢弃）', false);
});
document.getElementById('banner-mine').addEventListener('click', () => { banner.style.display = 'none'; save(true); });
editor.addEventListener('input', () => {
  render(editor.value);
  status(baseDigest && editor.value !== '' ? '有没保存的改动（⌘S / Ctrl+S 保存）' : '', true);
});
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 's') { event.preventDefault(); save(false); }
});
window.addEventListener('beforeunload', (event) => {
  if (statusEl.classList.contains('dirty')) { event.preventDefault(); event.returnValue = ''; }
});

/* ---- 轮询：AI 改了这份文件就发现 ---- */
async function poll() {
  if (saving) return;
  try {
    const state = await api(`/digest?conversation_id=${encodeURIComponent(conversationId)}&path=${encodeURIComponent(path)}`);
    if (state.digest === baseDigest) return;
    if (statusEl.classList.contains('dirty') && editor.value !== '' && banner.style.display !== 'block') {
      banner.style.display = 'block';  // 人有没保存的改动：横幅由人拍板
    } else if (!statusEl.classList.contains('dirty')) {
      await load();                    // 自己没动：静默载入 AI 的新版
    }
  } catch (_) { /* 瞬时失败下一轮再试 */ }
}

load().catch((error) => { status(error.message || '没能打开这个文件', true); editor.placeholder = ''; });
pollTimer = setInterval(poll, 4000);
</script>
</body>
</html>
"""
