"""
EventSink 的两个最小实现。注意这两个类都没有继承
application.event_sink.EventSink——鸭子类型即可,这和之前
LoanRepository / OrderRepository 的 Fake 实现是同一个套路:
实现方完全不知道、也不需要知道被谁按什么接口消费。
"""
from __future__ import annotations

from typing import List

from testplatform.domain.events import DomainEvent


class InMemoryEventSink:
    """最简单的实现:攒在内存列表里,方便测试断言、方便 pytest 插件
    在会话结束时统一读出来打印。"""

    def __init__(self) -> None:
        self.events: List[DomainEvent] = []

    def emit(self, event: DomainEvent) -> None:
        self.events.append(event)


class LoggingEventSink:
    """生产环境可以换成这个,或者再写一个推给 Allure/钉钉的实现——
    domain 层、application 层的代码一行都不用改。"""

    def emit(self, event: DomainEvent) -> None:
        print(f"[device-event] {event}")
