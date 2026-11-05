/**
 * 个人设置 → 我的记忆（settings.tabs 插槽）：开关、立即整理、按状态分页签的列表、
 * 添加/编辑/删除、导出与清空。只有本人看得到自己的记忆（后端按会话身份判定）。
 * 提取链路在 M2 接上：本页先覆盖手动管理与「待确认」的人工处理入口。
 */
import { useState } from 'react';
import { Button } from '@openai/apps-sdk-ui/components/Button';
import { Plus } from '@openai/apps-sdk-ui/components/Icon';
import { Select } from '@openai/apps-sdk-ui/components/Select';
import { Switch } from '@openai/apps-sdk-ui/components/Switch';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { readableError } from '@knowwork/web/api/http';
import { formatDateTime } from '@knowwork/web/utils/format';
import { useWorkspaceStore } from '@knowwork/web/stores/workspace';
import { Dialog, DialogActions } from '@knowwork/web/ui/Dialog';
import { Empty } from '@knowwork/web/ui/Empty';
import { Field } from '@knowwork/web/ui/Field';
import { Input } from '@openai/apps-sdk-ui/components/Input';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { Table, type Column } from '@knowwork/web/ui/Table';
import { Tabs, type TabItem } from '@knowwork/web/ui/Tabs';
import { toast } from '@knowwork/web/ui/toastStore';
import { Toolbar } from '@knowwork/web/features/admin/AdminPage';
import { RowActions } from '@knowwork/web/features/admin/common';
import {
  MEMORY_KEYS,
  TYPE_LABELS,
  SOURCE_LABELS,
  addMemory,
  clearMemories,
  confirmMemory,
  deleteMemory,
  dismissMemory,
  editMemory,
  exportMemories,
  memoryCounts,
  memorySettings,
  listMemories,
  organizeNow,
  restoreMemory,
  setMemorySettings,
  workspaceMemorySetting,
  type MemoryItem,
  type MemoryStatus,
  type MemoryType,
} from '../memory';

const STATUSES: readonly { readonly key: MemoryStatus; readonly label: string }[] = [
  { key: 'active', label: '生效中' },
  { key: 'pending', label: '待确认' },
  { key: 'superseded', label: '已被更新' },
  { key: 'archived', label: '已归档' },
];

function AddDialog({ onClose }: { readonly onClose: () => void }) {
  const client = useQueryClient();
  const [type, setType] = useState<MemoryType>('preference');
  const [content, setContent] = useState('');
  const add = useMutation({
    mutationFn: () => addMemory(type, content.trim()),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.counts });
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.list('active') });
      toast.success('已添加，之后的对话会带上这条记忆');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有添加成功')),
  });
  return (
    <Dialog
      title="添加记忆"
      onClose={onClose}
      onSubmit={() => add.mutate()}
      footer={
        <DialogActions okText="添加" loading={add.isPending} disabled={content.trim().length === 0} onCancel={onClose} />
      }
    >
      <Field label="类型" required>
        {() => (
          <Select
            block
            value={type}
            options={Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))}
            onChange={(option) => setType(option.value as MemoryType)}
          />
        )}
      </Field>
      <Field label="内容" help="一句第三人称的陈述，例如「回答用表格」" required>
        {(id) => <Input id={id} value={content} maxLength={500} onChange={(e) => setContent(e.target.value)} />}
      </Field>
    </Dialog>
  );
}

function EditDialog({ item, onClose }: { readonly item: MemoryItem; readonly onClose: () => void }) {
  const client = useQueryClient();
  const [content, setContent] = useState(item.content);
  // 在办事项可改期限：日期输入只到天，按当天 00:00 (UTC) 提交给后端
  const [deadline, setDeadline] = useState(
    item.type === 'task' && item.expiresAt ? item.expiresAt.slice(0, 10) : '',
  );
  const edit = useMutation({
    mutationFn: () =>
      editMemory(item.id, {
        content: content.trim(),
        ...(item.type === 'task' && deadline ? { expiresAt: `${deadline}T00:00:00Z` } : {}),
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.counts });
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.list(item.status) });
      toast.success('已修改');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有修改成功')),
  });
  return (
    <Dialog
      title="编辑记忆"
      onClose={onClose}
      onSubmit={() => edit.mutate()}
      footer={
        <DialogActions okText="保存" loading={edit.isPending} disabled={content.trim().length === 0} onCancel={onClose} />
      }
    >
      <Field label="内容" required>
        {(id) => <Input id={id} value={content} maxLength={500} onChange={(e) => setContent(e.target.value)} />}
      </Field>
      {item.type === 'task' ? (
        <Field label="期限" help="到这天还没办完，整理时会归档这条在办事项">
          {(id) => (
            <Input id={id} type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
          )}
        </Field>
      ) : null}
    </Dialog>
  );
}

function ClearDialog({ onClose }: { readonly onClose: () => void }) {
  const client = useQueryClient();
  const [typed, setTyped] = useState('');
  const clear = useMutation({
    mutationFn: () => clearMemories(),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['memory'] });
      toast.success('已清空全部记忆');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有清空成功')),
  });
  return (
    <Dialog
      title="清空全部记忆"
      onClose={onClose}
      onSubmit={() => clear.mutate()}
      footer={
        <DialogActions okText="清空" danger loading={clear.isPending} disabled={typed !== '清空'} onCancel={onClose} />
      }
    >
      <p className="kw-secondary-text">
        各个状态的记忆（含生效中）全部物理删除，不可恢复。输入「清空」两个字确认。
      </p>
      <Field label="确认" required>
        {(id) => <Input id={id} value={typed} maxLength={2} onChange={(e) => setTyped(e.target.value)} />}
      </Field>
    </Dialog>
  );
}

/** 行操作（确认/忽略/恢复/删除）：成功后刷新本页签与角标，失败弹统一错误。 */
function useMemoryAction(fn: (id: string) => Promise<unknown>, success: string, status: MemoryStatus) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.counts });
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.list(status) });
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.list('active') });
      toast.success(success);
    },
    onError: (error) => toast.error(readableError(error, '操作没有成功')),
  });
}

const PAGE_SIZE = 50;

function MemoryList({
  status,
  cap,
  onEdit,
}: {
  readonly status: MemoryStatus;
  readonly cap: number;
  readonly onEdit: (item: MemoryItem) => void;
}) {
  const [page, setPage] = useState(0);
  // 只约束 active 的 200 上限管不住 pending/归档的增长：按页翻，别让第 201 条归档记忆消失
  const list = useQuery({
    queryKey: [...MEMORY_KEYS.list(status), page],
    queryFn: () => listMemories(status, PAGE_SIZE, page * PAGE_SIZE),
  });
  const confirm = useMemoryAction(confirmMemory, '已生效', status);
  const dismiss = useMemoryAction(dismissMemory, '已忽略', status);
  const restore = useMemoryAction(restoreMemory, '已恢复生效', status);
  const remove = useMemoryAction(deleteMemory, '已删除', status);

  const columns: readonly Column<MemoryItem>[] = [
    { key: 'type', title: '类型', width: 110, render: (m) => TYPE_LABELS[m.type] },
    { key: 'content', title: '内容', ellipsis: true, render: (m) => m.content },
    { key: 'source', title: '来源', width: 110, render: (m) => SOURCE_LABELS[m.source] ?? m.source },
    {
      key: 'createdAt',
      title: '记下的时间',
      width: 170,
      render: (m) => (m.createdAt ? formatDateTime(m.createdAt) : '—'),
    },
    {
      key: 'actions',
      title: '操作',
      width: 170,
      render: (m) => (
        <RowActions
          actions={[
            ...(status === 'pending'
              ? [
                  { key: 'confirm', label: '确认', onClick: () => confirm.mutate(m.id) },
                  { key: 'dismiss', label: '忽略', danger: true, onClick: () => dismiss.mutate(m.id) },
                ]
              : []),
            ...(status === 'active' ? [{ key: 'edit', label: '编辑', onClick: () => onEdit(m) }] : []),
            ...(status === 'superseded' || status === 'archived'
              ? [{ key: 'restore', label: '恢复', onClick: () => restore.mutate(m.id) }]
              : []),
            { key: 'delete', label: '删除', danger: true, onClick: () => remove.mutate(m.id) },
          ]}
        />
      ),
    },
  ];

  if (list.isPending) return <Skeleton rows={4} />;
  if (list.isError) return <Empty title={readableError(list.error, '记忆列表没有加载出来')} />;
  if (!list.data || list.data.items.length === 0)
    return (
      <Empty
        title={status === 'active' ? `还没有生效中的记忆（上限 ${cap} 条）` : '这一页暂无记忆'}
        description={status === 'active' ? '对话里说「记住…」或在上面手动添加。' : undefined}
      />
    );
  const { items, total } = list.data;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <div className="kw-field-stack">
      <Table<MemoryItem> rowKey={(m) => m.id} columns={columns} rows={items} minWidth={720} />
      {pages > 1 ? (
        <div className="kw-admin-toolbar">
          <span className="kw-secondary-text">共 {total} 条，第 {page + 1}/{pages} 页</span>
          <div className="kw-admin-toolbar-right">
            <Button color="secondary" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
              上一页
            </Button>
            <Button
              color="secondary"
              disabled={page + 1 >= pages}
              onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
            >
              下一页
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function MemoryTab() {
  const client = useQueryClient();
  const workspaceId = useWorkspaceStore((state) => state.id);
  const settings = useQuery({ queryKey: MEMORY_KEYS.settings, queryFn: memorySettings });
  const counts = useQuery({ queryKey: MEMORY_KEYS.counts, queryFn: memoryCounts });
  const workspaceSetting = useQuery({
    queryKey: MEMORY_KEYS.workspace(workspaceId ?? ''),
    queryFn: () => workspaceMemorySetting(workspaceId ?? ''),
    enabled: workspaceId !== null,
  });
  const [status, setStatus] = useState<MemoryStatus>('active');
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [clearing, setClearing] = useState(false);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setMemorySettings(enabled),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: MEMORY_KEYS.settings });
      toast.success('已保存。关掉后助手不再读取或新增你的记忆，已有的记忆保留');
    },
    onError: (error) => toast.error(readableError(error, '没有保存成功')),
  });
  const organize = useMutation({
    mutationFn: () => organizeNow(workspaceId ?? ''),
    // handler 在 M3 注册：M1 只入队，措辞照实说，不承诺立刻有结果
    onSuccess: () => toast.success('已排入整理队列，整理完成后列表会更新'),
    onError: (error) => toast.error(readableError(error, '没有排上整理任务')),
  });
  const exportAll = async () => {
    try {
      const items = await exportMemories();
      const blob = new Blob([JSON.stringify(items, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'knowwork-memory.json';
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(readableError(error, '导出没有成功'));
    }
  };

  const cap = counts.data?.cap ?? 200;
  const active = counts.data?.active ?? 0;
  const tabs: readonly TabItem[] = STATUSES.map(({ key, label }) => ({
    key,
    label: key === 'active' ? `${label} (${active}/${cap})` : `${label} (${counts.data?.[key] ?? 0})`,
    children: <MemoryList status={key} cap={cap} onEdit={setEditing} />,
  }));

  return (
    <div className="kw-field-stack">
      <p className="kw-secondary-text">
        这里是助手会话记住的关于你的内容，关于你的情况和偏好会在每次对话里带上。只有你自己看得到，可以随时修改、删除。
      </p>
      <Switch
        label="为我启用个人记忆"
        checked={settings.data?.enabled ?? true}
        onCheckedChange={(enabled) => toggle.mutate(enabled)}
      />
      {workspaceSetting.data?.disabled ? (
        <p className="kw-secondary-text">本空间已停用记忆：在这个空间里助手不读取、不新增记忆；其他空间与已有数据不受影响。</p>
      ) : null}
      <Toolbar
        left={
          <span className="kw-secondary-text">合并说法重复的记忆，很久没提的降低优先级，到期的归档；每天也会自动整理一次。</span>
        }
        right={
          <>
            <Button color="secondary" onClick={() => organize.mutate()} disabled={organize.isPending}>
              立即整理
            </Button>
            <Button color="secondary" onClick={() => void exportAll()}>导出</Button>
            <Button color="danger" onClick={() => setClearing(true)}>
              清空
            </Button>
            <Button color="primary" onClick={() => setAdding(true)}>
              <Plus />
              添加记忆
            </Button>
          </>
        }
      />
      <Tabs items={tabs} value={status} onChange={(key) => setStatus(key as MemoryStatus)} />
      {adding ? <AddDialog onClose={() => setAdding(false)} /> : null}
      {editing ? <EditDialog item={editing} onClose={() => setEditing(null)} /> : null}
      {clearing ? <ClearDialog onClose={() => setClearing(false)} /> : null}
    </div>
  );
}
