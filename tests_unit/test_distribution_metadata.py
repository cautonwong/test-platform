"""
分发元数据(票 09):发行版名一处常量、插件两处入口都指向同一个顶层模块。

PyPI 的发行版名不能带 `@` / `/`,npm 风格的 scope 只在 `testplatform/__init__.py` 里出现一次,
这里拿 pyproject 与它对齐 —— 改 scope 只改那一行,漂了这里红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from testplatform import DISTRIBUTION_NAME, PACKAGE_NAME, PACKAGE_SCOPE
from testplatform.config.paths import PLUGIN_ENTRY_POINT, PLUGIN_MODULE

tomllib = pytest.importorskip("tomllib", reason="tomllib 是 3.11+ 的标准库")

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_distribution_name_is_derived_from_the_scope_in_one_place():
    """`@cautonwong/testplatform` 的 scope 只在 __init__.py 里写一次,发行版名由它派生。"""
    assert PACKAGE_SCOPE == "@cautonwong"
    assert PACKAGE_NAME == "testplatform"
    assert DISTRIBUTION_NAME == "cautonwong-testplatform"
    assert _pyproject()["project"]["name"] == DISTRIBUTION_NAME, (
        "pyproject 的 name 与 PACKAGE_SCOPE 派生出来的发行版名不一致:"
        "改 scope 要同时改这两处(这个断言就是那条提醒)"
    )


def test_the_plugin_ships_inside_the_package():
    """`-p testplatform.plugin` 装完还得能用:插件是包内的子模块,不是仓库根的文件。"""
    relative = Path(*PLUGIN_MODULE.split(".")).with_suffix(".py")
    assert (REPO / "src" / relative).is_file(), f"没有 {relative}:`-p {PLUGIN_MODULE}` 装完起不来"


def test_the_entry_point_points_at_the_same_module():
    """pytest11 entry point 与 `-p` 必须是同一个模块,否则两处加载就是两个插件。"""
    entry_points = _pyproject()["project"]["entry-points"]["pytest11"]
    assert entry_points[PLUGIN_ENTRY_POINT] == PLUGIN_MODULE


def test_the_task_runner_console_script_resolves():
    """装成包以后跑任务只有这一个入口:声明的目标必须 import 得到、且是个函数。"""
    from importlib import import_module

    target = _pyproject()["project"]["scripts"]["testplatform-run-task"]
    module_name, _, attribute = target.partition(":")
    assert callable(getattr(import_module(module_name), attribute))
