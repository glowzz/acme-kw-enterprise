/**
 * 输入框上方（chat.composer.top 插槽）：本人本月额度用完且口径是拦截时，提前把输入框拦住
 * （block(reason)：发送按钮不可点、按回车提示原因——后端反正会 429，这里只是提前说清楚）。
 * 只提醒口径不拦；没配额度的空间什么都不画。一轮结束后重取（used 变了）。
 */
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { currentWorkspaceId } from '@knowwork/web/stores/workspace';
import { quotaStatus } from '../quota';

export function QuotaComposerTop({
  conversationId,
  running,
  block,
}: {
  readonly conversationId?: string;
  readonly answerMode: string;
  readonly running: boolean;
  readonly block: (reason: string | null) => void;
}) {
  const workspaceId = currentWorkspaceId();
  const states = useQuery({
    queryKey: ['quota', 'status', workspaceId, running],
    queryFn: () => quotaStatus(workspaceId),
    enabled: Boolean(workspaceId),
    // 一轮结束（running 由 true 翻回 false）或切对话时重取；额度是月级的，平时不轮询
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    const state = states.data;
    if (!state) {
      block(null);
      return;
    }
    if (state.mode === 'block' && state.status === 'exhausted') {
      const left = state.resetsOn
        ? `${Number(state.resetsOn.slice(5, 7))} 月 ${Number(state.resetsOn.slice(8, 10))} 日恢复`
        : '每月 1 日恢复';
      block(`本月额度已用完（${state.workspaceName}），${left}；可找空间管理员临时加额`);
      return;
    }
    if (state.mode === 'warn' && state.status === 'exhausted') {
      // 只提醒：不拦输入框，提醒由「我的用量」与回答时的说明承担（M2 挂 block 只读文案）
      block(null);
      return;
    }
    block(null);
  }, [states.data, states.isFetching, block, conversationId]);

  return null; // 这一项只负责拦与放；可见的提醒条在「我的用量」与 M2 的 composer 提醒
}
