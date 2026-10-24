"""
领域事件 —— "事件向上"在 domain 层的落地。

聚合根完成一次状态变更后,如果这件事"值得被外面知道"(功率接近额定上限、
SOC 过低、设备进入故障态……),不会直接调用任何上层代码——那样会让
domain 反过来 import application/infrastructure,彻底打乱依赖方向。
它只会把这件事实"记下来",放进自己内部的待发队列,由外部在合适的时机
主动调用 pull_events() 取走。

这是"事件向上"真正的含义:子层只管诚实地描述"发生了什么",
不知道、也不关心谁会处理、处理逻辑是什么、甚至会不会有人来取。

同时这里也是"消费者定义接口"的一次变形:事件的形状由产生事件的一方
(聚合根)按自己的业务语义来定义,不是由"谁来消费事件"那一侧反过来
规定"你必须按我的格式上报"——只是这次"定义形状的一方"和"实现消费逻辑
的一方"的角色,和 transport 那边的 Protocol 刚好反过来了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DomainEvent:
    device_id: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class DeviceFaulted(DomainEvent):
    reason: str = ""


@dataclass(frozen=True)
class HighPowerWarning(DomainEvent):
    """软信号:功率接近额定上限,但还没到需要拒绝这次读数的程度。
    和 InvariantViolation 的区别:这不是"数据非法",只是"值得关注"。"""

    output_power_w: float = 0.0
    rated_power_w: float = 0.0


@dataclass(frozen=True)
class LowSocWarning(DomainEvent):
    soc_percent: float = 0.0
