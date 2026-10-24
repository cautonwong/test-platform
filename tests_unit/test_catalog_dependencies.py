"""
票 13 的导入守卫:资产目录不许把报告层/执行层/组合根一起拖进来。

为什么值得一条子进程用例:在同一个 pytest 进程里 import 什么都"已经发生过"了,断言不出
"目录自己的依赖面"。所以这里起一个**干净解释器**,只 `import testplatform.catalog`,
再看 `sys.modules` 里到底进了什么 —— 这是唯一能证明"依赖面"的办法。

反面(必须红):把 `catalog.py` 里的 `registrations` 改回 `composition.root`,
下面第一条立刻红 —— 组合根会为了一个常量把 `reporting.evidence` 拖进来。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

PROBE = """
import sys
import testplatform.catalog
from testplatform.drivers.registry import registered_types

print("|".join(sorted(name for name in sys.modules if name.startswith("testplatform."))))
print("|".join(registered_types()))
"""


def _import_probe() -> tuple[list[str], list[str]]:
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]).rstrip(
                os.pathsep
            ),
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    modules, device_types = completed.stdout.splitlines()[:2]
    return [name for name in modules.split("|") if name], [
        name for name in device_types.split("|") if name
    ]


def test_importing_the_catalog_does_not_pull_in_the_report_or_execution_layer():
    """目录的依赖面:注册清单 + 清单/条款解析。报告层、执行层、组合根一律不进。"""
    modules, _device_types = _import_probe()

    offenders = [
        name
        for name in modules
        if name.startswith(("testplatform.reporting", "testplatform.execution"))
        or name == "testplatform.composition.root"
    ]
    assert offenders == [], f"目录把整条链路拖进来了: {offenders}"


def test_importing_the_catalog_still_registers_the_implementations():
    """切断连带依赖不等于把注册也切掉:只 import 目录之后,注册表必须是齐的。"""
    _modules, device_types = _import_probe()

    assert device_types == ["battery", "grid_simulator", "inverter", "meter", "terminal"]
