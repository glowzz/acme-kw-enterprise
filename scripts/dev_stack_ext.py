"""扩展联调的整套启动：与上游 dev_stack.py 相同，只把端口错开（8000 被本机另一套部署占用）。

不改上游文件：导入后覆盖模块常量再进 main。用法（在 KnowWork-stable/server 里）：
    uv run python ../../acme-kw-enterprise/scripts/dev_stack_ext.py
端口：api 公开 8100 / 内部 8101、宿主 7310、前端 5180。
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "KnowWork-stable" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dev_stack  # noqa: E402

dev_stack.PUBLIC_PORT, dev_stack.INTERNAL_PORT = 8100, 8101
dev_stack.HOST_PORT, dev_stack.WEB_PORT = 7310, 5180

if __name__ == "__main__":
    raise SystemExit(dev_stack.main())
