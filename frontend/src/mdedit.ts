/**
 * Markdown 人机双改：给云端对话的 .md 文件补上「在网页里编辑」（chat.files.server 插槽）。
 * 编辑器页由后端直出（/api/workspaces/{ws}/md-editor），这里只负责打开地址与把「AI 改了这份文件」
 * 轮询给外层的文件卡片（EditedFile：changed；unsaved 编辑器自己管，抽屉侧始终 false）。
 */

import { currentWorkspaceId } from '@knowwork/web/stores/workspace';
import type { EditedFile } from '@knowwork/web/features/chat/sources';
import { request } from '@knowwork/web/api/http';
import { isRecord, str } from '@knowwork/web/api/parse';

const EDITABLE = /\.(md|markdown|mkd)$/i;
const POLL_MS = 5000;

/** 已打开过的编辑器（按对话记）：路径 → 上次看到的摘要；editorChanges 轮询拿它比对 */
const opened = new Map<string, Map<string, string>>();

async function digestOf(conversationId: string, workspaceId: string, path: string): Promise<string | null> {
  const body = await request<unknown>(
    `/api/workspaces/${workspaceId}/md-editor/digest?conversation_id=${encodeURIComponent(conversationId)}&path=${encodeURIComponent(path)}`,
  );
  return isRecord(body) ? (str(body.digest) ?? null) : null;
}

export const mdFileEditing = {
  /** .md/.markdown/.mkd 返回编辑器页地址（预览抽屉直接当编辑器嵌）；别的格式 null，维持只读预览 */
  async edit(conversationId: string, path: string): Promise<{ readonly url: string } | null> {
    const workspaceId = currentWorkspaceId();
    if (!workspaceId || !EDITABLE.test(path)) return null;
    const digest = await digestOf(conversationId, workspaceId, path).catch(() => null);
    let paths = opened.get(conversationId);
    if (!paths) {
      paths = new Map();
      opened.set(conversationId, paths);
    }
    paths.set(path, digest ?? '');
    return {
      url: `/api/workspaces/${workspaceId}/md-editor?conversation_id=${encodeURIComponent(conversationId)}&path=${encodeURIComponent(path)}`,
    };
  },

  /** 外层每轮/按需来订阅：轮询各路径的摘要，变了就报 changed（AI 在这轮里改了它） */
  editorChanges(
    conversationId: string,
    listener: (files: readonly EditedFile[]) => void,
  ): () => void {
    const timer = setInterval(async () => {
      const paths = opened.get(conversationId);
      if (!paths || paths.size === 0) return;
      const workspaceId = currentWorkspaceId();
      if (!workspaceId) return;
      const changed: EditedFile[] = [];
      for (const [path, seen] of paths) {
        try {
          const digest = await digestOf(conversationId, workspaceId, path);
          if (digest !== null && digest !== seen) {
            paths.set(path, digest);
            changed.push({ path, changed: true, unsaved: false });
          }
        } catch {
          /* 文件没了或瞬时失败：下一轮再试 */
        }
      }
      if (changed.length > 0) listener(changed);
    }, POLL_MS);
    return () => clearInterval(timer);
  },
};
