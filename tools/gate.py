"""
一条命令算出门禁结论。

    python tools/gate.py reports/            # 取 reports/*/summary.json
    python tools/gate.py a/summary.json b/summary.json

打印一行理由(冒烟任务另起一行),退出码见 `python tools/gate.py --help`。
判决逻辑在 src/testplatform/execution/gate.py:这个文件只是入口。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from testplatform.execution.gate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
