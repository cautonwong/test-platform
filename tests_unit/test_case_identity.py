"""
用例身份、统一筛选入口(--select)与适用矩阵的单元测试。

和 test_capability_skip.py 同一套做法:直接调收集阶段的钩子 + 假的 item/config,
不需要真起一个 pytest 会话,也不需要真连设备。
"""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from testplatform import plugin
from testplatform.cases import (
    Case,
    case,
    case_from_param,
    case_of,
    case_param_of,
    parse_matrix_entries,
    parse_select,
)
from testplatform.config.manifest import parse_manifest

# 型号 / 设备 / 矩阵的最小清单:两个型号各一台设备,矩阵决定它们跑哪些用例。
BASE_MANIFEST = {
    "models": {
        "inverter_pv_series": {
            "type": "inverter",
            "protocol": "vendor_ascii",
            "capabilities": ["有功功率", "频率"],
            "point_table": {"有功功率": {"address": 0, "length": 8, "encoding": "ascii"}},
        },
        "battery_lfp_series": {
            "type": "battery",
            "protocol": "vendor_ascii",
            "capabilities": ["SOC"],
            "point_table": {"SOC": {"address": 0, "length": 6, "encoding": "ascii"}},
        },
    },
    "devices": [
        {"id": "inv_01", "model": "inverter_pv_series", "access": "tcp"},
        {"id": "bat_01", "model": "battery_lfp_series", "access": "serial"},
    ],
    "test_matrix": {
        "inverter_pv_series": ["tag:温度"],
        "battery_lfp_series": ["M_00_00_01_000003_01"],
    },
}


class _FakeConfig:
    """收集钩子只用到 getoption:--device-config / --select / file_or_dir,外加记范围的 stash。"""

    def __init__(self, path, select="", args=()):
        self._options = {
            "--device-config": str(path),
            "--select": select,
            "file_or_dir": list(args),
        }
        self.stash = pytest.Stash()

    def getoption(self, name):
        return self._options[name]


class _FakeItem:
    """收集钩子用到 nodeid / callspec / obj 三样。"""

    def __init__(self, nodeid, test_function=None, device_id=None):
        self.nodeid = nodeid
        self.obj = test_function or _plain_test
        params = {} if device_id is None else {"inverter": device_id, "battery": device_id}
        self.callspec = SimpleNamespace(params=params) if device_id else None


def _plain_test():
    """没标 @case 的用例。"""


@case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=["一致性", "读数"])
def _annotated_test():
    """标了 @case 的用例。"""


def _write_manifest(tmp_path, manifest) -> Path:
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture()
def plain_config(tmp_path):
    """没有矩阵的清单:只测 --select 的筛选,不和适用矩阵混在一起。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    del manifest["test_matrix"]
    return _write_manifest(tmp_path, manifest)


def _apply(config, items):
    plugin._apply_case_selection(config, items)
    return items


# ========================= 身份:唯一事实源是测试函数本身 =========================

def test_case_of_reads_the_identity_off_the_test_function():
    """公开访问点:函数与 pytest 的 item 都拿到同一份身份(证据层从这里取)。"""
    identity = case_of(_annotated_test)
    via_item = case_of(_FakeItem("tests/test_x.py::_annotated_test", _annotated_test))

    assert identity == Case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=("一致性", "读数"))
    assert via_item == identity


def test_identity_has_no_second_copy():
    """改函数上的 @case 就改了收集期看到的值:没有第二份清单可以漂移。"""
    @case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2")
    def demo():
        ...

    assert case_of(demo).clause == "DLT645-4.5.2"

    case(id="M_00_00_01_000002_10", clause="DLT645-4.5.3")(demo)

    assert case_of(demo).id == "M_00_00_01_000002_10"
    assert case_of(demo).clause == "DLT645-4.5.3"


def test_test_without_case_declaration_has_no_identity():
    assert case_of(_plain_test) is None
    assert case_of(_FakeItem("tests/test_x.py::_plain_test")) is None


# ========================= 编号唯一性:一个测试函数一个编号 =========================

def test_two_functions_sharing_one_case_id_fail_at_collection():
    """一个编号挂在两个不同函数上:归因分不清证据属于谁,收集期就报错并给出两个 nodeid。"""
    @case(id="M_00_00_01_000005_09", clause="GBT37408-5.2.1")
    def one():
        ...

    @case(id="M_00_00_01_000005_09", clause="GBT37408-5.2.1")
    def other():
        ...

    items = [
        _FakeItem("tests/test_x.py::one", one),
        _FakeItem("tests/test_x.py::other", other),
    ]

    with pytest.raises(pytest.UsageError) as excinfo:
        plugin._validate_case_ids_are_unique(items)

    message = str(excinfo.value)
    assert "M_00_00_01_000005_09" in message
    assert "tests/test_x.py::one" in message and "tests/test_x.py::other" in message


def test_parametrized_nodes_of_one_function_are_not_duplicates():
    """同一个函数按参数展开成 4 个节点 = 同一条用例跑了 4 轮:不报重号。"""
    items = [
        _FakeItem(f"tests/test_x.py::annotated[{freq}]", _annotated_test)
        for freq in (47.5, 49.5, 50.5, 51.5)
    ]

    plugin._validate_case_ids_are_unique(items)


# ====== 身份由参数给:一个函数跑 N 条条款,N 个节点各用自己的编号 ======


@case_from_param("case_id", tags=["一致性", "设置"])
def _setting_case_test():
    """设置类条款:编号与 clause 不在装饰器里,在清单里。"""


def _setting_cases_manifest(tmp_path):
    manifest = copy.deepcopy(BASE_MANIFEST)
    # 可写的点得是写路径真能编码的:ascii 文本 token 没有唯一的写法(补空格?补几个?),
    # 写不回去就成不了回读比对,所以这里加一个数值点。
    manifest["models"]["inverter_pv_series"]["point_table"]["有功功率设定"] = {
        "address": 1,
        "length": 2,
        "encoding": "uint",
        "writable": True,
    }
    manifest["setting_cases"] = {
        "inverter_pv_series": {
            "M_00_00_01_000002_18": {
                "clause": "DLT645-4.5.2",
                "steps": [{"write": "有功功率设定", "value": "1600", "expect": "1600"}],
            },
            "M_00_00_0A_000008_01": {
                "clause": "DLT645-4.5.3",
                "steps": [{"write": "有功功率设定", "value": "1601", "expect": "1601"}],
            },
        }
    }
    return _write_manifest(tmp_path, manifest)


def _parametrised_item(nodeid, case_id):
    item = _FakeItem(nodeid, _setting_case_test)
    item.callspec = SimpleNamespace(params={"case_id": case_id})
    return item


def test_each_parametrised_node_gets_its_own_clause_identity(tmp_path):
    """一个函数跑 N 条条款:编号与 clause 各从清单取,证据与归因里才分得清是谁的记录。"""
    config = _FakeConfig(_setting_cases_manifest(tmp_path))
    items = [
        _parametrised_item("tests/test_x.py::setting[M_00_00_01_000002_18]", "M_00_00_01_000002_18"),
        _parametrised_item("tests/test_x.py::setting[M_00_00_0A_000008_01]", "M_00_00_0A_000008_01"),
    ]

    plugin._apply_param_case_identity(config, items)

    assert case_of(items[0]) == Case(
        id="M_00_00_01_000002_18", clause="DLT645-4.5.2", tags=("一致性", "设置")
    )
    assert case_of(items[1]) == Case(
        id="M_00_00_0A_000008_01", clause="DLT645-4.5.3", tags=("一致性", "设置")
    )
    # 函数本身没有身份:编号只存在于清单里,不会出现第二份可以漂移的值。
    assert case_of(_setting_case_test) is None
    assert case_param_of(_setting_case_test).param == "case_id"


def test_a_case_id_that_is_not_in_the_manifest_is_rejected(tmp_path):
    """参数值就是条款编号:清单里没有这一条,收集期就报错并列出已声明的编号。"""
    config = _FakeConfig(_setting_cases_manifest(tmp_path))
    items = [_parametrised_item("tests/test_x.py::setting[M_99_99_99_999999_99]", "M_99_99_99_999999_99")]

    with pytest.raises(pytest.UsageError) as excinfo:
        plugin._apply_param_case_identity(config, items)

    message = str(excinfo.value)
    assert "M_99_99_99_999999_99" in message
    assert "M_00_00_01_000002_18" in message and "M_00_00_0A_000008_01" in message



# ========================= --select 的四种写法 =========================

def test_select_by_model_keeps_only_that_models_devices(plain_config):
    items = [
        _FakeItem("tests/test_inverter.py::t[inv_01]", device_id="inv_01"),
        _FakeItem("tests/test_battery.py::t[bat_01]", device_id="bat_01"),
    ]

    kept = _apply(_FakeConfig(plain_config, "model:battery_lfp_series"), items)

    assert [item.nodeid for item in kept] == ["tests/test_battery.py::t[bat_01]"]


def test_select_by_capability_uses_what_the_model_declares(plain_config):
    """能力是型号声明的:逆变器型号没有 SOC,电池型号有能力 SOC。"""
    items = [
        _FakeItem("tests/test_inverter.py::t[inv_01]", device_id="inv_01"),
        _FakeItem("tests/test_battery.py::t[bat_01]", device_id="bat_01"),
    ]

    kept = _apply(_FakeConfig(plain_config, "capability:SOC"), items)

    assert [item.nodeid for item in kept] == ["tests/test_battery.py::t[bat_01]"]


def test_select_by_tag_reads_tags_from_the_case_declaration(plain_config):
    items = [
        _FakeItem("tests/test_x.py::annotated", _annotated_test),
        _FakeItem("tests/test_x.py::plain", _plain_test),
    ]

    kept = _apply(_FakeConfig(plain_config, "tag:读数"), items)

    assert [item.nodeid for item in kept] == ["tests/test_x.py::annotated"]


def test_select_by_case_matches_the_id_prefix(plain_config):
    @case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2")
    def one():
        ...

    @case(id="M_00_00_01_000003_01", clause="GBT34131-5.1.1")
    def other():
        ...

    items = [
        _FakeItem("tests/test_x.py::one", one),
        _FakeItem("tests/test_x.py::other", other),
    ]

    kept = _apply(_FakeConfig(plain_config, "case:M_00_00_01_000002"), items)

    assert [item.nodeid for item in kept] == ["tests/test_x.py::one"]


def test_select_conditions_stack_with_and(plain_config):
    """model 与 tag 同时给 = 两个条件都要满足。"""
    items = [
        _FakeItem("tests/test_x.py::annotated[inv_01]", _annotated_test, device_id="inv_01"),
        _FakeItem("tests/test_x.py::plain[inv_01]", _plain_test, device_id="inv_01"),
    ]

    kept = _apply(_FakeConfig(plain_config, "model:inverter_pv_series,tag:读数"), items)

    assert [item.nodeid for item in kept] == ["tests/test_x.py::annotated[inv_01]"]


def test_parse_select_rejects_an_unknown_kind_instead_of_running_everything():
    with pytest.raises(ValueError) as excinfo:
        parse_select("model:inverter,foo:bar")

    assert "foo:bar" in str(excinfo.value)
    assert "tag" in str(excinfo.value)  # 列出可用写法


def test_select_matching_nothing_reports_why_instead_of_zero_green(plain_config):
    items = [_FakeItem("tests/test_inverter.py::t[inv_01]", device_id="inv_01")]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(plain_config, "capability:无功电量"), items)

    message = str(excinfo.value)
    assert "capability:无功电量" in message
    assert "没有设备声明这项能力" in message


def test_select_by_unknown_case_id_lists_the_ids_it_saw(plain_config):
    items = [_FakeItem("tests/test_x.py::annotated", _annotated_test)]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(plain_config, "case:M_00_00_NOPE"), items)

    assert "M_00_00_01_000005_02" in str(excinfo.value)


# ========================= 适用矩阵 =========================

def test_matrix_decides_which_cases_a_model_runs(tmp_path):
    """同一份矩阵:温度型号只跑 tag:温度,另一个同类型型号两条都跑。
    不同型号跑不同子集是合法的,代价是每条用例至少得有一个型号跑它(见下一条用例)。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["models"]["inverter_hot_series"] = dict(
        manifest["models"]["inverter_pv_series"], capabilities=["有功功率"]
    )
    manifest["devices"].append({"id": "inv_hot", "model": "inverter_hot_series", "access": "tcp"})
    manifest["test_matrix"]["inverter_hot_series"] = ["tag:温度", "tag:读数"]
    path = _write_manifest(tmp_path, manifest)

    @case(id="M_00_00_01_000005_01", clause="GBT37408-5.1.1", tags=["温度"])
    def tagged():
        ...

    @case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=["读数"])
    def other_tag():
        ...

    @case(id="M_00_00_01_000003_01", clause="GBT34131-5.1.1")
    def battery_case():
        ...

    items = [
        _FakeItem("tests/test_x.py::tagged[inv_01]", tagged, device_id="inv_01"),
        _FakeItem("tests/test_x.py::other_tag[inv_01]", other_tag, device_id="inv_01"),
        _FakeItem("tests/test_x.py::tagged[inv_hot]", tagged, device_id="inv_hot"),
        _FakeItem("tests/test_x.py::other_tag[inv_hot]", other_tag, device_id="inv_hot"),
        _FakeItem("tests/test_x.py::battery_case[bat_01]", battery_case, device_id="bat_01"),
    ]

    kept = _apply(_FakeConfig(path), items)

    assert [item.nodeid for item in kept] == [
        "tests/test_x.py::tagged[inv_01]",
        "tests/test_x.py::tagged[inv_hot]",
        "tests/test_x.py::other_tag[inv_hot]",
        "tests/test_x.py::battery_case[bat_01]",
    ]


def test_device_case_without_identity_fails_at_collection(tmp_path):
    """按设备参数化却没打 @case:矩阵无法表达它的适用范围 → 报错,不是静默少跑。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["test_matrix"]["inverter_pv_series"] = ["tag:读数"]
    path = _write_manifest(tmp_path, manifest)
    items = [
        _FakeItem("tests/test_x.py::annotated[inv_01]", _annotated_test, device_id="inv_01"),
        _FakeItem("tests/test_x.py::plain[inv_01]", _plain_test, device_id="inv_01"),
    ]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(path), items)

    message = str(excinfo.value)
    assert "tests/test_x.py::plain[inv_01]" in message
    assert "@case" in message


def test_device_case_covered_by_no_models_matrix_fails_at_collection(tmp_path):
    """有身份但不在任何型号的矩阵里:那个型号会静默不再测这条 → 报错并列出它。"""
    @case(id="M_00_00_01_000005_01", clause="GBT37408-5.1.1", tags=["温度"])
    def tagged():
        ...

    @case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=["读数"])
    def orphan():
        ...

    path = _write_manifest(tmp_path, copy.deepcopy(BASE_MANIFEST))
    items = [
        _FakeItem("tests/test_x.py::tagged[inv_01]", tagged, device_id="inv_01"),
        _FakeItem("tests/test_x.py::orphan[inv_01]", orphan, device_id="inv_01"),
    ]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(path), items)

    message = str(excinfo.value)
    assert "tests/test_x.py::orphan[inv_01]" in message
    assert "M_00_00_01_000005_02" in message
    assert "inverter_pv_series" in message



def test_model_absent_from_the_matrix_is_not_restricted(tmp_path):
    """不在矩阵里的型号 = 不受限,它的用例全跑(免得漏测)。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    del manifest["test_matrix"]["battery_lfp_series"]
    path = _write_manifest(tmp_path, manifest)
    items = [
        _FakeItem("tests/test_x.py::annotated[bat_01]", _annotated_test, device_id="bat_01"),
    ]

    kept = _apply(_FakeConfig(path), items)

    assert [item.nodeid for item in kept] == ["tests/test_x.py::annotated[bat_01]"]


def test_matrix_entry_pointing_at_no_case_fails_at_collection(tmp_path):
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["test_matrix"]["inverter_pv_series"] = ["M_00_00_NOPE"]
    path = _write_manifest(tmp_path, manifest)
    items = [_FakeItem("tests/test_x.py::annotated[inv_01]", _annotated_test, device_id="inv_01")]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(path), items)

    message = str(excinfo.value)
    assert "M_00_00_NOPE" in message
    assert "inverter_pv_series" in message


def test_matrix_reference_is_not_checked_when_only_one_file_runs(tmp_path):
    """只跑一个文件时,别的文件的用例根本没被收集:不能拿它去比,否则误报。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["test_matrix"]["inverter_pv_series"] = ["tag:温度"]
    path = _write_manifest(tmp_path, manifest)
    items = [_FakeItem("tests/test_x.py::annotated[inv_01]", _annotated_test, device_id="inv_01")]

    with pytest.raises(pytest.UsageError) as excinfo:
        _apply(_FakeConfig(path, args=("tests/test_x.py",)), items)

    # 报的是"筛完没有可跑的",不是"矩阵写错了"
    assert "筛完没有任何用例可跑" in str(excinfo.value)


def test_matrix_rejects_an_unknown_model_at_parse_time():
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["test_matrix"]["no_such_model"] = ["tag:温度"]

    with pytest.raises(KeyError) as excinfo:
        parse_manifest(manifest)

    assert "no_such_model" in str(excinfo.value)
    assert "inverter_pv_series" in str(excinfo.value)


def test_matrix_entries_only_accept_case_ids_or_tags():
    with pytest.raises(ValueError) as excinfo:
        parse_matrix_entries("inverter_pv_series", ["model:inverter_pv_series"])

    assert "model:inverter_pv_series" in str(excinfo.value)


def test_case_select_falls_back_to_the_function_name_without_case_declaration(plain_config):
    """没标 @case 的用例也能被点名只跑某一条(按函数名前缀),不必先补身份。"""
    items = [
        _FakeItem("tests/test_x.py::test_meter_connects[meter_01]", _plain_test),
        _FakeItem("tests/test_x.py::test_battery_connects[bat_01]", _plain_test),
    ]

    kept = _apply(_FakeConfig(plain_config, "case:test_meter_connects"), items)

    assert [item.nodeid for item in kept] == ["tests/test_x.py::test_meter_connects[meter_01]"]
