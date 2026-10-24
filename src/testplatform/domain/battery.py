"""
Battery —— 电池聚合根。

保护 SOC(荷电状态)必须落在 [0, 100] 这条核心不变量,以及额定容量必须为正。
和 Inverter 一样,这个文件完全不认识任何协议细节,只认识物理量。

SOC 低、电压为负都只标记在读数上,不抛错;合格与否由注入的
CriteriaProfile 判(domain/criteria.py),阈值不写死在这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple

from .criteria import Breach, CriteriaProfile, Severity
from .device import Device
from .events import LowSocWarning
from .exceptions import InvariantViolation


@dataclass(frozen=True)
class SocReading:
    timestamp: datetime
    soc_percent: float
    voltage_v: float
    current_a: float  # 正值表示充电,负值表示放电(约定由业务层统一)
    breaches: Tuple[Breach, ...] = ()

    @property
    def passed(self) -> bool:
        """合格结论:没有任何一条 REJECT 级判据被越过。"""
        return not any(b.severity is Severity.REJECT for b in self.breaches)


class Battery(Device):
    def __init__(
        self, device_id: str, name: str, capacity_ah: float, criteria: CriteriaProfile
    ) -> None:
        super().__init__(device_id, name)
        if capacity_ah <= 0:
            raise InvariantViolation(f"电池容量不合理: {capacity_ah}")
        self.capacity_ah = capacity_ah
        self.criteria = criteria

    def record_reading(
        self, soc_percent: float, voltage_v: float, current_a: float
    ) -> SocReading:
        self.ensure_connected()

        # SOC 越出 [0, 100] 是平台自身该发现的不变量违反,不是判据问题。
        if not (0.0 <= soc_percent <= 100.0):
            raise InvariantViolation(f"SOC {soc_percent}% 超出 [0, 100] 范围")

        evaluation = self.criteria.evaluate(
            {"soc_percent": soc_percent, "voltage_v": voltage_v}
        )
        reading = SocReading(
            timestamp=datetime.now(timezone.utc),
            soc_percent=soc_percent,
            voltage_v=voltage_v,
            current_a=current_a,
            breaches=evaluation.breaches,
        )
        self._record(reading)

        for breach in evaluation.warnings:
            if breach.quantity == "soc_percent":
                self._raise_event(LowSocWarning(device_id=self.id, soc_percent=breach.value))
        return reading

    @property
    def is_low_soc(self) -> bool:
        latest = self._history[-1] if self._history else None
        return latest is not None and any(b.quantity == "soc_percent" for b in latest.breaches)
