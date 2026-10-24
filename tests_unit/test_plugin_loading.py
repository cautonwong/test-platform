"""
插件加载的两处调用点(票 09 双加载坑):entry point 能 resolve 时**不许**再传 `-p`。

装了包以后 pytest 按 pyproject 的 pytest11 entry point 自己加载插件;同一个模块再 `-p` 一次
就是两个名字注册同一个模块,pytest 直接 `Plugin already registered` 中止整轮。
判据不是"包装没装",而是"entry point 能不能 resolve 到这个模块"。
"""
from __future__ import annotations

import importlib.metadata

from testplatform import plugin
from testplatform.config import paths
from testplatform.execution.task import _plugin_arguments


class _EntryPoint:
    """`importlib.metadata.EntryPoint` 里这一层用得到的部分:name + load()。"""

    def __init__(self, name: str, module=None, *, raises: bool = False):
        self.name = name
        self._module = module
        self._raises = raises

    def load(self):
        if self._raises:
            raise ImportError("entry point 指到了不存在的模块")
        return self._module


def _fake_entry_points(monkeypatch, entries):
    monkeypatch.setattr(
        paths.importlib.metadata, "entry_points", lambda **_: tuple(entries)
    )


def test_a_resolvable_entry_point_means_no_explicit_plugin_argument(monkeypatch):
    """entry point 真能 resolve 到插件模块 → 调用点不再传 `-p`(传了就是 already registered)。"""
    _fake_entry_points(monkeypatch, [_EntryPoint(paths.PLUGIN_ENTRY_POINT, plugin)])

    assert paths.plugin_from_entry_point() is True
    assert _plugin_arguments() == []


def test_without_an_entry_point_the_plugin_is_loaded_explicitly(monkeypatch):
    """源码布局(没装成包):没有 entry point,必须自己传 `-p testplatform.plugin`。"""
    _fake_entry_points(monkeypatch, [])

    assert paths.plugin_from_entry_point() is False
    assert _plugin_arguments() == ["-p", paths.PLUGIN_MODULE]


def test_an_entry_point_that_cannot_resolve_does_not_count(monkeypatch):
    """装了包但 entry point 指错模块:能 resolve 才算数,否则整轮会一个插件都没有。"""
    _fake_entry_points(monkeypatch, [_EntryPoint(paths.PLUGIN_ENTRY_POINT, raises=True)])

    assert paths.plugin_from_entry_point() is False
    assert _plugin_arguments() == ["-p", paths.PLUGIN_MODULE]


def test_an_entry_point_pointing_at_another_module_does_not_count(monkeypatch):
    _fake_entry_points(monkeypatch, [_EntryPoint(paths.PLUGIN_ENTRY_POINT, importlib.metadata)])

    assert paths.plugin_from_entry_point() is False


def test_broken_metadata_means_the_plugin_is_loaded_explicitly(monkeypatch):
    """元数据层自己坏了不该让整轮起不来:当作没有 entry point,照旧 `-p`。"""

    def explode(**_):
        raise ValueError("元数据读不了")

    monkeypatch.setattr(paths.importlib.metadata, "entry_points", explode)

    assert paths.plugin_from_entry_point() is False
