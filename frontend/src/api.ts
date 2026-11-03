/**
 * 扩展前端的接口封装。能用开源封装的就不复制：MCP 管理直接用 mcpAdminApi 工厂（我们后端的路由
 * 就是照它的方法清单设计的），技能的解析复用 parseSkill；空间管理、平台技能、收编这几个开源没有
 * 的接口在这里补。路径与后端 acme.kw_enterprise 的三组路由一一对应。
 */
import { request, seg } from '@knowwork/web/api/http';
import { listOf, isRecord, str, text, bool } from '@knowwork/web/api/parse';
import { parseSkill, type Skill } from '@knowwork/web/api/skills';
import {
  createBody,
  mcpAdminApi,
  parseServer,
  type McpServer,
  type ServerInput,
} from '@knowwork/web/api/mcp';

// ---- 空间管理（/api/system/workspaces） ----

export interface WorkspaceSummary {
  readonly id: string;
  readonly name: string;
  readonly createdAt: string;
  readonly owners: readonly string[];
  /** 默认空间（平台配置所在），界面不提供关闭入口 */
  readonly isDefault?: boolean;
}

function parseWorkspace(value: unknown): WorkspaceSummary | undefined {
  if (!isRecord(value)) return undefined;
  const id = text(value.id);
  if (!id) return undefined;
  return {
    id,
    name: str(value.name) ?? '',
    createdAt: str(value.created_at) ?? '',
    owners: listOf(value.owners, (item) => (typeof item === 'string' ? item : undefined)),
    isDefault: bool(value.isDefault),
  };
}

export async function listWorkspaces(): Promise<readonly WorkspaceSummary[]> {
  return listOf(await request<unknown>('/api/system/workspaces'), parseWorkspace);
}

export async function createWorkspace(name: string, ownerUserId: string): Promise<{ id: string; name: string }> {
  return await request('/api/system/workspaces', {
    method: 'POST',
    body: { name, ownerUserId },
  });
}

export async function renameWorkspace(id: string, name: string): Promise<void> {
  await request(`/api/system/workspaces/${seg(id)}`, { method: 'PUT', body: { name } });
}

export async function closeWorkspace(id: string): Promise<void> {
  await request(`/api/system/workspaces/${seg(id)}`, { method: 'DELETE' });
}

// ---- 企业技能（/api/platform-skills） ----

export interface SkillReleaseView {
  readonly id: string;
  readonly version: string;
  readonly releasedBy: string;
  readonly releasedAt: string | null;
}

function parseRelease(value: unknown): SkillReleaseView | undefined {
  if (!isRecord(value)) return undefined;
  const id = text(value.id);
  if (!id) return undefined;
  return {
    id,
    version: str(value.version) ?? '',
    releasedBy: str(value.releasedBy) ?? '',
    releasedAt: str(value.releasedAt) ?? null,
  };
}

export interface PromotableSkill {
  readonly workspaceId: string;
  readonly workspaceName: string;
  readonly skillId: string;
  readonly name: string;
  readonly description: string;
  readonly version: string;
}

function parsePromotable(value: unknown): PromotableSkill | undefined {
  if (!isRecord(value)) return undefined;
  const skillId = text(value.skillId);
  if (!skillId) return undefined;
  return {
    workspaceId: str(value.workspaceId) ?? '',
    workspaceName: str(value.workspaceName) ?? '',
    skillId,
    name: str(value.name) ?? '',
    description: str(value.description) ?? '',
    version: str(value.version) ?? '',
  };
}

/** 企业技能的 Skill.scope 会被 parseSkill 归一成 'workspace'，路径一律在这里显式拼，不用它。 */
const base = (path = '') => `/api/platform-skills${path}`;

/** 平台技能 = 开源的 Skill + audience（开源的 Skill 类型没有这个字段，parseSkill 会丢）。 */
export type PlatformSkill = Skill & { readonly audience?: readonly string[] };

function parsePlatformSkill(value: unknown): PlatformSkill | undefined {
  const skill = parseSkill(value);
  if (!skill) return undefined;
  if (!isRecord(value) || !Array.isArray(value.audience)) return skill;
  const audience = value.audience.filter((item): item is string => typeof item === 'string');
  return { ...skill, audience };
}

export async function listPlatformSkills(): Promise<readonly PlatformSkill[]> {
  const body = await request<unknown>(base());
  return listOf(isRecord(body) ? body.skills : undefined, parsePlatformSkill);
}

export async function getPlatformSkill(id: string): Promise<{ skill: PlatformSkill; releases: readonly SkillReleaseView[] }> {
  const body = await request<unknown>(base(`/${seg(id)}`));
  const skill = parsePlatformSkill(body);
  if (!skill) throw new Error('企业技能数据格式不对');
  return { skill, releases: listOf(isRecord(body) ? body.releases : undefined, parseRelease) };
}

export async function installPlatformSkill(archive: Blob): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base(), {
    method: 'POST',
    raw: archive,
    contentType: 'application/zip',
  }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

export async function replacePlatformDraft(id: string, draftRevision: number, archive: Blob): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base(`/${seg(id)}/draft`), {
    method: 'PUT',
    raw: archive,
    contentType: 'application/zip',
    headers: { 'If-Match': String(draftRevision) },
  }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

export async function publishPlatformSkill(id: string, draftRevision: number, version?: string): Promise<SkillReleaseView | undefined> {
  return parseRelease(await request<unknown>(base(`/${seg(id)}/publish`), {
    method: 'POST',
    body: version ? { version } : {},
    headers: { 'If-Match': String(draftRevision) },
  }));
}

export async function setPlatformEnabled(id: string, enabled: boolean): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base(`/${seg(id)}/${enabled ? 'enable' : 'disable'}`), {
    method: 'POST',
  }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

export async function setPlatformEnv(
  id: string,
  values: Readonly<Record<string, string | null>>,
): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base(`/${seg(id)}/env`), { method: 'PUT', body: { values } }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

export async function setPlatformAudience(id: string, workspaces: readonly string[]): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base(`/${seg(id)}/audience`), {
    method: 'PUT',
    body: { workspaces },
  }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

export async function deletePlatformSkill(id: string): Promise<void> {
  await request(base(`/${seg(id)}`), { method: 'DELETE' });
}

export async function listPromotableSkills(): Promise<readonly PromotableSkill[]> {
  return listOf(await request<unknown>(base('/promotable')), parsePromotable);
}

export async function promoteSkill(workspaceId: string, skillId: string): Promise<PlatformSkill> {
  const skill = parsePlatformSkill(await request<unknown>(base('/promote'), {
    method: 'POST',
    body: { workspaceId, skillId },
  }));
  if (!skill) throw new Error('企业技能数据格式不对');
  return skill;
}

// ---- 企业 MCP（/api/enterprise-mcp） ----

/** 列表、登记、探测、启停、同步、删除、单工具三档全部复用开源工厂，路由形态一致。 */
export const enterpriseMcpApi = mcpAdminApi((path = '') => `/api/enterprise-mcp${path}`);

export interface McpPromotable {
  readonly workspaceId: string;
  readonly workspaceName: string;
  readonly server: McpServer;
}

function parseMcpPromotable(value: unknown): McpPromotable | undefined {
  if (!isRecord(value)) return undefined;
  const workspace = isRecord(value.workspace) ? value.workspace : {};
  const server = parseServer(value.server);
  if (!server) return undefined;
  return {
    workspaceId: str(workspace.id) ?? '',
    workspaceName: str(workspace.name) ?? '',
    server,
  };
}

export async function listPromotableMcp(): Promise<readonly McpPromotable[]> {
  return listOf(await request<unknown>('/api/enterprise-mcp/promotable'), parseMcpPromotable);
}

/** 收编：连接信息用开源的 createBody 组请求体（上游注释明说扩展的收编也用它）。 */
export async function promoteMcp(workspaceId: string, serverId: string, input: ServerInput): Promise<McpServer | undefined> {
  return parseServer(await request<unknown>('/api/enterprise-mcp/promote', {
    method: 'POST',
    body: { workspaceId, serverId, server: createBody(input) },
  }));
}

export async function setMcpAudience(id: string, workspaces: readonly string[]): Promise<McpServer | undefined> {
  return parseServer(await request<unknown>(`/api/enterprise-mcp/${seg(id)}/audience`, {
    method: 'PUT',
    body: { workspaces },
  }));
}

// ---- 共用 ----

export const WORKSPACES_KEY = ['system', 'workspaces'] as const;
export const SKILL_KEYS = { list: ['admin', 'platform-skills'] } as const;
export const MCP_KEYS = { list: ['admin', 'enterprise-mcp'] } as const;
