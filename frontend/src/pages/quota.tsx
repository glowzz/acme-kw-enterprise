/**
 * 管理后台 → 用量 → 「额度」标签页（admin.usage.tabs 插槽）：四卡（空间月总额 / 个人默认额度 /
 * 已分配合计 / 下次恢复）+ 按成员列表（本月已用、额度来自、状态）+ 设额度与临时加额。
 * 空间月总额只有系统管理员能改（后端判，界面上的「修改」失败会提示）。
 * 对齐官方商业版口径（docs/额度层详细设计.md §1）；部门与用户组分配、参考建议值、变更记录在 M2。
 */
import { useState } from 'react';
import { Button } from '@openai/apps-sdk-ui/components/Button';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { readableError } from '@knowwork/web/api/http';
import { useWorkspaceStore } from '@knowwork/web/stores/workspace';
import { Dialog, DialogActions } from '@knowwork/web/ui/Dialog';
import { Empty } from '@knowwork/web/ui/Empty';
import { Field } from '@knowwork/web/ui/Field';
import { Input } from '@openai/apps-sdk-ui/components/Input';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { Table, type Column } from '@knowwork/web/ui/Table';
import { toast } from '@knowwork/web/ui/toastStore';
import { Select } from '@openai/apps-sdk-ui/components/Select';
import {
  MODE_LABELS,
  SOURCE_LABELS,
  STATUS_LABELS,
  addQuotaGrant,
  quotaOverview,
  quotaSetting,
  setMemberQuota,
  setQuotaSetting,
  type QuotaMemberRow,
  type QuotaMode,
} from '../quota';

/** token 的说法：万为单位取一位小数，四舍五入（官方口径 435.7 万 / 700 万）。 */
function wan(tokens: number | null): string {
  if (tokens === null) return '不限';
  if (tokens < 10000) return String(tokens);
  return `${(tokens / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 万`;
}

function Card({
  title,
  children,
  footer,
}: {
  readonly title: string;
  readonly children: React.ReactNode;
  readonly footer?: React.ReactNode;
}) {
  return (
    <div className="flex min-w-56 flex-col gap-1 rounded-lg border border-black/8 bg-white px-5 py-4">
      <div className="text-sm text-black/55">{title}</div>
      <div className="text-2xl font-semibold">{children}</div>
      {footer && <div className="text-xs text-black/45">{footer}</div>}
    </div>
  );
}

function bar(used: number, total: number | null): React.ReactNode {
  if (total === null || total <= 0) return null;
  const ratio = Math.min(1, used / total);
  return (
    <div className="mt-1 h-1.5 w-full rounded-full bg-black/8">
      <div
        className={`h-1.5 rounded-full ${ratio >= 1 ? 'bg-red-500' : ratio >= 0.85 ? 'bg-amber-500' : 'bg-black/70'}`}
        style={{ width: `${Math.round(ratio * 100)}%` }}
      />
    </div>
  );
}

function SettingDialog({
  workspaceId,
  initial,
  onClose,
}: {
  readonly workspaceId: string;
  readonly initial: { monthlyTotalTokens: number | null; memberDefaultTokens: number | null; mode: QuotaMode };
  readonly onClose: () => void;
}) {
  const client = useQueryClient();
  const [total, setTotal] = useState(initial.monthlyTotalTokens === null ? '' : String(initial.monthlyTotalTokens));
  const [def, setDef] = useState(initial.memberDefaultTokens === null ? '' : String(initial.memberDefaultTokens));
  const [mode, setMode] = useState<QuotaMode>(initial.mode);
  const save = useMutation({
    mutationFn: () =>
      setQuotaSetting(workspaceId, {
        monthlyTotalTokens: total.trim() === '' ? null : Number(total),
        memberDefaultTokens: def.trim() === '' ? null : Number(def),
        mode,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['quota', workspaceId] });
      toast.success('额度已更新');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有保存成功')),
  });
  const bad = (v: string) => v.trim() !== '' && (!/^\d+$/.test(v.trim()) || Number(v) <= 0);
  return (
    <Dialog
      title="空间额度"
      onClose={onClose}
      onSubmit={() => save.mutate()}
      footer={
        <DialogActions okText="保存" loading={save.isPending} onCancel={onClose} />
      }
    >
      <Field label="空间本月总额" help="所有调用（含访客与机器人）加起来的月上限；留空是不限。只有系统管理员能改">
        {(id) => <Input id={id} value={total} inputMode="numeric" placeholder="不限" onChange={(e) => setTotal(e.target.value)} />}
      </Field>
      <Field label="个人默认额度" help="没设个人额度的成员按这个数；留空是不限" required>
        {(id) => <Input id={id} value={def} inputMode="numeric" placeholder="不限" onChange={(e) => setDef(e.target.value)} />}
      </Field>
      <Field label="超额口径" help="拦截：开始新一轮时拒绝；只提醒：照常回答，界面上提醒" required>
        {() => (
          <Select
            block
            value={mode}
            options={Object.entries(MODE_LABELS).map(([value, label]) => ({ value, label }))}
            onChange={(option) => setMode(option.value as QuotaMode)}
          />
        )}
      </Field>
      {bad(total) && <p className="text-sm text-red-600">空间总额要填正整数或留空</p>}
      {bad(def) && <p className="text-sm text-red-600">个人默认要填正整数或留空</p>}
    </Dialog>
  );
}

function GrantDialog({ workspaceId, row, onClose }: { readonly workspaceId: string; readonly row: QuotaMemberRow; readonly onClose: () => void }) {
  const client = useQueryClient();
  const [tokens, setTokens] = useState('');
  const [reason, setReason] = useState('');
  const grant = useMutation({
    mutationFn: () => addQuotaGrant(workspaceId, row.id, Number(tokens), reason.trim()),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['quota', workspaceId] });
      toast.success(`已给 ${row.name} 临时加额，本月有效`);
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有加成')),
  });
  const ok = /^\d+$/.test(tokens.trim()) && Number(tokens) > 0;
  return (
    <Dialog
      title={`给 ${row.name} 临时加额`}
      onClose={onClose}
      onSubmit={() => grant.mutate()}
      footer={<DialogActions okText="加额" loading={grant.isPending} disabled={!ok} onCancel={onClose} />}
    >
      <Field label="加多少 token" help="叠在本月额度上，月底（每月 1 日）自动失效" required>
        {(id) => <Input id={id} value={tokens} inputMode="numeric" onChange={(e) => setTokens(e.target.value)} />}
      </Field>
      <Field label="说明" help="为什么加（选填）">
        {(id) => <Input id={id} value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} />}
      </Field>
    </Dialog>
  );
}

function MemberQuotaDialog({
  workspaceId,
  row,
  onClose,
}: {
  readonly workspaceId: string;
  readonly row: QuotaMemberRow;
  readonly onClose: () => void;
}) {
  const client = useQueryClient();
  const [tokens, setTokens] = useState(row.tokens === null ? '' : String(row.tokens));
  const [mode, setMode] = useState<QuotaMode>(row.mode);
  const save = useMutation({
    mutationFn: () => setMemberQuota(workspaceId, row.id, tokens.trim() === '' ? null : Number(tokens), mode),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['quota', workspaceId] });
      toast.success(tokens.trim() === '' ? '已清除个人额度，回到空间默认' : '已设置个人额度');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有保存成功')),
  });
  const bad = tokens.trim() !== '' && (!/^\d+$/.test(tokens.trim()) || Number(tokens) <= 0);
  return (
    <Dialog
      title={`给 ${row.name} 设额度`}
      onClose={onClose}
      onSubmit={() => save.mutate()}
      footer={<DialogActions okText="保存" loading={save.isPending} disabled={bad} onCancel={onClose} />}
    >
      <Field label="个人额度（token / 月）" help="留空 = 用空间默认。个人同时属于多个部门/组时，组里取最高的那个（M2）">
        {(id) => <Input id={id} value={tokens} inputMode="numeric" placeholder="空间默认" onChange={(e) => setTokens(e.target.value)} />}
      </Field>
      <Field label="超额口径" required>
        {() => (
          <Select
            block
            value={mode}
            options={Object.entries(MODE_LABELS).map(([value, label]) => ({ value, label }))}
            onChange={(option) => setMode(option.value as QuotaMode)}
          />
        )}
      </Field>
      {bad && <p className="text-sm text-red-600">额度要填正整数或留空</p>}
    </Dialog>
  );
}

const COLUMNS: readonly Column<QuotaMemberRow>[] = [
  {
    key: 'member',
    title: '成员',
    render: (row) => (
      <div>
        <div>{row.name}</div>
        <div className="text-xs text-black/45">{row.email}</div>
      </div>
    ),
  },
  {
    key: 'used',
    title: '本月已用',
    align: 'right',
    render: (row) => (
      <div>
        <div>{wan(row.used)}</div>
        {row.effective !== null && bar(row.used, row.effective)}
        {row.effective !== null && (
          <div className="text-xs text-black/45">{Math.round((row.used / row.effective) * 100)}%</div>
        )}
      </div>
    ),
  },
  { key: 'tokens', title: '本月额度', align: 'right', render: (row) => (row.effective === null ? '不限' : `${wan(row.effective)}${row.grants > 0 ? '（含加额）' : ''}`) },
  { key: 'source', title: '额度来自', render: (row) => SOURCE_LABELS[row.source] ?? row.source },
  {
    key: 'status',
    title: '状态',
    render: (row) => {
      if (row.status === null) return <span className="text-black/45">—</span>;
      const color = row.status === 'exhausted' ? 'text-red-600' : row.status === 'near' ? 'text-amber-600' : 'text-black/55';
      return <span className={color}>● {STATUS_LABELS[row.status] ?? row.status}</span>;
    },
  },
  {
    key: 'actions',
    title: '操作',
    render: () => null, // 动作列在行上挂回调（见下面 QuotaTab 的 render 包裹）
  },
];

export function QuotaTab() {
  const workspaceId = useWorkspaceStore((s) => s.id) ?? '';
  const overview = useQuery({
    queryKey: ['quota', 'overview', workspaceId],
    queryFn: () => quotaOverview(workspaceId),
    enabled: workspaceId !== '',
  });
  const setting = useQuery({
    queryKey: ['quota', 'setting', workspaceId],
    queryFn: () => quotaSetting(workspaceId),
    enabled: workspaceId !== '',
  });
  const [editing, setEditing] = useState(false);
  const [member, setMember] = useState<{ row: QuotaMemberRow; kind: 'quota' | 'grant' } | null>(null);
  if (workspaceId === '') return null;
  if (overview.isPending || setting.isPending) return <Skeleton />;
  if (overview.isError || setting.isError) return <Empty title="额度读不出来，稍后再试" />;
  const data = overview.data;
  const s = setting.data;
  const allocated = (data?.members ?? [])
    .filter((row) => row.effective !== null)
    .reduce((sum, row) => sum + (row.effective ?? 0), 0);
  const nextMonth = data?.resetsOn ? `${Number(data.resetsOn.slice(5, 7))} 月 ${Number(data.resetsOn.slice(8, 10))} 日` : '—';
  const withActions = (row: QuotaMemberRow): QuotaMemberRow => ({
    ...row,
  });

  return (
    <div className="flex flex-col gap-5 pt-2">
      <div className="flex flex-wrap gap-4">
        <Card
          title="空间本月总额"
          footer={`${wan(data?.workspaceUsed ?? 0)} 已用 · ${MODE_LABELS[(s?.mode ?? 'block') as QuotaMode]}`}
        >
          {wan(data?.monthlyTotalTokens ?? null)}
          {data?.monthlyTotalTokens !== null && data && bar(data.workspaceUsed, data.monthlyTotalTokens)}
          <div className="pt-1">
            <Button color="secondary" onClick={() => setEditing(true)}>修改</Button>
          </div>
        </Card>
        <Card title="个人默认额度" footer="没设个人额度的成员用它">
          {wan(s?.memberDefaultTokens ?? null)}
          <div className="pt-1">
            <Button color="secondary" onClick={() => setEditing(true)}>修改</Button>
          </div>
        </Card>
        <Card title="已分配合计" footer="各人本月有效额度加起来">
          {wan(allocated === 0 ? null : allocated)}
        </Card>
        <Card title="下次恢复" footer="每月 1 日自动恢复，本月的临时加额同时失效">
          {nextMonth}
        </Card>
      </div>
      <div className="rounded-lg border border-black/8">
        <div className="px-4 pt-3 text-sm text-black/55">
          按成员（临时加额给某个人本月多用一点；只有系统管理员能改空间总额）
        </div>
        {data?.members.length === 0 ? (
          <Empty title="这个空间还没有成员" />
        ) : (
          <Table<QuotaMemberRow>
            rowKey={(row) => row.id}
            rows={(data?.members ?? []).map(withActions)}
            columns={COLUMNS.map((column) =>
              column.key === 'actions'
                ? {
                    ...column,
                    render: (row: QuotaMemberRow) => (
                      <div className="flex gap-2">
                        <Button color="secondary" onClick={() => setMember({ row, kind: 'grant' })}>加额</Button>
                        <Button color="secondary" onClick={() => setMember({ row, kind: 'quota' })}>设额度</Button>
                      </div>
                    ),
                  }
                : column,
            )}
          />
        )}
      </div>
      {editing && s && (
        <SettingDialog
          workspaceId={workspaceId}
          initial={{
            monthlyTotalTokens: s.monthlyTotalTokens,
            memberDefaultTokens: s.memberDefaultTokens,
            mode: s.mode,
          }}
          onClose={() => setEditing(false)}
        />
      )}
      {member?.kind === 'grant' && <GrantDialog workspaceId={workspaceId} row={member.row} onClose={() => setMember(null)} />}
      {member?.kind === 'quota' && <MemberQuotaDialog workspaceId={workspaceId} row={member.row} onClose={() => setMember(null)} />}
    </div>
  );
}
