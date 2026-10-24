"""
结构和 test_inverter.py 完全对称——这正是"新增一种设备的测试套件"
该有的样子:复制这个文件,换成新设备类型的领域方法和字段,
不需要碰 conftest.py 里的 pytest_generate_tests 钩子(它是按 type 泛化的)。
"""
import pytest

from testplatform.cases import case
from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity
from testplatform.drivers.point_table_driver import PointTableDriver


def _battery_default() -> CriteriaProfile:
    """和 configs/devices.yaml 里的 battery_default 同形。"""
    return CriteriaProfile(
        name="battery_default",
        criteria=(
            Criterion("voltage_v", low=0, low_exclusive=True),
            Criterion("soc_percent", low=10, severity=Severity.WARN),
        ),
    )


@case(id="M_00_00_01_000003_01", clause="GBT34131-5.1.1", tags=["一致性", "通信"])
@pytest.mark.contract
def test_battery_connects(battery):
    battery.connect()
    assert battery.device.state.name == "CONNECTED"


@case(id="M_00_00_01_000003_02", clause="GBT34131-5.2.1", tags=["一致性", "读数"])
@pytest.mark.contract
def test_battery_soc_in_range(battery):
    battery.connect()
    soc = battery.read("SOC")
    assert 0 <= soc <= 100


@case(id="M_00_00_01_000003_03", clause="GBT34131-5.2.2", tags=["一致性", "读数"])
@pytest.mark.contract
def test_battery_reads_through_the_generic_point_table_driver(battery):
    """和电表是同一条通用读取路径:没有一设备一方法的 read_soc。"""
    assert isinstance(battery, PointTableDriver)
    assert not hasattr(battery, "read_soc")


@case(id="M_00_00_01_000003_04", clause="GBT34131-5.2.3", tags=["一致性", "读数"])
@pytest.mark.contract
def test_battery_reading_key_reaches_the_domain_quantity(battery):
    """读数键 → 聚合根参数名这一步错了,判据会在缺量的映射上判"合格"。"""
    battery.connect()
    reading = battery.read_all()

    for key, quantity in (("SOC", "soc_percent"), ("电压", "voltage_v"), ("电流", "current_a")):
        assert reading.readings[key] == getattr(battery.device.history[-1], quantity)


@case(id="M_00_00_01_000003_05", clause="GBT34131-5.2.4", tags=["一致性", "告警"])
@pytest.mark.contract
def test_battery_low_soc_flag_false_when_healthy(battery):
    battery.connect()
    battery.read("SOC")
    assert battery.device.is_low_soc is False


@pytest.mark.parametrize(
    "soc",
    [
        pytest.param(-5, id="SOC为负"),
        pytest.param(150, id="SOC超过100%"),
    ],
)
def test_battery_rejects_soc_out_of_range(soc):
    """SOC 越出 [0, 100] 是平台自身该发现的不变量违反,依然抛错——
    它和"电压偏低判不合格"是两种不同性质的事。"""
    from testplatform.domain.battery import Battery
    from testplatform.domain.exceptions import InvariantViolation

    b = Battery(device_id="tmp", name="tmp", capacity_ah=50, criteria=_battery_default())
    b.mark_connected()
    with pytest.raises(InvariantViolation):
        b.record_reading(soc_percent=soc, voltage_v=50, current_a=1)


def test_battery_records_zero_voltage_as_failing_not_passing():
    """0V 电池读数以前被 closed min: 0 判成合格;开区间后判不合格,读数照样留着。"""
    from testplatform.domain.battery import Battery

    b = Battery(device_id="tmp", name="tmp", capacity_ah=50, criteria=_battery_default())
    b.mark_connected()

    reading = b.record_reading(soc_percent=50, voltage_v=0, current_a=1)

    assert reading is b.history[-1]
    assert reading.passed is False
    assert [br.quantity for br in reading.breaches] == ["voltage_v"]
    assert reading.breaches[0].exceeded_by == pytest.approx(0.0)


def test_battery_records_negative_voltage_as_failing_reading():
    """电压为负以前抛错,现在记录并判不合格——读数不再丢掉。"""
    from testplatform.domain.battery import Battery

    b = Battery(device_id="tmp", name="tmp", capacity_ah=50, criteria=_battery_default())
    b.mark_connected()

    reading = b.record_reading(soc_percent=50, voltage_v=-1, current_a=1)

    assert reading is b.history[-1]
    assert reading.passed is False
    assert reading.breaches[0].quantity == "voltage_v"
