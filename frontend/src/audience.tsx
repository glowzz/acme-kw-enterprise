/**
 * 「发给哪些空间」的展示与编辑：企业技能、企业 MCP 共用（audience 存法一样：["*"] 或空间 id 列表）。
 * 编辑框要的空间清单来自空间管理接口（只有系统管理员能调，面板本来就只给系统管理员）。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox';
import { Dialog, DialogActions } from '@knowwork/web/ui/Dialog';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { toast } from '@knowwork/web/ui/toastStore';
import { readableError } from '@knowwork/web/api/http';
import { listWorkspaces, WORKSPACES_KEY, type WorkspaceSummary } from './api';

/** 空间清单：发放目标与收编来源都要用。 */
export function useWorkspaces() {
  return useQuery({ queryKey: WORKSPACES_KEY, queryFn: listWorkspaces });
}

export function audienceText(audience: readonly string[], workspaces?: readonly WorkspaceSummary[]): string {
  if (audience.includes('*')) return '全部空间';
  if (audience.length === 0) return '未发放';
  if (!workspaces) return `${audience.length} 个空间`;
  const names = audience.map((id) => workspaces.find((workspace) => workspace.id === id)?.name ?? id);
  return names.length <= 3 ? names.join('、') : `${names.slice(0, 3).join('、')} 等 ${names.length} 个空间`;
}

interface EditorProps {
  readonly title: string;
  readonly audience: readonly string[];
  readonly pending: boolean;
  readonly onSave: (workspaces: string[]) => Promise<boolean>;
  readonly onClose: () => void;
}

export function AudienceEditor({ title, audience, pending, onSave, onClose }: EditorProps) {
  const workspaces = useWorkspaces();
  const all = audience.includes('*');
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set(all ? [] : audience));
  const [allOn, setAllOn] = useState(all);
  // 发放记录里可能残留已关闭空间的 id：列表里没有对应的行，勾不掉；保存时把它们移除（服务层本来也不收）。
  // 「还没加载出来」不等于「已关闭」：列表没就绪时不算残留、也不允许保存（非全部模式下会把发放清空）。
  // '*' 不是空间 id（全部模式的标记），不算残留；有效勾选数按「picked ∩ 已知空间」算，全残留时等于 0，保存被拦。
  const listReady = workspaces.isSuccess;
  const known = new Set((workspaces.data ?? []).map((workspace) => workspace.id));
  const staleCount = listReady && !allOn
    ? [...audience].filter((id) => id !== '*' && !known.has(id)).length
    : 0;
  const validPicked = [...picked].filter((id) => known.has(id));
  // 清空发放（[]）是合法状态但基本是误操作：非全部模式下有效勾选为 0 就不允许保存
  const canSave = allOn ? true : listReady && validPicked.length > 0;
  const submit = async () => {
    if (!allOn && (!listReady || validPicked.length === 0)) return;
    if (await onSave(allOn ? ['*'] : validPicked)) onClose();
  };
  // 保存被禁或记录有残留时的解释文案，按成因区分：
  // - 「全部残留」只在本就没勾任何在线空间且原始记录不含 '*' 时成立（刚从「全部」切回、列表为空都给中性提示）；
  // - 用户自己取消勾选导致的 validPicked=0 也是中性提示，不误归因到已关闭空间。
  const note = (() => {
    if (!listReady || allOn) return null;
    const nonStarIds = [...audience].filter((id) => id !== '*');
    const allStale = !all && nonStarIds.length > 0 && staleCount === nonStarIds.length;
    if ((workspaces.data ?? []).length === 0) return '当前没有可发放的空间。';
    if (validPicked.length === 0) {
      return allStale
        ? '发放记录全部指向已关闭的空间：请先重新勾选要发放的空间。'
        : '请至少勾选一个要发放的空间。';
    }
    if (staleCount > 0) {
      return `发放记录里有 ${staleCount} 个指向已关闭的空间，保存时会移除。`;
    }
    return null;
  })();
  return (
    <Dialog
      title={title}
      onClose={onClose}
      size="md"
      footer={
        <DialogActions
          okText="保存"
          onOk={() => void submit()}
          loading={pending}
          disabled={!canSave}
          onCancel={onClose}
        />
      }
    >
      <p className="kw-secondary-text">发到空间后默认全体成员可用；空间管理员可以在本空间停用或收窄给谁用。</p>
      {note ? <p className="kw-secondary-text">{note}</p> : null}
      <div className="kw-check-list">
        <Checkbox
          label="全部空间（以后新建的也发放）"
          checked={allOn}
          onCheckedChange={(on) => setAllOn(on === true)}
        />
        {!allOn ? (
          workspaces.isPending ? (
            <Skeleton rows={3} />
          ) : workspaces.isError ? (
            <p className="kw-secondary-text">{readableError(workspaces.error, '空间列表没有加载出来')}</p>
          ) : (
            (workspaces.data ?? []).map((workspace) => (
              <Checkbox
                key={workspace.id}
                label={workspace.name}
                checked={picked.has(workspace.id)}
                onCheckedChange={(on) =>
                  setPicked((prev) => {
                    const next = new Set(prev);
                    if (on === true) next.add(workspace.id);
                    else next.delete(workspace.id);
                    return next;
                  })
                }
              />
            ))
          )
        ) : null}
      </div>
    </Dialog>
  );
}

/** 改发放的通用动作：面板把各自的 set 函数与查询键传进来；失败给 toast，成功返回 true（供对话框收尾）。 */
export function useSetAudience(
  set: (id: string, workspaces: string[]) => Promise<unknown>,
  invalidate: readonly (readonly unknown[])[],
) {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ id, workspaces }: { id: string; workspaces: string[] }) => set(id, workspaces),
    onSuccess: async (_data, variables) => {
      await Promise.all(invalidate.map((queryKey) => client.invalidateQueries({ queryKey })));
      toast.success(variables.workspaces.includes('*') ? '已发给全部空间' : '已保存发给哪些空间');
    },
  });
  const run = async (input: { id: string; workspaces: string[] }): Promise<boolean> => {
    try {
      await mutation.mutateAsync(input);
      return true;
    } catch (error) {
      toast.error(readableError(error, '没有保存成功'));
      return false;
    }
  };
  return { run, pending: mutation.isPending };
}
