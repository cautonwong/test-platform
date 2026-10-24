"""
这个文件演示两种用例写法的区别,新增设备类型时建议照搬这个结构。

- 用 `inverter` 这个参数化 fixture 写的用例(标了 @pytest.mark.contract),
  会在收集阶段自动对 devices.yaml 里每一个型号 type: inverter 的设备各展开一次
  (机制见 tests/conftest.py 的 pytest_generate_tests)。新增一台逆变器
  不需要新增任何测试代码,报告里会看到每台设备独立的一行结果。
  这类用例适合写"这一类设备都必须满足的通用规则"。

- 用 `inverter_01` / `inverter_02_via_ipc` 这种具名 fixture 写的用例,
  只针对某一个具体实例,适合验证"这个特定场景/这条特定接入链路"本身
  (比如专门验证工控机网关这条链路能不能正常工作)。

- 直接 new 一个 Inverter() 聚合根、不经过任何 fixture 的用例,
  测的是"类级别的不变量规则",不应该也不需要对每个实例重复跑。
"""
import time

import pytest

from testplatform.cases import case
from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity
from testplatform.domain.device import HISTORY_WINDOW
from testplatform.domain.inverter import Inverter
from testplatform.drivers.point_table_driver import PointTableDriver


def _grid_50hz(rated_power_w: float) -> CriteriaProfile:
    """和 configs/devices.yaml 里的 grid_50hz 同形;类级别用例不经过组合根,自己拼一份。"""
    return CriteriaProfile(
        name="grid_50hz",
        criteria=(
            Criterion("grid_freq_hz", low=45, high=55),
            Criterion("grid_voltage_v", low=0, low_exclusive=True),
            Criterion("output_power_w", low=0, high=rated_power_w * 1.1),
            Criterion("output_power_w", high=rated_power_w * 0.9, severity=Severity.WARN),
        ),
    )


# ========================= 契约测试:对每一台逆变器都要成立 =========================

@case(id="M_00_00_01_000005_01", clause="GBT37408-5.1.1", tags=["一致性", "通信"])
@pytest.mark.contract
def test_inverter_connects(inverter):
    inverter.connect()
    assert inverter.device.state.name == "CONNECTED"


@case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=["一致性", "读数"])
@pytest.mark.contract
def test_inverter_reads_physical_value_by_reading_key(inverter):
    """读数键进、物理量出:用例不知道应答行里的第几个字段是功率。"""
    inverter.connect()

    power = inverter.read("有功功率")
    voltage = inverter.read("电压")
    freq = inverter.read("频率")

    assert 0 <= power <= inverter.device.rated_power_w * 1.1
    assert voltage > 0
    assert 45 <= freq <= 65


@case(id="M_00_00_01_000005_03", clause="GBT37408-5.2.2", tags=["一致性", "读数"])
@pytest.mark.contract
def test_inverter_reads_through_the_generic_point_table_driver(inverter):
    """和电表是同一条通用读取路径:没有一设备一方法的 read_output_power。"""
    assert isinstance(inverter, PointTableDriver)
    assert not hasattr(inverter, "read_output_power")


@case(id="M_00_00_01_000005_04", clause="GBT37408-5.2.3", tags=["一致性", "读数"])
@pytest.mark.contract
def test_inverter_reading_key_reaches_the_domain_quantity(inverter):
    """读数键 → 聚合根参数名这一步错了,判据会在缺量的映射上判"合格"。
    读一个键,聚合根的对应字段必须拿到同一个数。"""
    inverter.connect()
    reading = inverter.read_all()

    for key, quantity in (
        ("有功功率", "output_power_w"),
        ("电压", "grid_voltage_v"),
        ("频率", "grid_freq_hz"),
    ):
        assert reading.readings[key] == getattr(inverter.device.latest_reading, quantity)


@case(id="M_00_00_01_000006_02", clause="GBT37408-5.2.4", tags=["一致性", "记录"])
@pytest.mark.contract
def test_inverter_reading_recorded_in_history(inverter):
    """composition_root 是会话级 fixture,聚合根被所有用例共用,history 会跨用例累积,
    所以只断言"本次多了一条",不断言累计绝对值(否则结果依赖执行顺序)。
    断言计数器而不是 len(history):history 是固定窗口,长度会封顶。"""
    inverter.connect()
    before = inverter.device.readings_recorded
    inverter.read_all()
    assert inverter.device.readings_recorded == before + 1


@case(id="M_00_00_01_000004_02", clause="GBT37408-5.3.1", tags=["一致性", "实时性"])
@pytest.mark.contract
@pytest.mark.real_hardware
def test_inverter_responds_within_protocol_timeout(composition_root, inverter, inverter_id):
    """响应延迟只有连真实设备才有意义,模拟器是瞬间返回的,测了也没有意义。
    这里演示两层跳过机制:
      1. 类型级别:标了 real_hardware 的用例,--force-simulator 时全局跳过
         (逻辑在 testplatform/plugin.py 的 pytest_collection_modifyitems 里)。
      2. 实例级别:即使没有加 --force-simulator,如果这台具体设备本身
         配置的就是 simulator 模式,也应该在用例内部主动跳过,
         而不是"测了但其实没测到东西"却显示为通过。
    """
    if composition_root.mode_of(inverter_id) != "real":
        pytest.skip(f"{inverter_id} 当前运行在模拟器模式,响应延迟测试在模拟器下没有意义")

    inverter.connect()
    start = time.monotonic()
    inverter.read("有功功率")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5


# ========================= 类级别不变量:不需要遍历任何实例 =========================

def test_inverter_rejects_read_before_connect():
    """直接测聚合根本身,完全不需要 transport/composition_root,毫秒级跑完。
    规则是"Inverter 这个类"的规则,不是某一台具体设备的规则,
    所以不应该用 inverter 这个参数化 fixture 去遍历设备清单——
    遍历 3 台配置相同的模拟器只会把同一个逻辑重复跑 3 遍,没有任何新信息。"""
    from testplatform.domain.exceptions import DeviceNotConnectedError

    inv = Inverter(device_id="tmp", name="tmp", rated_power_w=5000, criteria=CriteriaProfile("tmp"))
    with pytest.raises(DeviceNotConnectedError):
        inv.record_reading(1000, 230, 50)


@pytest.mark.parametrize(
    "power, voltage, freq, breached",
    [
        pytest.param(-100, 230, 50, "output_power_w", id="功率为负"),
        pytest.param(999_999, 230, 50, "output_power_w", id="功率远超额定值"),
        pytest.param(3000, 230, 70, "grid_freq_hz", id="频率超出判据范围"),
        pytest.param(3000, -10, 50, "grid_voltage_v", id="电压为负"),
    ],
)
def test_inverter_records_out_of_spec_readings(power, voltage, freq, breached):
    """超范围的读数不再抛错,而是被记录下来并在读数上标出超了哪条判据——
    坏设备也要留下"实测多少"这份证据,抛错只会让它变成一个 error。"""
    inv = Inverter(
        device_id="tmp", name="tmp", rated_power_w=5000, criteria=_grid_50hz(5000)
    )
    inv.mark_connected()

    reading = inv.record_reading(power, voltage, freq)

    assert reading is inv.latest_reading
    assert reading.passed is False
    rejected = [b.quantity for b in reading.breaches if b.severity is Severity.REJECT]
    assert rejected == [breached]


def test_inverter_records_zero_voltage_as_failing_not_passing():
    """0V 不是合格电压:开区间下 0 落在边界上也算越界。
    以前 0V 因为 min: 0 是闭区间而被判合格,那是假合格。"""
    inv = Inverter(
        device_id="tmp", name="tmp", rated_power_w=5000, criteria=_grid_50hz(5000)
    )
    inv.mark_connected()

    reading = inv.record_reading(3000, 0, 50)

    assert inv.history == (reading,)
    assert reading.passed is False
    assert [b.quantity for b in reading.breaches] == ["grid_voltage_v"]
    assert reading.breaches[0].exceeded_by == pytest.approx(0.0)


def test_inverter_records_60hz_reading_as_failing_not_raising():
    """50Hz 判据下的 60Hz 机型:读数照样进 history,结论是不合格,并说清超出 5Hz。"""
    inv = Inverter(
        device_id="tmp", name="tmp", rated_power_w=5000, criteria=_grid_50hz(5000)
    )
    inv.mark_connected()

    reading = inv.record_reading(3000, 230, 60)

    assert inv.history == (reading,)
    assert reading.passed is False
    assert reading.breaches[0].quantity == "grid_freq_hz"
    assert reading.breaches[0].exceeded_by == pytest.approx(5.0)


def test_history_keeps_only_the_last_window_while_the_counter_keeps_counting():
    """没有上界的历史会跟着跑时长一起涨:窗口满 1000 条后只留最近的,且 latest_reading
    仍是最后那次。readings_recorded 不受裁剪影响 —— 证据层靠它算增量。"""
    inv = Inverter(device_id="tmp", name="tmp", rated_power_w=5000, criteria=_grid_50hz(5000))
    inv.mark_connected()

    for index in range(HISTORY_WINDOW + 5):
        inv.record_reading(3000 + index, 230, 50)

    assert len(inv.history) == HISTORY_WINDOW
    assert inv.history[0].output_power_w == 3005, "窗口里只剩最近 1000 条,最旧的是第 6 条"
    assert inv.latest_reading.output_power_w == 3000 + HISTORY_WINDOW + 4
    assert inv.readings_recorded == HISTORY_WINDOW + 5


# ========================= 针对特定实例/特定接入链路的测试 =========================

@case(id="M_00_00_01_000006_04_2", clause="GBT37408-5.1.2", tags=["链路"])
def test_inverter_via_industrial_pc_gateway(inverter_02_via_ipc):
    """验证的是"接入方式本身":经工控机网关接入的逆变器,读数、
    不变量校验、状态流转和直连设备用的是同一套 driver/domain 代码,
    唯一区别只在 devices.yaml 里的 protocol 字段。这条用例特意不用
    `inverter` 泛型 fixture,因为它测的不是"逆变器的通用规则",
    而是"网关这条链路有没有把东西正确传过去"这件专属的事。"""
    inverter_02_via_ipc.connect()
    power = inverter_02_via_ipc.read("有功功率")
    assert 0 <= power <= inverter_02_via_ipc.device.rated_power_w * 1.1
