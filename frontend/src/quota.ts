/**
 * 额度（用量配额）的接口封装。路径与后端 acme.kw_enterprise.quota.api 一一对应；
 * 字段 camelCase，解析照 web/src/api 的 parse 工具，认不出的字段直接丢（向前兼容）。
 */

import { request } from '@knowwork/web/api/http';
import { isRecord, num, listOf, str } from '@knowwork/web/api/parse';

export type QuotaMode = 'block' | 'warn';
export type QuotaSource = 'member' | 'default' | 'none';
export type QuotaStatus = 'ok' | 'near' | 'exhausted' | null;

export const MODE_LABELS: Readonly<Record<QuotaMode, string>> = {
  block: '超额时拦截',
  warn: '只提醒、不拦截',
};

export const SOURCE_LABELS: Readonly<Record<string, string>> = {
  member: '个人设置',
  default: '空间默认',
  none: '不限',
};

export const STATUS_LABELS: Readonly<Record<string, string>> = {
  ok: '正常',
  near: '接近上限',
  exhausted: '已用完',
};

export interface QuotaSetting {
  readonly workspaceId: string;
  readonly monthlyTotalTokens: number | null;
  readonly memberDefaultTokens: number | null;
  readonly mode: QuotaMode;
  readonly updatedBy: string | null;
  readonly updatedAt: string | null;
}

export interface QuotaMemberRow {
  readonly id: string;
  readonly name: string;
  readonly email: string;
  readonly kind: string;
  readonly role: string | null;
  readonly used: number;
  readonly tokens: number | null;
  readonly source: string;
  readonly grants: number;
  readonly effective: number | null;
  readonly mode: QuotaMode;
  readonly status: QuotaStatus;
}

export interface QuotaOverview {
  readonly workspaceId: string;
  readonly monthlyTotalTokens: number | null;
  readonly memberDefaultTokens: number | null;
  readonly mode: QuotaMode;
  readonly workspaceUsed: number;
  readonly resetsOn: string;
  readonly members: readonly QuotaMemberRow[];
}

export interface MyQuotaState {
  readonly workspaceId: string;
  readonly workspaceName: string;
  readonly used: number;
  readonly tokens: number | null;
  readonly effective: number | null;
  readonly grants: number;
  readonly mode: QuotaMode;
  readonly status: QuotaStatus;
  readonly resetsOn: string;
  readonly workspaceTotalTokens: number | null;
  readonly workspaceMode: QuotaMode;
}

export const GROUP_TYPE_LABELS: Readonly<Record<string, string>> = {
  department: '部门',
  user_group: '用户组',
};

export interface QuotaGroupRow {
  readonly groupType: string;
  readonly groupId: string;
  readonly name: string;
  readonly members: number;
  readonly monthlyTokens: number | null;
  readonly mode: QuotaMode;
}

export interface QuotaSuggestion {
  readonly days: number;
  readonly users: number;
  readonly average: number;
  readonly median: number;
  readonly p90: number;
  readonly max: { readonly userId: string; readonly name: string; readonly tokens: number } | null;
  readonly suggestedDefault: number | null;
  readonly perTurnTokens: number | null;
  readonly quotaTurns: number | null;
  readonly costTotal: number;
  readonly quotaCost: number | null;
}

export interface QuotaChangeRow {
  readonly id: number;
  readonly target: string;
  readonly targetId: string | null;
  readonly before: Record<string, unknown> | null;
  readonly after: Record<string, unknown> | null;
  readonly changedBy: string;
  readonly changedAt: string | null;
}

export interface QuotaGrantItem {
  readonly grantId: string;
  readonly userId: string;
  readonly userName: string;
  readonly month: string;
  readonly extraTokens: number;
  readonly reason: string;
  readonly grantedBy: string;
  readonly grantedAt: string | null;
}

function parseSetting(value: unknown): QuotaSetting | undefined {
  if (!isRecord(value)) return undefined;
  const workspaceId = str(value.workspaceId);
  if (!workspaceId) return undefined;
  return {
    workspaceId,
    monthlyTotalTokens: num(value.monthlyTotalTokens) ?? null,
    memberDefaultTokens: num(value.memberDefaultTokens) ?? null,
    mode: (str(value.mode) ?? 'block') as QuotaMode,
    updatedBy: str(value.updatedBy) ?? null,
    updatedAt: str(value.updatedAt) ?? null,
  };
}

function parseMemberRow(value: unknown): QuotaMemberRow | undefined {
  if (!isRecord(value)) return undefined;
  const id = str(value.id);
  if (!id) return undefined;
  return {
    id,
    name: str(value.name) ?? id,
    email: str(value.email) ?? '',
    kind: str(value.kind) ?? 'person',
    role: str(value.role) ?? null,
    used: num(value.used) ?? 0,
    tokens: num(value.tokens) ?? null,
    source: str(value.source) ?? 'none',
    grants: num(value.grants) ?? 0,
    effective: num(value.effective) ?? null,
    mode: (str(value.mode) ?? 'block') as QuotaMode,
    status: (str(value.status) ?? null) as QuotaStatus,
  };
}

function parseOverview(value: unknown): QuotaOverview | undefined {
  if (!isRecord(value)) return undefined;
  const workspaceId = str(value.workspaceId);
  if (!workspaceId) return undefined;
  return {
    workspaceId,
    monthlyTotalTokens: num(value.monthlyTotalTokens) ?? null,
    memberDefaultTokens: num(value.memberDefaultTokens) ?? null,
    mode: (str(value.mode) ?? 'block') as QuotaMode,
    workspaceUsed: num(value.workspaceUsed) ?? 0,
    resetsOn: str(value.resetsOn) ?? '',
    members: listOf(value.members, parseMemberRow),
  };
}

function parseMyState(value: unknown): MyQuotaState | undefined {
  if (!isRecord(value)) return undefined;
  const workspaceId = str(value.workspaceId);
  if (!workspaceId) return undefined;
  return {
    workspaceId,
    workspaceName: str(value.workspaceName) ?? workspaceId,
    used: num(value.used) ?? 0,
    tokens: num(value.tokens) ?? null,
    effective: num(value.effective) ?? null,
    grants: num(value.grants) ?? 0,
    mode: (str(value.mode) ?? 'block') as QuotaMode,
    status: (str(value.status) ?? null) as QuotaStatus,
    resetsOn: str(value.resetsOn) ?? '',
    workspaceTotalTokens: num(value.workspaceTotalTokens) ?? null,
    workspaceMode: (str(value.workspaceMode) ?? 'block') as QuotaMode,
  };
}

function parseGrant(value: unknown): QuotaGrantItem | undefined {
  if (!isRecord(value)) return undefined;
  const grantId = str(value.grantId);
  const userId = str(value.userId);
  if (!grantId || !userId) return undefined;
  return {
    grantId,
    userId,
    userName: str(value.userName) ?? userId,
    month: str(value.month) ?? '',
    extraTokens: num(value.extraTokens) ?? 0,
    reason: str(value.reason) ?? '',
    grantedBy: str(value.grantedBy) ?? '',
    grantedAt: str(value.grantedAt) ?? null,
  };
}

export async function quotaSetting(workspaceId: string): Promise<QuotaSetting | null> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/setting`);
  return parseSetting(body) ?? null;
}

export async function quotaOverview(workspaceId: string): Promise<QuotaOverview | null> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/overview`);
  return parseOverview(body) ?? null;
}

export async function setQuotaSetting(
  workspaceId: string,
  body: { monthlyTotalTokens?: number | null; memberDefaultTokens?: number | null; mode?: QuotaMode },
): Promise<void> {
  await request(`/api/workspaces/${workspaceId}/quota/setting`, { method: 'PUT', body });
}

export async function setMemberQuota(
  workspaceId: string,
  userId: string,
  monthlyTokens: number | null,
  mode: QuotaMode,
): Promise<void> {
  await request(`/api/workspaces/${workspaceId}/quota/member/${userId}`, {
    method: 'PUT',
    body: { monthlyTokens, mode },
  });
}

export async function addQuotaGrant(
  workspaceId: string,
  userId: string,
  extraTokens: number,
  reason: string,
): Promise<void> {
  await request(`/api/workspaces/${workspaceId}/quota/grants`, {
    method: 'POST',
    body: { userId, extraTokens, reason },
  });
}

export async function quotaGrants(workspaceId: string): Promise<readonly QuotaGrantItem[]> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/grants`);
  return listOf(body, parseGrant);
}

export async function myQuota(): Promise<readonly MyQuotaState[]> {
  const body = await request<unknown>('/api/quota/me');
  return listOf(body, parseMyState);
}

export async function quotaStatus(workspaceId: string): Promise<MyQuotaState | null> {
  const body = await request<unknown>(
    `/api/quota/status?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  return parseMyState(body) ?? null;
}

export async function quotaGroups(workspaceId: string): Promise<readonly QuotaGroupRow[]> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/groups`);
  return listOf(body, parseGroup);
}

function parseGroup(value: unknown): QuotaGroupRow | undefined {
  if (!isRecord(value)) return undefined;
  const groupId = str(value.groupId);
  const groupType = str(value.groupType);
  if (!groupId || !groupType) return undefined;
  return {
    groupType,
    groupId,
    name: str(value.name) ?? groupId,
    members: num(value.members) ?? 0,
    monthlyTokens: num(value.monthlyTokens) ?? null,
    mode: (str(value.mode) ?? 'block') as QuotaMode,
  };
}

export async function setGroupQuota(
  workspaceId: string,
  groupType: string,
  groupId: string,
  monthlyTokens: number | null,
  mode: QuotaMode,
): Promise<void> {
  await request(`/api/workspaces/${workspaceId}/quota/group/${groupType}/${groupId}`, {
    method: 'PUT',
    body: { monthlyTokens, mode },
  });
}

export async function quotaSuggestion(workspaceId: string): Promise<QuotaSuggestion | null> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/suggestion`);
  if (!isRecord(body)) return null;
  const max = isRecord(body.max) ? {
    userId: str(body.max.userId) ?? '',
    name: str(body.max.name) ?? '',
    tokens: num(body.max.tokens) ?? 0,
  } : null;
  return {
    days: num(body.days) ?? 30,
    users: num(body.users) ?? 0,
    average: num(body.average) ?? 0,
    median: num(body.median) ?? 0,
    p90: num(body.p90) ?? 0,
    max,
    suggestedDefault: num(body.suggestedDefault) ?? null,
    perTurnTokens: num(body.perTurnTokens) ?? null,
    quotaTurns: num(body.quotaTurns) ?? null,
    costTotal: num(body.costTotal) ?? 0,
    quotaCost: num(body.quotaCost) ?? null,
  };
}

function parseChange(value: unknown): QuotaChangeRow | undefined {
  if (!isRecord(value)) return undefined;
  const id = num(value.id);
  if (id === undefined) return undefined;
  return {
    id,
    target: str(value.target) ?? '',
    targetId: str(value.targetId) ?? null,
    before: isRecord(value.before) ? value.before : null,
    after: isRecord(value.after) ? value.after : null,
    changedBy: str(value.changedBy) ?? '',
    changedAt: str(value.changedAt) ?? null,
  };
}

export async function quotaChanges(workspaceId: string): Promise<readonly QuotaChangeRow[]> {
  const body = await request<unknown>(`/api/workspaces/${workspaceId}/quota/changes`);
  return listOf(body, parseChange);
}
