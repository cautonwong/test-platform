"""entry point 插件发现:装了的插件必须被导入;导入失败要带插件名报错。"""
import types

import pytest

from testplatform.composition import registrations


def _entry(name: str, value: str, load):
    entry = types.SimpleNamespace(name=name, value=value)
    entry.load = load
    return entry


def _patch_entry_points(monkeypatch, by_group):
    seen = []

    def fake_entry_points(group=None):
        seen.append(group)
        return tuple(by_group.get(group, ()))

    monkeypatch.setattr(registrations.importlib.metadata, "entry_points", fake_entry_points)
    return seen


def test_every_declared_group_is_scanned(monkeypatch):
    seen = _patch_entry_points(monkeypatch, {})

    registrations.load_plugins()

    assert seen == list(registrations.PLUGIN_GROUPS)


def test_a_plugin_is_loaded(monkeypatch):
    loaded = []
    entry = _entry(
        "fake_driver",
        "fake_module:register",
        lambda: loaded.append(True) or types.ModuleType("fake_module"),
    )
    _patch_entry_points(monkeypatch, {"testplatform.drivers": [entry]})

    registrations.load_plugins()

    assert loaded == [True]


def test_a_broken_plugin_fails_with_its_name(monkeypatch):
    def boom():
        raise ImportError("no such module")

    entry = _entry("broken_bus", "broken_module:register", boom)
    _patch_entry_points(monkeypatch, {"testplatform.transports": [entry]})

    with pytest.raises(RuntimeError, match="broken_bus"):
        registrations.load_plugins()
