"""
判据集走清单配置这条缝:CompositionRoot.build(清单路径) 装配出来的设备,
按型号引用的判据集(含设备级行内覆盖)判合格与否。
"""
import copy

import pytest

from tests_support.manifest_factory import build, manifest
from testplatform.composition.root import CompositionRoot
from testplatform.domain.events import HighPowerWarning

# 50Hz 判据集之上再加三条:电压非负、功耗上限用型号参数、逼近额定时只发软信号。
_EXTRA_CRITERIA = [
    {"quantity": "grid_voltage_v", "gt": 0},
    {"quantity": "output_power_w", "min": 0, "max": {"rated_power_w": 1.1}},
    {"quantity": "output_power_w", "max": {"rated_power_w": 0.9}, "severity": "warn"},
]


def _sixty_hz_variant(manifest_dict):
    """60Hz 机型靠行内覆盖用 60Hz 判据,与 50Hz 机型共用同一个型号与同一份点表。"""
    manifest_dict["criteria_profiles"]["grid_50hz"].extend(copy.deepcopy(_EXTRA_CRITERIA))
    manifest_dict["devices"][1].update(
        criteria_override=[{"quantity": "grid_freq_hz", "min": 55, "max": 65}],
        connection={"simulated_response": "4600.0 230.0 60.0"},
    )


MANIFEST = manifest(inverters=["inv_50", "inv_60"], mutate=_sixty_hz_variant)


@pytest.fixture()
def root(tmp_path):
    root = build(tmp_path, MANIFEST)
    yield root
    root.teardown()


def test_50hz_and_60hz_models_share_one_manifest(root):
    """60Hz 机型靠行内覆盖用 60Hz 判据:读出的 60Hz 判合格,而不是抛异常;
    覆盖只换掉频率判据,判据集里的功率软告警线原样保留。"""
    for device_id in ("inv_50", "inv_60"):
        driver = root.get(device_id)
        driver.connect()
        driver.read_all()

    assert root.get("inv_50").device.latest_reading.passed is True

    inv_60 = root.get("inv_60")
    reading = inv_60.device.latest_reading
    assert reading.grid_freq_hz == 60.0
    assert reading.passed is True
    assert [type(e) for e in inv_60.device.pull_events()] == [HighPowerWarning]


def test_device_referencing_unknown_criteria_profile_fails_before_assembly(tmp_path):
    """判据集的引用现在写在型号上,所以报错点名型号——型号写错时一次就报到,
    不用等 100 台同型号设备各自装配一遍。"""
    config = tmp_path / "devices.yaml"
    config.write_text(
        """
criteria_profiles:
  grid_50hz:
    - {quantity: grid_freq_hz, min: 45, max: 55}
models:
  inverter_60hz:
    type: inverter
    protocol: vendor_ascii
    criteria: grid_60hz
    capabilities: [有功功率]
    device_params: [rated_power_w]
    point_table:
      有功功率: {address: 0, length: 8, encoding: ascii, unit: W}
devices:
  - id: inv_01
    model: inverter_60hz
    access: tcp
    mode: simulator
    rated_power_w: 5000
    connection: {simulated_response: "3000.0 230.0 50.0"}
""",
        encoding="utf-8",
    )

    with pytest.raises(KeyError) as excinfo:
        CompositionRoot.build(str(config))

    message = str(excinfo.value)
    assert "inverter_60hz" in message
    assert "grid_60hz" in message
    assert "grid_50hz" in message


@pytest.mark.parametrize("bound", ["min: 45", "max: 55", "gt: 45", "lt: 55"])
def test_equals_written_together_with_a_range_fails_before_assembly(tmp_path, bound):
    """一个判据要么判区间要么判等值。两者同写,求值时必有一半被忽略——
    那正是这个平台上最危险的假通过,所以在装配前就拒掉。"""
    config = tmp_path / "devices.yaml"
    config.write_text(
        f"""
criteria_profiles:
  grid_50hz:
    - {{quantity: grid_freq_hz, equals: "50", {bound}}}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        CompositionRoot.build(str(config))

    message = str(excinfo.value)
    assert "grid_50hz" in message
    assert "equals" in message
    assert bound.split(":")[0] in message


@pytest.mark.parametrize(
    "bad_equals",
    [pytest.param("[1601]", id="列表"), pytest.param("{mode: 1601}", id="映射"), pytest.param("null", id="空值")],
)
def test_equals_must_be_a_single_value_fails_before_assembly(tmp_path, bad_equals):
    """equals 只收一个具体的值(数字 / 字符串 / 布尔):列表和映射说不出"期望什么"。"""
    config = tmp_path / "devices.yaml"
    config.write_text(
        f"""
criteria_profiles:
  meter_220v:
    - {{quantity: 有功计量模式, equals: {bad_equals}}}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        CompositionRoot.build(str(config))

    assert "equals" in str(excinfo.value)
