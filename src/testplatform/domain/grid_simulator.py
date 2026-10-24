"""
GridSimulator —— 电网模拟器聚合根。清单里它是 `role: support` 的周边设备。

周围设备不判合格:它没有"合格"这个概念,它只是为被测设备提供可设定的电网。
它挂掉时结论是"测试无效"(测试层的 skip),不是被测设备不合格。
不变量因此只剩三条:未连接不能设定;电压/电流非负、频率为正;接线方式是整数编码。

和 Inverter / Battery 一样,这个文件完全不认识协议细节,只认识物理量。
台架量的名字沿用上游条款的写法(Ua / Ia / θa / QUaUb / Fre / 接线方式),
与型号点表的读数键是同一套名字 —— 条款里的 Ua 和 Ub 是两件事,合成一个「电压」就丢了信息。
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from .device import Device
from .exceptions import InvariantViolation

# 三相台架能设定的量:上游条款「调整台体」块里的核心量名。
# 不在这个集合里的键当场报错 —— 拼错一个量名(θa 写成 ta)就会静默设了个没人读的东西。
THREE_PHASE_QUANTITIES = (
    "Ua",
    "Ub",
    "Uc",
    "Ia",
    "Ib",
    "Ic",
    "θa",
    "θb",
    "θc",
    "QUaUb",
    "QUaUc",
    "Fre",
    "接线方式",
    "MeterConst",
)
# 有正负方向的只有相角;电压 / 电流 / 频率 / 接线方式 / 表常数都是非负(频率为正)。
SIGNED_QUANTITIES = frozenset({"θa", "θb", "θc", "QUaUb", "QUaUc"})
NON_NEGATIVE_QUANTITIES = frozenset(THREE_PHASE_QUANTITIES) - SIGNED_QUANTITIES - {"Fre"}
INTEGER_QUANTITIES = frozenset({"接线方式", "MeterConst"})


@dataclass(frozen=True)
class GridSetpoint:
    """值对象:电网模拟器当前输出给被测设备的电压与频率。"""

    voltage_v: float
    freq_hz: float


@dataclass(frozen=True)
class ThreePhaseSetpoint:
    """值对象:一次三相台架设定。键是量名,值是按量给的数(接线方式/表常数是整数)。"""

    values: Mapping[str, float]


class GridSimulator(Device):
    def __init__(self, device_id: str, name: str) -> None:
        super().__init__(device_id, name)
        self._setpoint: Optional[GridSetpoint] = None
        self._three_phase: Optional[ThreePhaseSetpoint] = None

    def apply_setpoint(self, voltage_v: float, freq_hz: float) -> GridSetpoint:
        self.ensure_connected()
        if voltage_v < 0:
            raise InvariantViolation(f"电网电压不能为负: {voltage_v}")
        if freq_hz <= 0:
            raise InvariantViolation(f"电网频率必须为正: {freq_hz}")
        self._setpoint = GridSetpoint(voltage_v=voltage_v, freq_hz=freq_hz)
        return self._setpoint

    def apply_three_phase(self, values: Mapping[str, float]) -> ThreePhaseSetpoint:
        """设定一块三相台架(Ua/Ub/Uc/Ia/…/接线方式),逐量校验后**合并**到已有设定上。

        一次设定只带条款里写到的那些量(条款常常只写 `Ua=Un / Ia=0 / Fre=Hz`),没写的量台架保持原样。
        条款的前置天然是多段写的(先接线方式、再三相电压、再电流、再相角),所以这里必须合并而不是替换 ——
        替换会让"前置分四段设好"变成"只剩最后一段",而且报告里看不出来。同名量以新值为准。
        """
        self.ensure_connected()
        checked = {name: _check_quantity(name, value) for name, value in values.items()}
        merged = {**(self._three_phase.values if self._three_phase else {}), **checked}
        self._three_phase = ThreePhaseSetpoint(values=MappingProxyType(merged))
        return self._three_phase

    @property
    def setpoint(self) -> Optional[GridSetpoint]:
        return self._setpoint

    @property
    def three_phase(self) -> Optional[ThreePhaseSetpoint]:
        return self._three_phase


def _check_quantity(name: str, value: float) -> float:
    if name not in THREE_PHASE_QUANTITIES:
        signed = sorted(SIGNED_QUANTITIES)
        raise InvariantViolation(
            f"台架没有量 {name!r};可用的量: {list(THREE_PHASE_QUANTITIES)}"
            f"(相角类 {signed} 可以带负号)"
        )
    if name in INTEGER_QUANTITIES and float(value) != int(value):
        raise InvariantViolation(
            f"台架量 {name!r} 是整数编码,不能设成 {value}:"
            f"3P4W / 3P3W 这类接线方式是枚举,不是模拟量"
        )
    if name == "Fre" and value <= 0:
        raise InvariantViolation(f"电网频率必须为正: {value}")
    if name in NON_NEGATIVE_QUANTITIES and value < 0:
        raise InvariantViolation(f"台架量 {name!r} 不能为负: {value}")
    return float(value)
