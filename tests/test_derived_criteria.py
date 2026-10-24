"""
派生判据走清单这条缝:清单一处声明"叫什么、谁参与、允许落在哪",值由用例算出来。

铁律是公式不进 yaml —— 公式配置化就是 ADR 0002 / 0005 明确否决的那门 DSL 的开端。
所以这里验的是清单侧的三件事(参与方、重名、区间)与 judge_derived 的求值;
"增量怎么算、误差怎么求"的端到端在 tests_scenario/test_derived_error.py。
"""
import copy

import pytest

from tests_support.manifest_factory import build, manifest

DUT, REFERENCE = "inv_01", "inv_02"

# 参与方各一台同型号设备:派生判据只认清单里的参与方 id,不看设备类型。
BASE_MANIFEST = manifest(inverters=[DUT, REFERENCE])


DECLARED = {
    "有功误差": {
        "participants": [DUT, REFERENCE],
        "limits": {"min": -1.0, "max": 1.0},
    }
}


def _build(tmp_path, manifest):
    return build(tmp_path, manifest)


def _manifest(**derived):
    """在基础清单上换成给定的 derived_criteria 段(不传就是没有这一段)。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    if derived:
        manifest["derived_criteria"] = derived
    return manifest


def test_judge_derived_reads_the_range_from_the_manifest(tmp_path):
    """改阈值只改 yaml 一处:同一个值在清单写 -1.0/1.0 时合格,写 -0.5/0.5 时不合格。"""
    root = _build(tmp_path, _manifest(**DECLARED))
    try:
        criterion = root.derived_criterion("有功误差")
        assert criterion.participants == (DUT, REFERENCE)
        assert (criterion.limits.low, criterion.limits.high) == (-1.0, 1.0)

        assert root.judge_derived("有功误差", 0.8) is True
        assert root.judge_derived("有功误差", 2.0) is False
        assert root.judge_derived("有功误差", -2.0) is False  # 反向超差也是超差
    finally:
        root.teardown()


def test_a_derived_range_may_judge_one_side_only(tmp_path):
    """只写 max 就是只不许偏大:与单台设备判据同一个口径(Criterion 的另一侧为 None 即不判)。"""
    manifest = _manifest(有功误差={"participants": [DUT, REFERENCE], "limits": {"max": 1.0}})
    root = _build(tmp_path, manifest)
    try:
        assert root.judge_derived("有功误差", 5.0) is False
        assert root.judge_derived("有功误差", -50.0) is True
    finally:
        root.teardown()


def test_a_manifest_without_derived_criteria_behaves_as_before(tmp_path):
    """没写这一段就没有派生判据:设备照旧能连、能读,门槛一点没动。"""
    root = _build(tmp_path, _manifest())
    try:
        assert root.derived_criteria == {}
        inverter = root.get(DUT)
        inverter.connect()
        assert inverter.read("有功功率") == pytest.approx(3000.0)
    finally:
        root.teardown()

    with pytest.raises(KeyError) as excinfo:
        root.derived_criterion("有功误差")
    assert "清单里没有派生判据" in str(excinfo.value)


def test_a_participant_that_is_not_a_device_fails_before_assembly(tmp_path):
    """参与方写错等于判的是另一个量:装配前就报,并列出清单里实际有谁。"""
    manifest = _manifest(
        有功误差={"participants": [DUT, "reference_meter_01"], "limits": {"min": -1.0, "max": 1.0}}
    )

    with pytest.raises(KeyError) as excinfo:
        _build(tmp_path, manifest)

    message = str(excinfo.value)
    assert "reference_meter_01" in message
    assert REFERENCE in message


def test_a_derived_name_clashing_with_a_reading_key_fails_before_assembly(tmp_path):
    """派生量不是读数:名字撞上读数键之后,证据里分不清判的是哪一个。"""
    manifest = _manifest(
        有功功率={"participants": [DUT, REFERENCE], "limits": {"min": 0, "max": 1}}
    )

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    message = str(excinfo.value)
    assert "读数键重名" in message
    assert "有功功率" in message


def test_a_derived_criterion_spans_at_least_two_devices(tmp_path):
    """一台设备读到的量用判据集判就够了:派生的意义是跨设备。"""
    manifest = _manifest(有功误差={"participants": [DUT], "limits": {"min": -1.0, "max": 1.0}})

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "至少两台设备" in str(excinfo.value)

def test_the_same_device_twice_does_not_count_as_two_participants(tmp_path):
    """写成 [DUT, DUT] 只凑够了列表长度:同一台设备的读数不是跨设备的量。"""
    manifest = _manifest(
        有功误差={"participants": [DUT, DUT], "limits": {"min": -1.0, "max": 1.0}}
    )

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    message = str(excinfo.value)
    assert DUT in message
    assert "写了不止一次" in message


def test_a_formula_in_the_manifest_is_rejected(tmp_path):
    """公式不进 yaml:写 formula 这类键就是 ADR 0002 / 0005 否决的那门 DSL 的开端 ——
    清单只收 participants 与 limits,多一个键在装配前就报。
    """
    manifest = _manifest(
        有功误差={
            "participants": [DUT, REFERENCE],
            "limits": {"min": -1.0, "max": 1.0},
            "formula": "(delta_dut - delta_ref) / delta_ref * 100",
        }
    )

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "formula" in str(excinfo.value)

@pytest.mark.parametrize(
    "limits",
    [
        pytest.param({}, id="空区间"),
        pytest.param({"median": 1.0}, id="未知边界"),
        pytest.param({"min": {"rated_power_w": 1.1}}, id="设备参数基准"),
        pytest.param({"max": "1.0"}, id="字符串"),
    ],
)
def test_a_derived_range_only_takes_numeric_min_and_max(tmp_path, limits):
    """派生量是跨设备算出来的数,没有"某台设备的额定功率"可当基准换算;
    写错边界要在装配前报出是哪一个。"""
    manifest = _manifest(有功误差={"participants": [DUT, REFERENCE], "limits": limits})

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "limits" in str(excinfo.value)
