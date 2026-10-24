"""源码布局里的任务入口:`python tools/run_task.py <任务文件>`。

装成包以后用 `testplatform-run-task <任务文件>`;两处是**同一个 main**(参数、退出码只有一份),
这里只是把本仓的 `src/` 放进 `sys.path` 的壳。任务文件的注释与 README 都还指这条路。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from testplatform.execution.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
