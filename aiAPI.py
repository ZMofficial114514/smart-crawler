"""
aiAPI.py —— 兼容入口(已迁移到 scripts/ai_configure.py)。

保留这个文件是为了不让已有习惯/脚本失效::

    python aiAPI.py --show          # 仍然可用
    python aiAPI.py --test
    python aiAPI.py --preset deepseek

新的推荐路径(项目结构与职责更清晰)::

    python scripts/ai_configure.py --show

图形化替代方案: 启动 Web 控制台后进入「系统配置 → 自检与维护」,可一键测试
AI 连通性并看到实际延迟,无需命令行。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "scripts" / "ai_configure.py"


def main() -> int:
    if not TARGET.exists():  # pragma: no cover - 只在文件被删除时触发
        print(f"找不到 {TARGET}", file=sys.stderr)
        return 2
    # run_path 会让目标脚本以自己的 __name__ == "__main__" 运行
    runpy.run_path(str(TARGET), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
