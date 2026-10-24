"""
Inverter —— 逆变器聚合根。

在 Device 保护的"连接生命周期"之上,额外保护逆变器特有的不变量:
- 额定功率必须为正
- 历史读数只能通过 record_reading() 追加,外部拿到 history 后无法反向修改
  (PowerReading 是 frozen dataclass,history 属性返回的是 tuple 拷贝)

读数本身一律被记录:频率 60Hz、功率超额定都只标记在这条读数上,不抛错——
平台的职责是判设备不合格,不是自己崩掉、把"实测 60Hz"这份证据丢掉。
合格与否由注入的 CriteriaProfile 判(domain/criteria.py),阈值不写死在这里。

任何协议细节(发什么指令、怎么解析字节)都不应该出现在这个文件里——
那是 drivers/inverter_driver.py 的职责,这个类只认识"物理量",不认识字节。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from .criteria import Breach, CriteriaProfile, Severity
from .device import Device
from .events import HighPowerWarning
from .exceptions import InvariantViolation


@dataclass(frozen=True)
class PowerReading:
    """值对象:一次功率测量记录,不可变,没有独立标识。"""

    timestamp: datetime
    output_power_w: float
    grid_voltage_v: float
    grid_freq_hz: float
    breaches: Tuple[Breach, ...] = ()

    @property
    def passed(self) -> bool:
        """合格结论:没有任何一条 REJECT 级判据被越过。"""
        return not any(b.severity is Severity.REJECT for b in self.breaches)


class Inverter(Device):
    def __init__(
        self, device_id: str, name: str, rated_power_w: float, criteria: CriteriaProfile
    ) -> None:
        super().__init__(device_id, name)
        if rated_power_w <= 0:
            raise InvariantViolation(f"额定功率不合理: {rated_power_w}")
        self.rated_power_w = rated_power_w
        self.criteria = criteria

    def record_reading(
        self, output_power_w: float, grid_voltage_v: float, grid_freq_hz: float
    ) -> PowerReading:
        self.ensure_connected()

        evaluation = self.criteria.evaluate(
            {
                "output_power_w": output_power_w,
                "grid_voltage_v": grid_voltage_v,
                "grid_freq_hz": grid_freq_hz,
            }
        )
        reading = PowerReading(
            timestamp=datetime.now(timezone.utc),
            output_power_w=output_power_w,
            grid_voltage_v=grid_voltage_v,
            grid_freq_hz=grid_freq_hz,
            breaches=evaluation.breaches,
        )
        self._record(reading)

        # 软信号:判据集里 WARN 级的功率判据被越过(默认是额定的 90%),
        # 不值得判不合格,但外面应该知道。
        for breach in evaluation.warnings:
            if breach.quantity == "output_power_w":
                self._raise_event(
                    HighPowerWarning(
                        device_id=self.id,
                        output_power_w=breach.value,
                        rated_power_w=self.rated_power_w,
                    )
                )
        return reading

    @property
    def latest_reading(self) -> Optional[PowerReading]:
        return self._history[-1] if self._history else None
