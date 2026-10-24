"""仓库根的 conftest:源码布局里插件由这里加载,装成包以后由 pyproject 的 pytest11 entry point 装上。"""
import sys
from pathlib import Path

# 根 conftest 比 pyproject 的 pythonpath 早被 import:先把本仓的 src 排到最前,
# 免得先命中别的 src(比如指向另一个 worktree 的可编辑安装),整轮就用错了树的代码。
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from testplatform.config.paths import (  # noqa: E402
    PLUGIN_MODULE,
    plugin_from_entry_point,
    sys_path,
)

# 代码路径与项目目录由 paths.sys_path 一条规则给(资源回落用的是同一个根):
# 引导那一行躲不掉(import 之前只能按本文件位置放 src),其余项照它补齐。
sys.path[:0] = [
    entry
    for entry in sys_path(root=Path(__file__).resolve().parent)
    if entry not in sys.path
]

# 同一个模块注册两次(entry point 一次 + 这里一次)= 同一个模块两个名字,pytest 直接
# `Plugin already registered` 中止整轮。判据是 entry point 能不能 resolve,不是"包装没装"。
pytest_plugins = [] if plugin_from_entry_point() else [PLUGIN_MODULE]
