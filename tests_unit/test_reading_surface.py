"""read_all() 的统一属性面:readings / passed / breaches。

电表、逆变器、电池的读数都带判据结论;终端没有判据,访问 passed 必须给明确错误,
而不是 AttributeError 或一个假的 None。
"""
from dataclasses import dataclass

import pytest

from testplatform.domain.exceptions import InvariantViolation, NoVerdictError
from testplatform.domain.terminal import Terminal
from testplatform.drivers.vendor_ascii import PointReadings, RecordedReadings


@dataclass(frozen=True)
class _Verdict:
    passed: bool
    breaches: tuple = ()


def test_recorded_readings_exposes_the_domain_verdict():
    reading = RecordedReadings({"电压": 230.0}, verdict=_Verdict(True))

    assert reading.passed is True
    assert reading.breaches == ()


def test_recorded_readings_without_a_verdict_says_so():
    reading = RecordedReadings({"电压": 230.0})

    with pytest.raises(InvariantViolation, match="判据"):
        reading.passed
    with pytest.raises(InvariantViolation, match="判据"):
        reading.breaches


def test_point_readings_carries_the_aggregate_verdict_back():
    class Aggregate:
        latest_reading = None

        def record_reading(self, **quantities):
            assert quantities == {"voltage_v": 230.0}
            self.latest_reading = _Verdict(True)
            return self.latest_reading

    aggregate = Aggregate()
    reading = PointReadings(aggregate, {"电压": "voltage_v"}).record_reading({"电压": 230.0})

    assert reading.readings == {"电压": 230.0}
    assert reading.passed is True


def test_terminal_reading_has_no_verdict():
    terminal = Terminal("t", "终端")
    terminal.mark_connected()

    reading = terminal.record_reading({"上电运行时间": 86400})

    assert reading.readings == {"上电运行时间": 86400}
    # 没有 breaches 属性:证据层据此知道"这台设备不判合格",而不是拿到一份空违约列表。
    assert not hasattr(reading, "breaches")
    with pytest.raises(NoVerdictError, match="终端"):
        reading.passed
