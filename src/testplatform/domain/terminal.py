"""
Terminal —— 采集终端聚合根。它既是被测设备,也是下游电表接入的那条总线。

终端自身的不变量:无。这里只沿用 Device 保护的连接生命周期(未连接不能读数),
不为"看起来完整"再编一条终端特有的规则。

终端也不判合格:上游没有终端自身的验收判据,拿电表的判据判终端就是假结论,
所以读数只被记录,不带 breaches / passed。读哪些量同样由型号点表决定,
点表里是终端自己的数据标识,不是电表的。

协议细节(645 帧怎么拼)不在这个文件里,那是 drivers/terminal_driver.py 的事。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping

from .device import Device


@dataclass(frozen=True)
class TerminalReading:
    """值对象:终端自己的一次读数。readings 的键就是终端型号点表里的读数键。"""

    timestamp: datetime
    readings: Dict[str, float]


class Terminal(Device):
    def __init__(self, device_id: str, name: str) -> None:
        super().__init__(device_id, name)

    def record_reading(self, readings: Mapping[str, float]) -> TerminalReading:
        self.ensure_connected()
        reading = TerminalReading(
            timestamp=datetime.now(timezone.utc), readings=dict(readings)
        )
        self._record(reading)
        return reading
