"""
装配期交叉校验:判据集判的量、型号声明的能力,都必须落在这台设备真能判的量里。

失败点原本在第一条读数 —— 那时台架已接、电源已开。宁可在装配期一次报全。
对 capabilities 更阴:能力声明用于"没有就跳过用例",声明了却读不到就是让跳过机制说谎。

"能判的量"是并集,所以这条校验不能在 parse_manifest 里按点表硬比:电表的判据量就是
点表读数键,而 vendor_ascii 型号的判据量是驱动把读数键映射成的聚合根参数名
(有功功率 → output_power_w)。映射只在驱动里,校验因此等驱动造出来之后在组合根里跑。
"""
import pytest

from tests_support.manifest_factory import build, manifest


def _error(tmp_path, manifest_dict) -> str:
    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, manifest_dict)
    return str(excinfo.value)


def test_a_criterion_quantity_the_model_cannot_read_is_rejected(tmp_path):
    """判据集拼错一个量,原本要到第一条读数才 KeyError(domain/criteria.py 的 evaluate)。"""
    broken = manifest(meters=1)
    broken["criteria_profiles"]["meter_220v"] = [{"quantity": "不存在的量", "min": 0}]

    message = _error(tmp_path, broken)

    assert "dtsd1352_modbus" in message  # 哪个型号
    assert "meter_01" in message  # 哪台设备
    assert "不存在的量" in message  # 缺的是哪个量
    assert "['有功电量', '电压']" in message  # 点表里有什么


def test_a_declared_capability_the_point_table_cannot_read_is_rejected(tmp_path):
    """声明了却读不到的能力让"没有就跳过"说谎:用例以为自己测过了。"""
    broken = manifest(meters=1)
    broken["models"]["dtsd1352_modbus"]["capabilities"] = ["有功电量", "无功电量"]

    message = _error(tmp_path, broken)

    assert "dtsd1352_modbus" in message
    assert "声明的能力 ['无功电量']" in message  # 只点名缺的那一项


def test_both_gaps_are_reported_in_one_error(tmp_path):
    """两个型号各缺一处:一次报全,不要改一处再跑一次装配。"""
    broken = manifest(inverters=1, meters=1)
    broken["criteria_profiles"]["grid_50hz"] = [{"quantity": "不存在的量", "min": 0}]
    broken["models"]["dtsd1352_modbus"]["capabilities"] = ["有功电量", "无功电量"]

    message = _error(tmp_path, broken)

    assert "inverter_pv_series" in message and "dtsd1352_modbus" in message
    assert "不存在的量" in message and "无功电量" in message


def test_an_override_judging_an_unreadable_quantity_is_rejected(tmp_path):
    """设备级覆盖是另一条解析路径(组合根里与基础集合并在一条判据集上):
    只校基础集就会漏掉它。"""
    broken = manifest(meters=1)
    broken["devices"][0]["criteria_override"] = [{"quantity": "不存在的量", "min": 0}]

    message = _error(tmp_path, broken)

    assert "meter_01" in message
    assert "不存在的量" in message


def test_a_replaced_criterion_is_checked_as_the_final_one(tmp_path):
    """覆盖整条替换掉基础集那条:校验的是最终生效的判据,被替换的那条不重复要求。"""
    replaced = manifest(meters=1)
    replaced["devices"][0]["criteria_override"] = [{"quantity": "电压", "min": 200, "max": 250}]

    root = build(tmp_path, replaced)
    try:
        judged = [criterion.quantity for criterion in root.get("meter_01").device.criteria.criteria]
    finally:
        root.teardown()

    assert judged == ["电压", "有功电量"]  # 同一物理量只有一条:覆盖是替换,不是追加


def test_a_driver_mapping_that_names_a_key_outside_the_point_table_is_rejected(tmp_path):
    """驱动自述"我要读哪些键"必须都是点表的读数键,否则自述本身会变成新的假通过入口
    (运行时 PointReadings 会因为两边对不上直接报错,那是第一条读数的事)。"""
    broken = manifest(inverters=1)
    broken["models"]["inverter_pv_series"]["point_table"].pop("频率")
    broken["models"]["inverter_pv_series"]["capabilities"].remove("频率")
    broken["criteria_profiles"]["grid_50hz"] = [{"quantity": "grid_freq_hz", "min": 45, "max": 55}]

    message = _error(tmp_path, broken)

    assert "驱动的映射表要读 ['频率']" in message


def test_a_profile_that_judges_the_aggregate_argument_names_still_assembles(tmp_path):
    """vendor_ascii 型号的判据量是聚合根参数名(grid_freq_hz),不是点表读数键(频率);
    只认点表键的话出厂清单里一台逆变器都装不上。"""
    root = build(tmp_path, manifest(inverters=1))
    root.teardown()


def test_derived_criteria_are_not_checked_against_the_point_table(tmp_path):
    """派生量是跨设备算出来的,不在任何单台点表里 —— 不在这条校验里。"""
    crossed = manifest(inverters=2)
    crossed["derived_criteria"] = {
        "有功误差": {"participants": ["inv_01", "inv_02"], "limits": {"min": -1.0, "max": 1.0}}
    }

    root = build(tmp_path, crossed)
    try:
        assert root.derived_criterion("有功误差").participants == ("inv_01", "inv_02")
    finally:
        root.teardown()
