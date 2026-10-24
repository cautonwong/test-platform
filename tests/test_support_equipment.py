"""
周边设备(role: support)与装配顺序。

清单是唯一的声明处:多台被测设备共用一台周边设备时,只声明一个 support 实例。
周边设备走同一套型号 / 点表 / 能力 / 驱动 / 注册表,不是另一套机制;
区别只有两处:它在装配时先连接,而且不判合格(挂掉 = 测试无效 = skip)。
"""
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

from tests_support.manifest_factory import build, manifest, write
from testplatform.composition.root import (
    TEST_INVALID_PREFIX,
    CompositionRoot,
    SupportEquipmentFault,
)
from testplatform.config.manifest import DEFAULT_ROLE, SUPPORT_ROLE, parse_manifest
from testplatform.domain.exceptions import DeviceNotConnectedError, InvariantViolation
from testplatform.domain.grid_simulator import GridSimulator

CONFTEST_PATH = Path(__file__).resolve().parent / "conftest.py"


def _load_tests_conftest():
    """测试层的 conftest 由 pytest 当插件加载,不经过 normal import;
    按文件路径单独加载一份,用来直接调用"周边设备故障 → skip"那唯一一处转换。"""
    spec = importlib.util.spec_from_file_location("tests_conftest_support_equipment", CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load_tests_conftest()

# 被测设备故意声明在周边设备之前:装配顺序不能依赖清单里的书写顺序。
BASE_MANIFEST = manifest(inverters=["inv_01", "inv_03"], support=True)


def _device(manifest_dict, device_id) -> dict:
    return next(spec for spec in manifest_dict["devices"] if spec["id"] == device_id)


def _write(tmp_path, manifest_dict) -> str:
    return write(tmp_path, manifest_dict)


def _build(tmp_path, manifest_dict) -> CompositionRoot:
    return build(tmp_path, manifest_dict)


def _build_error(tmp_path, manifest) -> str:
    with pytest.raises((KeyError, ValueError)) as excinfo:
        _build(tmp_path, manifest)
    return str(excinfo.value)


# ========================= 周边设备进同一份清单 =========================

def test_support_equipment_is_assembled_like_any_other_device(tmp_path):
    """周边设备不是新机制:同一个型号三段式、同一份点表、同一个 @register_device
    注册表。区别只有角色——它没有判据集,所以不判合格。"""
    root = _build(tmp_path, BASE_MANIFEST)
    try:
        assert root.role_of("grid_sim_01") == SUPPORT_ROLE
        assert root.ids_of_role(SUPPORT_ROLE) == ["grid_sim_01"]
        assert root.reading_keys_of("grid_sim_01") == ("Fre", "Ua")
        assert root.model_of("grid_sim_01").criteria is None  # 周边设备不判合格
    finally:
        root.teardown()


def test_role_defaults_to_dut(tmp_path):
    """不写 role 的实例就是被测设备:现有清单不需要为了这张票补 role。"""
    parsed = parse_manifest(BASE_MANIFEST)

    assert parsed.devices[0]["role"] == DEFAULT_ROLE
    assert parsed.ids_of_role(DEFAULT_ROLE) == ("inv_01", "inv_03")


def test_unknown_role_fails_before_assembly(tmp_path):
    manifest_dict = copy.deepcopy(BASE_MANIFEST)
    _device(manifest_dict, "grid_sim_01")["role"] = "duth"

    message = _build_error(tmp_path, manifest_dict)

    assert "grid_sim_01" in message
    assert "duth" in message
    assert "support" in message and "dut" in message  # 可用角色要列出来


# ========================= 装配顺序:周边先,被测后 =========================

def test_assembly_connects_support_equipment_and_leaves_the_dut_to_its_tests(tmp_path):
    """装配顺序的唯一实现点:组合根交出去之前,connect_support 已经按角色把周边设备
    连上,被测设备的连接留在用例里。所以任何一条被测设备用例开始时,
    周边设备一定已经就位,不靠用例自觉去先连它。"""
    root = _build(tmp_path, BASE_MANIFEST)  # 清单里 inv_01 写在 grid_sim_01 之前
    try:
        conftest.connect_support_or_skip(root)

        assert root.get("grid_sim_01").device.state.name == "CONNECTED"
        assert root.get("inv_01").device.state.name == "DISCONNECTED"
        assert root.get("inv_03").device.state.name == "DISCONNECTED"
    finally:
        root.teardown()


# ========================= 周边设备故障 = 测试无效 =========================

def test_support_fault_makes_the_test_invalid_not_the_dut_failed(tmp_path):
    """周边设备连不上时结论是"测试无效":测试层唯一的那一处把它转成 skip,
    reason 带固定前缀(证据层靠它区分无效与不合格),被测设备不被判失败。"""
    root = _build(tmp_path, BASE_MANIFEST)

    def _unreachable():
        raise ConnectionError("网口无响应")

    root.get("grid_sim_01").connect = _unreachable
    try:
        with pytest.raises(pytest.skip.Exception) as excinfo:
            conftest.connect_support_or_skip(root)

        reason = str(excinfo.value)
        assert reason.startswith(TEST_INVALID_PREFIX)
        assert "grid_sim_01" in reason
        # 被测设备既没被连上,更没有被判不合格
        assert root.get("inv_01").device.state.name == "DISCONNECTED"
    finally:
        root.teardown()


def test_support_fault_is_raised_by_the_composition_root_not_by_each_test(tmp_path):
    """转换点只有一个:组合根抛 SupportEquipmentFault,用例里没有任何 try/except。"""
    root = _build(tmp_path, BASE_MANIFEST)
    root.get("grid_sim_01").connect = lambda: (_ for _ in ()).throw(OSError("掉线"))
    try:
        with pytest.raises(SupportEquipmentFault) as excinfo:
            root.connect_support()
        assert excinfo.value.device_id == "grid_sim_01"
    finally:
        root.teardown()


# ========================= 契约测试只对 dut 展开 =========================

class _FakeMetafunc:
    """pytest_generate_tests 只用到 fixturenames / config / parametrize 三样。"""

    def __init__(self, fixturenames, config_path):
        self.fixturenames = list(fixturenames)
        self.config = self._Config(config_path)
        self.calls = []

    class _Config:
        def __init__(self, path):
            self._path = str(path)

        def getoption(self, name):
            assert name == "--device-config"
            return self._path

    def parametrize(self, argname, values, **kwargs):
        self.calls.append((argname, list(values), kwargs))


def test_contract_tests_do_not_expand_to_support_equipment(tmp_path):
    """一致性判定的展开集合按 role 过滤:被测设备各成一行,周边设备不展开。
    新增一种周边设备类型因此不需要在测试层加任何分支。"""
    config_path = _write(tmp_path, BASE_MANIFEST)

    dut_fake = _FakeMetafunc(["inverter"], config_path)
    conftest.pytest_generate_tests(dut_fake)
    assert dut_fake.calls == [("inverter", ["inv_01", "inv_03"], {"indirect": True})]

    support_fake = _FakeMetafunc(["grid_simulator"], config_path)
    conftest.pytest_generate_tests(support_fake)
    assert support_fake.calls == [("grid_simulator", [], {"indirect": True})]


# ========================= 落地一种真实的周边设备类型 =========================

def test_grid_simulator_applies_a_grid_setpoint(grid_sim_01):
    """电网模拟器走的是同一套 driver + domain:设定一次电网,聚合根记下实际输出值。"""
    grid_sim_01.connect()
    setpoint = grid_sim_01.apply_grid(230.0, 50.0)

    assert (setpoint.voltage_v, setpoint.freq_hz) == (230.0, 50.0)
    assert grid_sim_01.device.setpoint is setpoint


def test_grid_simulator_takes_a_three_phase_setpoint_and_reads_it_back(grid_sim_01):
    """上游条款的台架前置是一整块三相设定:一次下发,按同一份量名回读。

    模拟台架回的就是驱动发下去的那几个量 —— 它证明的是"驱动按点表量名下发、按同一份
    量名读回",不是真机接了线;真机的地址与编码还没定义(点表里是占位地址)。
    """
    grid_sim_01.connect()
    applied = grid_sim_01.apply_three_phase(
        {
            "Ua": 230.0,
            "Ub": 228.5,
            "Uc": 229.5,
            "Ia": 5.0,
            "Ib": 0.0,
            "Ic": 0.0,
            "θa": 0.0,
            "θb": -120.0,
            "θc": 120.0,
            "QUaUb": 120.0,
            "QUaUc": -120.0,
            "Fre": 50.0,
            "接线方式": 3,
            "MeterConst": 1,
        }
    )

    assert dict(applied.values) == {
        "Ua": 230.0,
        "Ub": 228.5,
        "Uc": 229.5,
        "Ia": 5.0,
        "Ib": 0.0,
        "Ic": 0.0,
        "θa": 0.0,
        "θb": -120.0,
        "θc": 120.0,
        "QUaUb": 120.0,
        "QUaUc": -120.0,
        "Fre": 50.0,
        "接线方式": 3.0,
        "MeterConst": 1.0,
    }
    assert grid_sim_01.device.three_phase is applied


def test_grid_simulator_refuses_quantities_it_does_not_have():
    """条款里写错的量名(θa 写成 ta)不能静默设了一个没人读的东西。"""
    grid = GridSimulator(device_id="tmp", name="tmp")
    grid.mark_connected()

    with pytest.raises(InvariantViolation) as caught:
        grid.apply_three_phase({"ta": 0.0})
    assert "ta" in str(caught.value) and "θa" in str(caught.value)

    with pytest.raises(InvariantViolation):
        grid.apply_three_phase({"Ua": -1.0})
    with pytest.raises(InvariantViolation):
        grid.apply_three_phase({"Fre": 0.0})
    with pytest.raises(InvariantViolation):
        grid.apply_three_phase({"接线方式": 3.5})  # 枚举是整数编码,不是模拟量


def test_grid_simulator_protects_its_setpoint_invariants():
    """周边设备也只剩真正不该发生的事:未连接不能设定,电压非负、频率为正。"""
    grid = GridSimulator(device_id="tmp", name="tmp")

    with pytest.raises(DeviceNotConnectedError):
        grid.apply_setpoint(230.0, 50.0)

    grid.mark_connected()
    with pytest.raises(InvariantViolation):
        grid.apply_setpoint(-1.0, 50.0)
    with pytest.raises(InvariantViolation):
        grid.apply_setpoint(230.0, 0.0)


def test_successive_three_phase_setpoints_merge_instead_of_replacing():
    """条款的台架前置天然分多段写:先接线方式、再三相电压、再电流、再相角。

    每一段只带自己那几个量,别的量必须**保持原样** —— 替换语义会让前置跑完只剩最后一段,
    而报告里看不出来("前置已设好"变成假的)。同名量以新值为准。

    用一台新的领域对象,不借会话级设备:那个设备被前面用例设过,会掩盖合并语义。
    """
    from testplatform.domain.grid_simulator import GridSimulator

    grid = GridSimulator(device_id="bench_tmp", name="临时台架")
    grid.mark_connected()

    grid.apply_three_phase({"接线方式": 3})
    grid.apply_three_phase({"Ua": 230.0, "Ub": 228.5, "Fre": 50.0})
    applied = grid.apply_three_phase({"Ia": 5.0, "Ua": 231.0})

    assert dict(applied.values) == {
        "接线方式": 3.0,
        "Ua": 231.0,
        "Ub": 228.5,
        "Fre": 50.0,
        "Ia": 5.0,
    }, "两段不相交的设定必须同时保留;同名量 Ua 以新值 231.0 为准"
