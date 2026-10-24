"""
一轮的失败归因入口 —— 工程师每天最耗时的那件事:这一轮到底哪些设备不合格、
哪些是台架问题(测试无效)、断在哪一步。

    python tools/attribute_round.py                              # 取 evidence/ 下最新的一轮
    python tools/attribute_round.py evidence/20240101T093000      # 指定一轮

输出两份,同一份归因:
    终端分节输出(人读)  四类分开列,每一条都带证据出处(文件:行号 / JSON 字段)
    attribution.json(机器读)  写回同一个证据目录,字段与 round-status.json / scenario-*.json 同一风格

四类不许并:设备不合格 / 测试无效 / 平台错误 / 没有有效结论。证据文件本身坏掉另算一类。
"没跑"不是"通过":被 --select 或适用矩阵筛掉、被能力跳过、中断的用例各有各的名字。

退出码:clean 为 0,其余(有失败 / 无效 / 平台错误 / 证据损坏 / 这一轮没跑到用例)为 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from testplatform.reporting.attribution import attribute, render, write  # noqa: E402
from testplatform.reporting.evidence import EVIDENCE_ROOT  # noqa: E402


def _latest_round(root: Path) -> Path:
    rounds = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if not rounds:
        raise SystemExit(f"{root} 下没有任何一轮证据:先跑一轮 pytest,或直接指定证据目录")
    return rounds[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="读出这一轮哪些设备不合格、哪些测试无效、哪些是平台 bug、哪些没有有效结论"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help=f"证据目录(evidence/<会话>);省略时取 {EVIDENCE_ROOT}/ 下最新的一轮",
    )
    args = parser.parse_args(argv)

    directory = Path(args.directory) if args.directory else _latest_round(Path(EVIDENCE_ROOT))
    attribution = attribute(directory)
    for line in render(attribution):
        print(line)
    print(f"机器可读: {write(directory, attribution)}")
    return 0 if attribution.verdict == "clean" else 1


if __name__ == "__main__":
    sys.exit(main())
