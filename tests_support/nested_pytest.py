"""
子进程里跑 pytest 的命令前缀。

嵌套 pytest 不继承本仓库的 pyproject.toml:`addopts = "-p no:randomly"` 只对 cwd 在本仓库的那一轮生效,
而子进程的 cwd 往往是 tmp_path。pytest-randomly 装了就自带 autouse,在子进程里是开着的:
收集顺序被打乱,`--maxfail=1` 于是要跑几条才停 —— 父进程的 `-p no:randomly` 保护不了子进程。
所以每个嵌套调用都显式带上它,让嵌套跑与主套件同构。
"""
from __future__ import annotations

import sys


def pytest_command(*args: str) -> list[str]:
    """子进程 pytest 命令行:先挡掉 pytest-randomly,再拼调用方自己的参数。"""
    return [sys.executable, "-m", "pytest", "-p", "no:randomly", *args]
