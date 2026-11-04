/**
 * 管理后台「空间管理」（/admin/workspaces）：列出全部空间、新建（指定所有者）、改名、关闭（软删除）。
 * 数据与操作走扩展后端的 /api/system/workspaces；默认空间不能关（后端也会拦，界面直接不给入口）。
 * 成员加入空间用各空间自己的管理后台（开源），这里只管空间本身。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@openai/apps-sdk-ui/components/Button';
import { Plus } from '@openai/apps-sdk-ui/components/Icon';
import { Input } from '@openai/apps-sdk-ui/components/Input';
import { readableError } from '@knowwork/web/api/http';
import { formatDateTime } from '@knowwork/web/utils/format';
import { useIsSystemAdmin } from '@knowwork/web/features/brand/queries';
import { UserPicker } from '@knowwork/web/features/admin/members/UserPicker';
import { Dialog, DialogActions } from '@knowwork/web/ui/Dialog';
import { Empty } from '@knowwork/web/ui/Empty';
import { Field } from '@knowwork/web/ui/Field';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { toast } from '@knowwork/web/ui/toastStore';
import { AdminPage, Toolbar } from '@knowwork/web/features/admin/AdminPage';
import { RowActions } from '@knowwork/web/features/admin/common';
import { Table, type Column } from '@knowwork/web/ui/Table';
import {
  closeWorkspace,
  createWorkspace,
  listWorkspaces,
  renameWorkspace,
  WORKSPACES_KEY,
  type WorkspaceSummary,
} from '../api';

function CreateDialog({ onClose }: { readonly onClose: () => void }) {
  const client = useQueryClient();
  const [name, setName] = useState('');
  const [ownerId, setOwnerId] = useState<string | undefined>(undefined);
  const create = useMutation({
    mutationFn: () => createWorkspace(name.trim(), ownerId ?? ''),
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: WORKSPACES_KEY });
      toast.success(`已创建空间「${created.name}」，到该空间的成员页邀请成员`);
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有创建成功')),
  });
  const canSubmit = name.trim().length > 0 && ownerId !== undefined;
  return (
    <Dialog
      title="新建空间"
      onClose={onClose}
      onSubmit={() => create.mutate()}
      footer={<DialogActions okText="创建" loading={create.isPending} disabled={!canSubmit} onCancel={onClose} />}
    >
      <Field label="空间名称" required>
        {(id) => (
          <Input id={id} value={name} maxLength={64} onChange={(e) => setName(e.target.value)} placeholder="例如：销售部" />
        )}
      </Field>
      <Field label="所有者" help="输入姓名或邮箱搜索账号，选中者成为空间的第一个所有者" required>
        {(id) => (
          <UserPicker
            id={id}
            onChange={setOwnerId}
            exclude={new Set()}
            invalid={!ownerId}
          />
        )}
      </Field>
    </Dialog>
  );
}

function RenameDialog({ workspace, onClose }: { readonly workspace: WorkspaceSummary; readonly onClose: () => void }) {
  const client = useQueryClient();
  const [name, setName] = useState(workspace.name);
  const rename = useMutation({
    mutationFn: () => renameWorkspace(workspace.id, name.trim()),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: WORKSPACES_KEY });
      toast.success('已改名');
      onClose();
    },
    onError: (error) => toast.error(readableError(error, '没有改名成功')),
  });
  return (
    <Dialog
      title={`改「${workspace.name}」的名称`}
      onClose={onClose}
      onSubmit={() => rename.mutate()}
      footer={
        <DialogActions okText="保存" loading={rename.isPending} disabled={name.trim().length === 0} onCancel={onClose} />
      }
    >
      <Field label="空间名称" required>
        {(id) => <Input id={id} value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />}
      </Field>
    </Dialog>
  );
}

export function WorkspacesPage() {
  const isAdmin = useIsSystemAdmin().data === true;
  const workspaces = useQuery({ queryKey: WORKSPACES_KEY, queryFn: listWorkspaces, enabled: isAdmin });
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<WorkspaceSummary | null>(null);
  const close = useMutation({
    mutationFn: (workspace: WorkspaceSummary) => closeWorkspace(workspace.id),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: WORKSPACES_KEY });
      toast.success('已关闭空间。数据保留，成员已全部失去访问');
    },
    onError: (error) => toast.error(readableError(error, '没有关闭成功')),
  });

  const columns: readonly Column<WorkspaceSummary>[] = [
      { key: 'name', title: '名称', width: 220, render: (w) => w.name },
      { key: 'createdAt', title: '创建时间', width: 180, render: (w) => (w.createdAt ? formatDateTime(w.createdAt) : '-') },
      {
        key: 'owners',
        title: '所有者',
        ellipsis: true,
        render: (w) => w.owners.join('、') || '-',
      },
      {
        key: 'actions',
        title: '操作',
        width: 160,
        render: (w) => (
          <RowActions
            actions={[
              { key: 'rename', label: '改名', onClick: () => setRenaming(w) },
              ...(w.isDefault
                ? []
                : [
                    {
                      key: 'close',
                      label: '关闭',
                      danger: true,
                      confirm: `关闭空间「${w.name}」？所有成员（包括系统管理员）立即失去访问，数据保留但界面进不去。此操作要恢复需另行处理，确定继续吗？`,
                      onClick: () => close.mutate(w),
                    },
                  ]),
            ]}
          />
        ),
      },
  ];

  if (!isAdmin) {
    return (
      <AdminPage title="空间管理" description="只有系统管理员可以管理空间。">
        <Empty title="没有权限" description="这个页面只对系统管理员开放。" />
      </AdminPage>
    );
  }
  return (
    <AdminPage
      title="空间管理"
      description="每个空间有自己的成员、知识库、技能与工具，数据按空间隔离。默认空间存放平台级配置，不能关闭。"
    >
      <Toolbar
        right={
          <Button color="primary" onClick={() => setCreating(true)}>
            <Plus />
            新建空间
          </Button>
        }
      />
      {workspaces.isPending ? (
        <Skeleton rows={4} />
      ) : workspaces.isError ? (
        <Empty title={readableError(workspaces.error, '空间列表没有加载出来')} />
      ) : (
        <Table<WorkspaceSummary>
          rowKey={(workspace) => workspace.id}
          columns={columns}
          rows={workspaces.data}
          minWidth={760}
          loading={workspaces.isFetching}
        />
      )}
      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {renaming ? <RenameDialog workspace={renaming} onClose={() => setRenaming(null)} /> : null}
    </AdminPage>
  );
}
