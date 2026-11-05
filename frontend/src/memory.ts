/**
 * 我的记忆（个人设置页签）的接口封装。路径与后端 acme.kw_enterprise.memory.api 一一对应；
 * 字段 camelCase，解析照 web/src/api 的 parse 工具，认不出的字段直接丢（向前兼容）。
 */

import { request, seg } from '@knowwork/web/api/http';
import { bool, isRecord, listOf, str, text } from '@knowwork/web/api/parse';

export type MemoryType = 'preference' | 'focus' | 'fact' | 'task';
export type MemoryStatus = 'active' | 'pending' | 'superseded' | 'archived';

export const TYPE_LABELS: Readonly<Record<MemoryType, string>> = {
  preference: '偏好',
  focus: '长期关注',
  fact: '事实',
  task: '在办事项',
};

export const SOURCE_LABELS: Readonly<Record<string, string>> = {
  extracted: '系统提取',
  user: '手动添加',
  tool: '会话记住',
};

export interface MemoryItem {
  readonly id: string;
  readonly type: MemoryType;
  readonly content: string;
  /** 提取时的用户原话（系统提取的才有；M2 接上提取后用于待确认页看出处） */
  readonly quote: string;
  readonly source: string;
  readonly status: MemoryStatus;
  readonly expiresAt: string | null;
  readonly createdAt: string | null;
}

function parseMemory(value: unknown): MemoryItem | undefined {
  if (!isRecord(value)) return undefined;
  const id = text(value.id);
  const content = text(value.content);
  if (!id || !content) return undefined;
  const type = str(value.type) as MemoryType | undefined;
  if (!type || !(type in TYPE_LABELS)) return undefined;
  return {
    id,
    type,
    content,
    quote: str(value.quote) ?? '',
    source: str(value.source) ?? 'extracted',
    status: (str(value.status) ?? 'active') as MemoryStatus,
    expiresAt: str(value.expiresAt) ?? null,
    createdAt: str(value.createdAt) ?? null,
  };
}

export interface MemoryCounts {
  readonly active: number;
  readonly pending: number;
  readonly superseded: number;
  readonly archived: number;
  readonly cap: number;
}

function parseCounts(value: unknown): MemoryCounts {
  if (!isRecord(value)) return { active: 0, pending: 0, superseded: 0, archived: 0, cap: 200 };
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  return {
    active: num(value.active),
    pending: num(value.pending),
    superseded: num(value.superseded),
    archived: num(value.archived),
    cap: num(value.cap) || 200,
  };
}

export interface MemoryPage {
  readonly items: readonly MemoryItem[];
  readonly total: number;
}

export async function listMemories(status: MemoryStatus, limit = 50, offset = 0): Promise<MemoryPage> {
  const body = await request<unknown>(
    `/api/memory?status=${encodeURIComponent(status)}&limit=${limit}&offset=${offset}`,
  );
  if (!isRecord(body)) return { items: [], total: 0 };
  return {
    items: listOf(body.items, parseMemory),
    total: typeof body.total === 'number' && Number.isFinite(body.total) ? body.total : 0,
  };
}

export async function memoryCounts(): Promise<MemoryCounts> {
  return parseCounts(await request<unknown>('/api/memory/counts'));
}

export async function addMemory(type: MemoryType, content: string): Promise<void> {
  await request('/api/memory', { method: 'POST', body: { type, content } });
}

export async function editMemory(
  id: string,
  changes: { content?: string; type?: MemoryType; expiresAt?: string; clearExpires?: boolean },
): Promise<void> {
  await request(`/api/memory/${seg(id)}`, { method: 'PATCH', body: changes });
}

export async function deleteMemory(id: string): Promise<void> {
  await request(`/api/memory/${seg(id)}`, { method: 'DELETE' });
}

export async function confirmMemory(id: string): Promise<void> {
  await request(`/api/memory/${seg(id)}/confirm`, { method: 'POST' });
}

export async function dismissMemory(id: string): Promise<void> {
  await request(`/api/memory/${seg(id)}/dismiss`, { method: 'POST' });
}

export async function restoreMemory(id: string): Promise<void> {
  await request(`/api/memory/${seg(id)}/restore`, { method: 'POST' });
}

export async function organizeNow(workspaceId: string): Promise<string> {
  const body = await request<unknown>('/api/memory/organize', {
    method: 'POST',
    body: { workspaceId },
  });
  return (isRecord(body) && str(body.jobId)) || '';
}

export async function memorySettings(): Promise<{ readonly enabled: boolean }> {
  const body = await request<unknown>('/api/memory/settings');
  return { enabled: isRecord(body) ? bool(body.enabled) : true };
}

export async function setMemorySettings(enabled: boolean): Promise<void> {
  await request('/api/memory/settings', { method: 'PUT', body: { enabled } });
}

export async function workspaceMemorySetting(workspaceId: string): Promise<{ readonly disabled: boolean }> {
  const body = await request<unknown>(`/api/workspaces/${seg(workspaceId)}/memory-setting`);
  return { disabled: isRecord(body) ? bool(body.disabled) : false };
}

export async function exportMemories(): Promise<readonly MemoryItem[]> {
  return listOf(await request<unknown>('/api/memory/export'), parseMemory);
}

export async function clearMemories(): Promise<void> {
  await request('/api/memory/clear', { method: 'POST' });
}

export const MEMORY_KEYS = {
  counts: ['memory', 'counts'] as const,
  list: (status: MemoryStatus) => ['memory', 'list', status] as const,
  settings: ['memory', 'settings'] as const,
  workspace: (workspaceId: string) => ['memory', 'workspace', workspaceId] as const,
};
