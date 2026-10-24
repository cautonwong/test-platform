"""
安装形态的插件只注册一次(票 09 双加载坑)。

装成包以后 pytest 按 pyproject 的 pytest11 entry point 自己加载插件。这里用一个**真的
`*.dist-info`**(放在项目目录里、在子进程的 PYTHONPATH 上)模拟"装上了":对 importlib.metadata
与 pytest 来说,这与 pip 装出来的包是一回事,不需要为此建 venv。

判据是 entry point 能不能 resolve 到插件模块 —— 能 resolve 时执行器不再传 `-p`(见
`_plugin_arguments`);而即便有人照旧传 `-p`,stderr 里也不许出现 `already registered`。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from testplatform.config import paths
from testplatform.execution.task import _plugin_arguments

REPO = Path(__file__).resolve().parents[1]


def _fake_installed_plugin(base: Path, *, entry_point: str = paths.PLUGIN_ENTRY_POINT) -> Path:
    """一个只有元数据的"安装":dist-info 里声明 pytest11 entry point。"""
    site = base / "site"
    info = site / "testplatform-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: testplatform\nVersion: 0.1.0\n", encoding="utf-8")
    (info / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(
        f"[pytest11]\n{entry_point} = {paths.PLUGIN_MODULE}\n", encoding="utf-8"
    )
    return site


def test_an_installed_package_registers_the_plugin_exactly_once(tmp_path, monkeypatch):
    """装成包以后:`-p testplatform.plugin --version` 成功,stderr 里没有 already registered。"""
    site = _fake_installed_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(site))

    # 装成包了 → 不再自己传 -p(父子进程同一个环境,判据在父进程这边也成立)
    assert paths.plugin_from_entry_point() is True
    assert _plugin_arguments() == []

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", paths.PLUGIN_MODULE, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(site)]),
        },
        timeout=120,
    )

    output = (completed.stdout + completed.stderr).lower()
    assert "already registered" not in output, output
    assert completed.returncode == 0, output
