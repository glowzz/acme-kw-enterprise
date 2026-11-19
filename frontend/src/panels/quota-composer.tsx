/**
 * 输入框上方（chat.composer.top 插槽）：本人本月额度用完且口径是拦截时，提前把输入框拦住
 * （block(reason)：发送按钮不可点、按回车提示原因——后端反正会 429，这里只是提前说清楚）。
 * 只提醒口径不拦；没配额度的空间什么都不画。一轮结束后重取（used 变了）。
 */
import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { currentWorkspaceId } from '@knowwork/web/stores/workspace';
import { quotaStatus } from '../quota';

function wan(tokens: number | null): string {
  if (tokens === null) return '不限';
  if (tokens < 10000) return String(tokens);
  return `${(tokens / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 万`;
}

export function QuotaComposerTop({
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
    block(null);
  }, [states.data, states.isFetching, block]);

  const state = states.data ?? null;
  const status = state?.status ?? null;
  const reminder = state === null || status === null || status === 'ok'
    ? null
    : status === 'exhausted'
      ? (state.mode === 'warn'
        ? `本月额度已用完（只提醒口径，照常回答）；${Number(state.resetsOn.slice(5, 7))} 月 1 日恢复`
        : null) // 拦截口径由 block(reason) 提示，不另画一条
      : `本月额度已用 ${Math.round((state.used / (state.effective ?? 1)) * 100)}%，还剩 ${wan((state.effective ?? 0) - state.used)} token`;

  if (reminder === null) return null;
  return (
    <div className={`rounded-md px-3 py-2 text-xs ${status === 'exhausted' ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'}`}>
      {reminder}
    </div>
  );
}
