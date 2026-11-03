/**
 * 「工具与 MCP → 企业」页签里系统管理员看到的管理视图：复用开源的 ServerList（列表、登记对话框、
 * 详情抽屉全在开源），只换两样——api 指到企业接口（scope.ts 的 McpAdminScope），多一列「发给哪些空间」
 * 和收编入口。非系统管理员看不到这个面板（插槽的渲染分支在开源的 HereMcp.tsx）。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@openai/apps-sdk-ui/components/Button';
import { Input } from '@openai/apps-sdk-ui/components/Input';
import { readableError } from '@knowwork/web/api/http';
import {
  SERVER_NAME,
  validateServerInput,
  type McpServer,
  type ServerAuth,
  type ServerInput,
} from '@knowwork/web/api/mcp';
import { Dialog, DialogActions } from '@knowwork/web/ui/Dialog';
import { Field } from '@knowwork/web/ui/Field';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { toast } from '@knowwork/web/ui/toastStore';
import { ServerList } from '@knowwork/web/features/admin/tools/ToolsPage';
import { McpScopeContext, type McpAdminScope } from '@knowwork/web/features/admin/tools/scope';
import { RowActions } from '@knowwork/web/features/admin/common';
import {
  enterpriseMcpApi,
  listPromotableMcp,
  promoteMcp,
  setMcpAudience,
  MCP_KEYS,
  type McpPromotable,
} from '../api';
import { AudienceEditor, audienceText, useSetAudience, useWorkspaces } from '../audience';

/** 发放的「改发放」动作列：展示那一列由 reach（发给哪些空间）负责，这里只放操作，避免重复。 */
function useAudienceExtra() {
  const setAudience = useSetAudience(setMcpAudience, [[...MCP_KEYS.list]]);
  const [editing, setEditing] = useState<McpServer | null>(null);
  return {
    extra: {
      title: '发放',
      cell: (server: McpServer) => (
        <RowActions actions={[{ key: 'audience', label: '改发放', onClick: () => setEditing(server) }]} />
      ),
      actionsOf: (server: McpServer) => [
        { key: 'audience', label: '改发放', onClick: () => setEditing(server) },
      ],
      dialog: editing ? (
        <AudienceEditor
          title={`「${editing.title}」发给哪些空间`}
          audience={editing.audience}
          pending={setAudience.pending}
          onSave={(list) => setAudience.run({ id: editing.id, workspaces: list })}
          onClose={() => setEditing(null)}
        />
      ) : null,
    },
  } as const;
}

/** 从空间 MCP 收编：选来源 → 连接信息预填（凭据要系统管理员重填）→ promote。 */
function PromoteDialog({ onClose }: { readonly onClose: () => void }) {
  const client = useQueryClient();
  const sources = useQuery({ queryKey: ['admin', 'enterprise-mcp-promotable'], queryFn: listPromotableMcp });
  const [source, setSource] = useState<McpPromotable | null>(null);
  const [form, setForm] = useState<ServerInput>(() => ({
    name: '',
    title: '',
    url: '',
    timeoutSeconds: 60,
    auth: 'shared',
    headers: [],
  }));
  const [token, setToken] = useState('');
  const [headersText, setHeadersText] = useState('');
  const promote = useMutation({
    mutationFn: () => {
      const headers = headersText
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const index = line.indexOf(':');
          return index > 0 ? { name: line.slice(0, index).trim(), value: line.slice(index + 1).trim() } : null;
        })
        .filter((header): header is { name: string; value: string } => header !== null);
      const input: ServerInput = { ...form, headers, ...(form.auth === 'user_token' && token ? { token } : {}) };
      const invalid = validateServerInput(input, true);
      if (invalid) throw new Error(invalid);
      return promoteMcp(source!.workspaceId, source!.server.id, input);
    },
    onSuccess: async (server) => {
      await client.invalidateQueries({ queryKey: [...MCP_KEYS.list] });
      toast.success(`已收为企业 MCP「${server?.title ?? ''}」，还没发给任何空间，先在列表里改发放`);
      onClose();
    },
    onError: (cause) => toast.error(readableError(cause, '没有收编成功')),
  });
  const pick = (item: McpPromotable) => {
    setSource(item);
    const suggested = `ent-${item.server.name}`.replace(/[^a-z0-9-]/g, '').slice(0, 24);
    setForm({
      name: SERVER_NAME.test(suggested) ? suggested : '',
      title: item.server.title,
      url: item.server.url,
      timeoutSeconds: item.server.timeoutSeconds,
      auth: item.server.auth,
      headers: [],
    });
  };
  return (
    <Dialog
      title="从空间 MCP 收为企业 MCP"
      onClose={onClose}
      size="md"
      onSubmit={() => void promote.mutate()}
      footer={
        <DialogActions
          okText="收编"
          loading={promote.isPending}
          disabled={source === null || form.name === '' || form.title.trim() === '' || form.url.trim() === ''}
          onCancel={onClose}
        />
      }
    >
      {!source ? (
        sources.isPending ? (
          <Skeleton rows={4} />
        ) : sources.isError ? (
          <p className="kw-secondary-text">{readableError(sources.error, '空间 MCP 列表没有加载出来')}</p>
        ) : (sources.data ?? []).length === 0 ? (
          <p className="kw-secondary-text">各空间还没有登记 MCP 服务。</p>
        ) : (
          (sources.data ?? []).map((item) => (
            <button
              key={`${item.workspaceId}/${item.server.id}`}
              type="button"
              className="kw-list-item"
              onClick={() => pick(item)}
            >
              <span>{item.server.title}</span>
              <span className="kw-secondary-text">{`${item.workspaceName} · ${item.server.url}`}</span>
            </button>
          ))
        )
      ) : (
        <>
          <p className="kw-detail-note">
            来源：{source.workspaceName} 的「{source.server.title}」。工具的允许/需确认/禁用会按原名带过来；
            共享凭据、OAuth 应用密钥要重填（那是那个部门的账号，不随收编走）。标识要用新的名字。
          </p>
          <Field label="标识" help="小写字母开头，小写字母、数字、连字符，最多 24 个字符" required>
            {(id) => (
              <Input id={id} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            )}
          </Field>
          <Field label="名称" required>
            {(id) => (
              <Input id={id} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            )}
          </Field>
          <Field label="地址" required>
            {(id) => <Input id={id} value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />}
          </Field>
          <Field label="认证方式">
            {(id) => (
              <select
                id={id}
                value={form.auth ?? 'shared'}
                onChange={(e) => setForm({ ...form, auth: e.target.value as ServerAuth })}
                className="kw-select"
              >
                <option value="shared">共享凭据（请求头）</option>
                <option value="user_token">个人访问令牌</option>
              </select>
            )}
          </Field>
          {(form.auth ?? 'shared') === 'shared' ? (
            <Field label="请求头" help="每行一条，格式 Name: Value">
              {(id) => (
                <textarea
                  id={id}
                  className="kw-textarea"
                  rows={3}
                  value={headersText}
                  onChange={(e) => setHeadersText(e.target.value)}
                  placeholder="Authorization: Bearer …"
                />
              )}
            </Field>
          ) : (
            <Field label="访问令牌" help="登记人自己的令牌，只用来测试连接、拉工具清单" required>
              {(id) => (
                <Input
                  id={id}
                  type="password"
                  autoComplete="new-password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
              )}
            </Field>
          )}
        </>
      )}
    </Dialog>
  );
}

export function ToolsEnterprisePanel() {
  const workspaces = useWorkspaces();
  const { extra } = useAudienceExtra();
  const [promoting, setPromoting] = useState(false);
  return (
    <McpScopeContext.Provider
      value={
        {
          kind: 'enterprise',
          api: enterpriseMcpApi,
          key: [...MCP_KEYS.list],
          reach: {
            title: '发给哪些空间',
            Cell: ({ server }) => <>{audienceText(server.audience, workspaces.data)}</>,
            Row: ({ server }) => <>{audienceText(server.audience, workspaces.data)}</>,
          },
          returnTo: '/admin/tools',
          deleteConfirm: (server) =>
            `删除企业 MCP「${server.title}」？发到的各空间将不再提供它的工具。各空间针对它的停用/收窄设置随服务删除；收窄时存的授权记录会残留，但服务名不再指向它，无害。`,
          useExtra: () => extra,
        } satisfies McpAdminScope
      }
    >
      <ServerList addLabel="接入企业 MCP 服务" toolbarExtra={<PromoteToolbarButton onClick={() => setPromoting(true)} />} />
      {promoting ? <PromoteDialog onClose={() => setPromoting(false)} /> : null}
    </McpScopeContext.Provider>
  );
}

function PromoteToolbarButton({ onClick }: { readonly onClick: () => void }) {
  return (
    <Button color="secondary" onClick={onClick} aria-label="从空间 MCP 收编">
      从空间 MCP 收编…
    </Button>
  );
}
