"""
Meter —— 电表聚合根。

电表和逆变器/电池的区别只有一处:它要读的量不是固定的三四个字段,而是由**点表**决定的一组
读数键(有功电量、电压……)。所以 record_reading 收的是一个「读数键 → 物理量」的映射,
原样记下来,再交给注入的 CriteriaProfile 判合格与否。型号差异(读哪些量、地址在哪)
只出现在点表里,这个类不为任何型号分叉。

读数一律被记录:电压 250V、电量为负都只标记在这条读数上,不抛错——
平台的职责是判设备不合格,不是自己崩掉、把"实测 250V"这份证据丢掉。
任何协议细节(功能码、CRC、字节序)都不在这个文件里,那是 protocol/ 和 drivers/ 的事。

不变量只剩两条:未连接不能读数(base 的 ensure_connected),以及一次读数不能是空的
(空映射说明通用读取路径自己写错了,那是平台 bug,不是设备不合格)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Tuple

from .criteria import Breach, CriteriaProfile, Severity
from .device import Device
from .exceptions import InvariantViolation


@dataclass(frozen=True)
class MeterReading:
    """值对象:一次读数记录。readings 的键就是点表里的读数键。"""

    timestamp: datetime
    readings: Dict[str, float]
    breaches: Tuple[Breach, ...] = ()

    @property
    def passed(self) -> bool:
        """合格结论:没有任何一条 REJECT 级判据被越过。"""
        return not any(b.severity is Severity.REJECT for b in self.breaches)


class Meter(Device):
    def __init__(self, device_id: str, name: str, criteria: CriteriaProfile) -> None:
        super().__init__(device_id, name)
        self.criteria = criteria

    def record_reading(self, readings: Mapping[str, float]) -> MeterReading:
        self.ensure_connected()
        if not readings:
            raise InvariantViolation(f"{self.id} 这次读数一个量都没有,通用读取路径不该传空映射")

        evaluation = self.criteria.evaluate(readings)
        reading = MeterReading(
            timestamp=datetime.now(timezone.utc),
            readings=dict(readings),
            breaches=evaluation.breaches,
        )
        self._record(reading)
        return reading

    @property
    def latest_reading(self) -> MeterReading | None:
        return self._history[-1] if self._history else None
