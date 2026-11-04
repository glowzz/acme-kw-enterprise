/**
 * 扩展联调用的 vite 配置：在 KnowWork-stable 上游配置之上只改两处——
 * 1. server.host 放开到所有网卡（从别的设备访问开发服务器）；
 * 2. /api 代理指到扩展联调栈的 api（8100；上游写死 8000，那台机器上 8000 被别的部署占用）。
 * 上游配置仍是唯一事实来源（别名、插件、fs.allow、KNOWWORK_WEB_EXTENSIONS 逻辑全部继承），升级不受影响。
 * 由 scripts/dev_stack_ext.py 经 `--config` 注入，不修改上游任何文件。
 */
import { defineConfig, mergeConfig, type UserConfig } from 'vite';
// @ts-expect-error 跨仓库引入上游配置（vite 用 esbuild 打包配置文件，支持 .ts 导入）
import upstream from '../../KnowWork-stable/web/vite.config.ts';

type ConfigFn = (env: { command: 'serve' | 'build'; mode: string }) => UserConfig | Promise<UserConfig>;

export default defineConfig(async ({ mode }: { mode: string }) => {
  const loaded = (upstream as unknown as ConfigFn)({ command: 'serve', mode });
  const base = await Promise.resolve(loaded);
  return mergeConfig(base, {
    server: {
      host: true, // 监听全部网卡：局域网设备可访问
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8100', // 扩展联调栈的 api 公开端口（dev_stack_ext.py）
          changeOrigin: true,
        },
      },
    },
  } satisfies UserConfig);
});
