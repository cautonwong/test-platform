"""
验证"事件向上"这条机制在 domain 层本身是怎么工作的——完全不需要
transport/composition_root,因为事件的产生只和聚合根自己的业务规则有关,
这也是为什么这个文件放在 tests_unit/ 而不是 tests/。
"""
from testplatform.domain.battery import Battery
from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity
from testplatform.domain.events import DeviceFaulted, HighPowerWarning, LowSocWarning
from testplatform.domain.inverter import Inverter


def _inverter(rated_power_w: float) -> Inverter:
    """软告警线(额定的 90%)来自判据集,不再写死在聚合根里。"""
    criteria = CriteriaProfile(
        name="test",
        criteria=(
            Criterion("grid_freq_hz", low=45, high=55),
            Criterion("grid_voltage_v", low=0, low_exclusive=True),
            Criterion("output_power_w", low=0, high=rated_power_w * 1.1),
            Criterion("output_power_w", high=rated_power_w * 0.9, severity=Severity.WARN),
        ),
    )
    return Inverter(device_id="inv_t", name="t", rated_power_w=rated_power_w, criteria=criteria)


def _battery() -> Battery:
    criteria = CriteriaProfile(
        name="test",
        criteria=(
            Criterion("voltage_v", low=0, low_exclusive=True),
            Criterion("soc_percent", low=10, severity=Severity.WARN),
        ),
    )
    return Battery(device_id="bat_t", name="t", capacity_ah=50, criteria=criteria)


def test_inverter_emits_high_power_warning_without_failing_the_call():
    inv = _inverter(1000)
    inv.mark_connected()

    # 950W / 1000W 额定 = 95%,超过 90% 的软告警线,但远没到 110% 的硬拒绝线
    inv.record_reading(output_power_w=950, grid_voltage_v=230, grid_freq_hz=50)

    events = inv.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], HighPowerWarning)
    assert events[0].device_id == "inv_t"
    assert events[0].output_power_w == 950


def test_pull_events_drains_the_queue():
    """取走一次之后队列必须清空,同一个事件不能被重复上报两次。"""
    inv = _inverter(1000)
    inv.mark_connected()
    inv.record_reading(950, 230, 50)

    first = inv.pull_events()
    second = inv.pull_events()

    assert len(first) == 1
    assert second == []


def test_normal_power_reading_emits_no_event():
    """低于 90% 额定的正常读数不应该产生任何事件——
    "事件向上"只用来报告"值得关注的事",不是把每次操作都变成一条事件。"""
    inv = _inverter(1000)
    inv.mark_connected()
    inv.record_reading(500, 230, 50)

    assert inv.pull_events() == []


def test_battery_emits_low_soc_warning():
    bat = _battery()
    bat.mark_connected()

    bat.record_reading(soc_percent=5, voltage_v=48, current_a=-2)

    events = bat.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], LowSocWarning)
    assert events[0].soc_percent == 5


def test_device_faulted_event_on_mark_fault():
    inv = _inverter(1000)
    inv.mark_connected()

    inv.mark_fault("过温保护触发")

    events = inv.pull_events()
    assert any(
        isinstance(e, DeviceFaulted) and e.reason == "过温保护触发" for e in events
    )
