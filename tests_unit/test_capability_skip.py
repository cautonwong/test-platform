"""
能力跳过机制的单元测试:设备型号没声明某能力时,用例在收集阶段被标 skip;
能力齐时原样放行。用"假装收集"的方式跑一遍钩子,不需要真起 pytest 会话,
也不需要真跑设备(和 test_type_registry.py 同一套做法)。
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

CONFTEST_PATH = Path(__file__).resolve().parent.parent / "tests" / "conftest.py"
DEVICES_YAML = Path(__file__).resolve().parent.parent / "configs" / "devices.yaml"


def _load_tests_conftest():
    spec = importlib.util.spec_from_file_location("tests_conftest_capability", CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load_tests_conftest()


class _FakeConfig:
    def __init__(self, path):
        self._path = str(path)
        # 单进程跑:分组守卫(_require_loadgroup)据此直接放行。
        self.option = SimpleNamespace(numprocesses=None, dist="no")

    def getoption(self, name):
        assert name == "--device-config"
        return self._path


class _FakeItem:
    """pytest_collection_modifyitems 只用到这四样东西。"""

    def __init__(self, device_id, required_capabilities):
        self.nodeid = "tests/test_demo.py::test_demo"
        self.callspec = SimpleNamespace(params={"inverter": device_id})
        self._required = required_capabilities
        self.added_markers = []

    def get_closest_marker(self, name):
        assert name == "requires_capability"
        return SimpleNamespace(args=self._required)

    def add_marker(self, marker):
        self.added_markers.append(marker)

    def marks(self, name):
        """只数某一种标记:同一条用例还会被拓扑分组打上 xdist_group。"""
        return [marker for marker in self.added_markers if marker.name == name]


def _collect(device_id, required):
    item = _FakeItem(device_id, required)
    conftest.pytest_collection_modifyitems(_FakeConfig(DEVICES_YAML), [item])
    return item


def test_device_without_the_capability_is_skipped_with_a_reason():
    """configs/devices.yaml 里的逆变器型号声明的是[有功功率, 电压, 频率],没有无功电量。"""
    item = _collect("inv_01", ("无功电量",))

    assert len(item.marks("skip")) == 1
    reason = item.marks("skip")[0].kwargs["reason"]
    assert "inv_01" in reason
    assert "无功电量" in reason


def test_device_with_the_capability_runs_untouched():
    item = _collect("inv_01", ("频率",))

    assert item.marks("skip") == []


def test_marker_on_a_test_that_is_not_parametrized_by_device_is_a_hard_error():
    """判断不了是哪台设备就不能假装放行——静默放行会变成另一个假通过。"""
    item = _FakeItem("inv_01", ("频率",))
    item.callspec = SimpleNamespace(params={"unrelated": "x"})

    with pytest.raises(pytest.UsageError) as excinfo:
        conftest.pytest_collection_modifyitems(_FakeConfig(DEVICES_YAML), [item])

    assert "requires_capability" in str(excinfo.value)
