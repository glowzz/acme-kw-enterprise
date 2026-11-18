/**
 * 个人设置 → 我的用量顶栏（settings.usage.top 插槽）：本人所在空间的额度进度，
 * 剩多少、哪天恢复；85% 起标「接近上限」，用完标「已用完」。没配额度的空间不出现（后端已过滤）。
 */
import { useQuery } from '@tanstack/react-query';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { myQuota } from '../quota';

function wan(tokens: number | null): string {
  if (tokens === null) return '不限';
  if (tokens < 10000) return String(tokens);
  return `${(tokens / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 万`;
}

function Bar({ used, total }: { readonly used: number; readonly total: number }) {
  const ratio = Math.min(1, used / total);
  const color = ratio >= 1 ? 'bg-red-500' : ratio >= 0.85 ? 'bg-amber-500' : 'bg-black/70';
  return (
    <div className="h-1.5 w-full rounded-full bg-black/8">
      <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${Math.round(ratio * 100)}%` }} />
    </div>
  );
}

export function QuotaMePanel() {
  const states = useQuery({ queryKey: ['quota', 'me'], queryFn: myQuota });
  if (states.isPending) return <Skeleton />;
  if (states.isError || states.data.length === 0) return null;
  return (
    <div className="mb-4 rounded-lg border border-black/8 bg-white px-5 py-4">
      <div className="pb-2 text-sm text-black/55">我的额度</div>
      <div className="flex flex-col gap-3">
        {states.data.map((state) => {
          const label = state.status === 'exhausted'
            ? `已用完，${Number(state.resetsOn.slice(5, 7))} 月 ${Number(state.resetsOn.slice(8, 10))} 日恢复`
            : state.status === 'near'
              ? `本月额度已用 ${Math.round((state.used / (state.effective ?? 1)) * 100)}%，还剩 ${wan((state.effective ?? 0) - state.used)} token`
              : `本月已用 ${wan(state.used)} / ${wan(state.effective)}`;
          return (
            <div key={state.workspaceId} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between">
                <span className="text-sm">{state.workspaceName}</span>
                <span className={`text-xs ${state.status === 'exhausted' ? 'text-red-600' : state.status === 'near' ? 'text-amber-600' : 'text-black/45'}`}>
                  {label}
                </span>
              </div>
              {state.effective !== null && <Bar used={state.used} total={state.effective} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
