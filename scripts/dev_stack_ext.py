"""扩展联调的整套启动：与上游 dev_stack.py 相同，只改三处——

1. 端口错开（8000 被本机另一套部署占用）：api 公开 8100 / 内部 8101、宿主 7310、前端 5180；
2. vite 监听全部网卡（上游写死 --host 127.0.0.1，别的设备访问不到）；
3. vite /api 代理改指 8100（上游 vite.config.ts 写死 8000；经 --config 注入 vite.config.ext.mts，
   上游配置仍是唯一事实来源）。

不改上游文件：导入后覆盖模块常量、包装 start() 再进 main。用法（在 KnowWork-stable/server 里）：
    uv run python ../../acme-kw-enterprise/scripts/dev_stack_ext.py
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "KnowWork-stable" / "scripts"
EXT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import dev_stack  # noqa: E402

dev_stack.PUBLIC_PORT, dev_stack.INTERNAL_PORT = 8100, 8101
dev_stack.HOST_PORT, dev_stack.WEB_PORT = 7310, 5180

_orig_start = dev_stack.start


def _patched_start(name: str, args: list, **kwargs):
    if name == "web":
        host_index = args.index("--host") + 1
        args[host_index] = "0.0.0.0"
        args += ["--config", str(EXT / "scripts" / "vite.config.ext.mts")]
    return _orig_start(name, args, **kwargs)


dev_stack.start = _patched_start

if __name__ == "__main__":
    raise SystemExit(dev_stack.main())
