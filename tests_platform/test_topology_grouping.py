"""拓扑分组的守卫:分组键的推导、xdist_group 标记、以及 `-n` 漏掉 `--dist loadgroup` 的报错。

全部用假清单 + 假 item 直接调 conftest 里的函数,不 spawn 子进程、秒级跑完。
端到端证明仍在 tests/parallel_topology_check.py,那是给人排障用的,不承担守卫职责:
分组键跟错根、两条用例拿到不同组名,这里的用例会红。
"""
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests_support.nested_pytest import pytest_command

from testplatform.config.manifest import parse_manifest

REPO = Path(__file__).resolve().parent.parent
CONFTEST_PATH = REPO / "tests" / "conftest.py"


def _load_tests_conftest():
    """测试层的 conftest 由 pytest 当插件加载,不经过 normal import:按文件路径单独加载一份。

    不能写 `from conftest import ...`:tests_scenario/ 也有 conftest.py,这个名字会解析到
    哪一个取决于 sys.path 顺序(即收集顺序),那是一处看不见的耦合。
    """
    spec = importlib.util.spec_from_file_location("tests_conftest_topology_grouping", CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load_tests_conftest()
# 这两个名字在本文件里用了十几处,保持原样读起来最短。
_mark_topology_groups = conftest._mark_topology_groups
_require_loadgroup = conftest._require_loadgroup
_topology_group = conftest._topology_group

# 三层链 meter_deep → concentrator → terminal_01;terminal_02 是另一条总线;
# standalone 没有 via:。
MANIFEST = {
    "models": {
        "topology_terminal": {
            "type": "terminal",
            "protocol": "vendor_ascii",
            "capabilities": ["上电运行时间"],
            "point_table": {"上电运行时间": {"address": 0, "length": 8, "encoding": "ascii"}},
        },
        "topology_meter": {
            "type": "meter",
            "protocol": "vendor_ascii",
            "capabilities": ["电压"],
            "point_table": {"电压": {"address": 0, "length": 7, "encoding": "ascii"}},
        },
    },
    "devices": [
        {"id": "terminal_01", "model": "topology_terminal", "access": "tcp"},
        {"id": "terminal_02", "model": "topology_terminal", "access": "tcp"},
        {"id": "concentrator", "model": "topology_terminal", "via": "terminal_01", "access": "tcp"},
        {"id": "meter_a", "model": "topology_meter", "via": "terminal_01", "access": "tcp"},
        {"id": "meter_deep", "model": "topology_meter", "via": "concentrator", "access": "tcp"},
        {"id": "meter_d", "model": "topology_meter", "via": "terminal_02", "access": "tcp"},
        {"id": "standalone", "model": "topology_meter", "access": "tcp"},
    ],
}


class _FakeItem:
    """_mark_topology_groups 用到的两样:callspec.params 看参数化的设备 id,add_marker 收标记。"""

    def __init__(self, device_id=None):
        self.nodeid = f"tests/test_demo.py::test_demo[{device_id}]"
        self._params = {} if device_id is None else {"meter": device_id}
        self.callspec = SimpleNamespace(params=self._params)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)

    @property
    def group(self):
        groups = [marker.args[0] for marker in self.markers if marker.name == "xdist_group"]
        assert len(groups) == 1, f"应该恰好一条 xdist_group 标记,拿到 {groups}"
        return groups[0]


def _manifest():
    return parse_manifest(MANIFEST)


def _device_ids(manifest):
    return {spec["id"]: spec["model"] for spec in manifest.devices}


def _groups_of(*device_ids):
    manifest = _manifest()
    items = [_FakeItem(device_id) for device_id in device_ids]
    _mark_topology_groups(items, manifest, _device_ids(manifest))
    return {item.group for item in items}


def _config(numprocesses, dist="load"):
    return SimpleNamespace(option=SimpleNamespace(numprocesses=numprocesses, dist=dist))


def test_group_key_follows_the_via_chain_to_its_root():
    """链中间层(抄表器)也归到根那台终端,不是自成一组。"""
    manifest = _manifest()

    assert _topology_group(manifest, "concentrator") == "terminal_01"
    assert _topology_group(manifest, "meter_deep") == "terminal_01"
    assert _topology_group(manifest, "meter_a") == "terminal_01"


def test_device_without_via_is_its_own_group():
    manifest = _manifest()

    assert _topology_group(manifest, "standalone") == "standalone"
    assert _topology_group(manifest, "terminal_01") == "terminal_01"


def test_items_on_one_bus_share_a_group_and_another_terminal_gets_another():
    """同一 via: 下的每一条用例(含终端自己)拿到同一个组名;不同终端拿到不同组名。"""
    assert _groups_of("terminal_01", "concentrator", "meter_a", "meter_deep") == {"terminal_01"}
    assert _groups_of("terminal_02", "meter_d") == {"terminal_02"}
    assert _groups_of("meter_a", "meter_d") == {"terminal_01", "terminal_02"}
    assert _groups_of("standalone") == {"standalone"}


def test_item_not_parametrized_by_device_gets_no_group():
    """不落在单台设备总线上的用例(平台自身 / 多设备组合)不参与分组。"""
    manifest = _manifest()
    item = _FakeItem()

    _mark_topology_groups([item], manifest, _device_ids(manifest))

    assert item.markers == []


def test_single_process_run_is_untouched():
    _require_loadgroup(_config(numprocesses=None, dist="no"), _manifest())


def test_dash_n_with_loadgroup_is_allowed():
    _require_loadgroup(_config(numprocesses=4, dist="loadgroup"), _manifest())


def test_dash_n_without_loadgroup_is_a_hard_error():
    with pytest.raises(pytest.UsageError) as excinfo:
        _require_loadgroup(_config(numprocesses=4, dist="load"), _manifest())

    assert "--dist loadgroup" in str(excinfo.value)


def test_forgetting_loadgroup_fails_the_run_before_any_test():
    """真的跑一遍 `-n` 不带 loadgroup:必须非零退出,并且告诉操作者正确命令。"""
    result = subprocess.run(
        pytest_command("-q", "-n", "2", "tests/test_meter.py"),
        cwd=REPO,
        capture_output=True,
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")

    assert result.returncode != 0, output
    assert "--dist loadgroup" in output
