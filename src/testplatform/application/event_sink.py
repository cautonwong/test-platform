"""
EventSink —— 消费者定义接口的又一个实例,只是这次"消费者"换成了
关心这些领域事件的一侧(通常是报告/监控/告警)。

谁需要事件,谁就按自己的需要声明"我希望收到什么形状的东西"——
具体是在内存里攒一个列表、写进日志、推给 Allure,还是推一条钉钉告警,
都是实现方(reporting/event_sinks.py)的自由,domain 层完全不关心、
也不 import 这个 Protocol。
"""
from __future__ import annotations

from typing import Protocol

from testplatform.domain.events import DomainEvent


class EventSink(Protocol):
    def emit(self, event: DomainEvent) -> None: ...
