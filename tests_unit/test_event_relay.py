"""
事件搬运的守卫(票 13a):sink 抛错时,**丢了几条、丢的是什么**必须能数出来。

复现(修前):5 条事件,sink 在第 2 条抛错 → `pull_events()` 已把设备队列清空,
sink 只收到 1 条,**另外 4 条永久消失**,现场只看到一句异常。
"""

from __future__ import annotations

import pytest

from testplatform.application.event_relay import EventEmitFailed, drain_device_events


class _Device:
    def __init__(self, device_id: str, events: list) -> None:
        self.id = device_id
        self._events = list(events)

    def pull_events(self) -> list:
        events, self._events = self._events, []
        return events


class _Driver:
    def __init__(self, device_id: str, events: list) -> None:
        self.device = _Device(device_id, events)


class _Sink:
    def __init__(self, fail_at: int | None = None) -> None:
        self.got: list = []
        self._fail_at = fail_at
        self._calls = 0

    def emit(self, event) -> None:
        self._calls += 1
        if self._fail_at is not None and self._calls == self._fail_at:
            raise RuntimeError("sink 坏了")
        self.got.append(event)


def test_a_working_sink_gets_every_event():
    sink = _Sink()

    drained = drain_device_events([_Driver("dev_01", ["ev0", "ev1"])], sink)

    assert drained == ["ev0", "ev1"] and sink.got == ["ev0", "ev1"]


def test_an_emit_failure_is_countable_and_names_the_lost_events():
    """sink 在第 2 条抛错:4 条没送到,消息里要能数出 4、指出哪台设备。"""
    sink = _Sink(fail_at=2)
    driver = _Driver("dev_01", [f"ev{i}" for i in range(5)])

    with pytest.raises(EventEmitFailed) as error:
        drain_device_events([driver], sink)

    lost = [event for _device, event, _cause in error.value.failures]
    assert len(lost) == 4, f"丢了 {len(lost)} 条,应该 4 条: {lost}"
    assert lost == ["ev1", "ev2", "ev3", "ev4"], lost
    assert error.value.delivered == ["ev0"]
    assert "4 条 event 没送到 sink" in str(error.value)
    assert "dev_01" in str(error.value)
    # 设备队列确实被清空了 —— 所以"不吞"是唯一能保住这些事件信息的办法
    assert driver.device._events == []


def test_the_failure_stops_at_the_first_bad_event_and_keeps_the_rest_counted():
    """第一台设备就炸:它后面的事件也要算进"丢掉",不能只报第一条。"""
    sink = _Sink(fail_at=1)

    with pytest.raises(EventEmitFailed) as error:
        drain_device_events(
            [_Driver("dev_01", ["a0", "a1"]), _Driver("dev_02", ["b0"])], sink
        )

    # 抛在第一台设备的第 1 条:它这一队整队都算丢;第二台设备**还没轮到**,不算丢
    assert [event for _device, event, _cause in error.value.failures] == ["a0", "a1"]
    # 一台设备炸了不连累别的设备:dev_02 的那条照常送到
    assert error.value.delivered == ["b0"]
    assert {device for device, _event, _cause in error.value.failures} == {"dev_01"}


def test_no_failure_means_no_exception_even_with_no_events():
    assert drain_device_events([], _Sink()) == []
    assert drain_device_events([_Driver("dev_01", [])], _Sink()) == []
