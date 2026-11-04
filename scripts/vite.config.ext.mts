/**
 * 扩展联调用的 vite 配置：在 KnowWork-stable 上游配置之上只改三处——
 * 1. server.host 放开到所有网卡（从别的设备访问开发服务器）；
 * 2. /api 代理指到扩展联调栈的 api（8100；上游写死 8000）；
 * 3. resolve.alias 与 server.fs.allow 里的扩展路径改用绝对路径——上游写的是相对路径
 *    （'../../acme-kw-enterprise/...'），经 --config 外挂后解析基准不再是 web 目录，会解析失败。
 *    两个别名覆盖后，registry.ts 的 @knowwork/extensions 与面板里的 @knowwork/web 都能稳定命中。
 * 路径全部从本文件位置推导（import.meta.url），不写死机器路径。
 * 由 scripts/dev_stack_ext.py 经 `--config` 注入，不修改上游任何文件。
 */
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, type UserConfig } from 'vite';
// @ts-expect-error 跨仓库引入上游配置（vite 用 esbuild 打包配置文件，支持 .ts 导入）
import upstream from '../../KnowWork-stable/web/vite.config.ts';

const HERE = fileURLToPath(new URL('.', import.meta.url)); // .../acme-kw-enterprise/scripts/
// 注意保留结尾斜杠：fileURLToPath 不补斜杠，少了它拼路径会变成 acme-kw-enterprisefrontend
const EXT_ROOT = fileURLToPath(new URL('../', import.meta.url)); // .../acme-kw-enterprise/
const KW_STABLE = fileURLToPath(new URL('../../KnowWork-stable/', import.meta.url)); // .../KnowWork-stable/

type ConfigFn = (env: { command: 'serve' | 'build'; mode: string }) => UserConfig | Promise<UserConfig>;

export default defineConfig(async ({ mode }: { mode: string }) => {
  const loaded = (upstream as unknown as ConfigFn)({ command: 'serve', mode });
  const base = await Promise.resolve(loaded);
  return mergeConfig(base, {
    resolve: {
      alias: {
        '@knowwork/extensions': `${EXT_ROOT}frontend/src/install.tsx`,
        '@knowwork/web': `${KW_STABLE}web/src`,
      },
    },
    server: {
      host: true, // 监听全部网卡：局域网设备可访问
      fs: {
        allow: ['.', EXT_ROOT, `${KW_STABLE}web`],
      },
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8100', // 扩展联调栈的 api 公开端口（dev_stack_ext.py）
          changeOrigin: true,
        },
      },
    },
  } satisfies UserConfig);
});
