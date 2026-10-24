"""
把"事件向上"这件事,从"聚合根产生事件"搬运到"消费方真正收到事件"的最后一棒。

domain 层只负责生产事件(放进自己的队列),这里负责把它们从每个聚合根的
队列里取出来,交给调用方提供的 EventSink。什么时候调用这个函数
(每条用例结束后?每次读数之后?)完全是调用方自己决定的时机问题——
这个函数本身不关心"谁在什么时候调用我",它只管搬运。
"""
from __future__ import annotations

from typing import Any, Iterable

from .event_sink import EventSink


class EventEmitFailed(RuntimeError):
    """有事件没送到 sink:搬运本身失败,**丢掉的那几条要能数出来**。

    为什么不是"忽略掉继续搬":事件是归因的输入(设备进故障态、链路断开都在这里),
    少一条就等于结论少一块依据,而现场只会看到"什么都没发生"。所以这里把
    "哪台设备、哪几条事件、什么原因"一起带走,让调用方的报告能写"丢了 N 条 event"。
    """

    def count_of(self, device_id: str) -> int:
        """这台设备丢了几条(报告里写条数,不是布尔)。"""
        return sum(1 for device, _event, _cause in self.failures if device == device_id)

    def __init__(self, failures: Iterable[tuple], delivered: Iterable[Any]) -> None:
        self.failures = list(failures)  # [(device_id, event, error), ...]
        self.delivered = list(delivered)
        devices = sorted({device_id for device_id, _event, _error in self.failures})
        first_error = self.failures[0][2] if self.failures else None
        super().__init__(
            f"有 {len(self.failures)} 条 event 没送到 sink(设备 {devices},"
            f"已送达 {len(self.delivered)} 条):{first_error}"
        )


def drain_device_events(drivers: Iterable[Any], sink: EventSink) -> list:
    """把每个聚合根队列里的事件搬到 sink,并把搬走的事件交回给调用方。

    交回是给证据层用的:事件被 pull 一次就清空,证据不能再 pull 第二次,
    所以由同一次搬运把结果同时交给 sink 和证据。

    **emit 失败不吞**:pull 已经清空了设备队列,这里一抛,那些事件就再没人能取到 ——
    所以把失败的那条**和它后面还没搬的**一并记进 `EventEmitFailed`(可数、可指认)。
    """
    drained = []
    failures: list[tuple] = []
    for driver in drivers:
        events = driver.device.pull_events()
        for index, event in enumerate(events):
            try:
                sink.emit(event)
            except Exception as exc:  # noqa: BLE001 - 任何 sink 失败都算"这条没落盘"
                failures.extend(
                    (driver.device.id, lost, exc) for lost in events[index:]
                )
                break
            drained.append(event)
    if failures:
        raise EventEmitFailed(failures, drained)
    return drained
