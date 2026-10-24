"""
vendor_ascii 规约的接入:应答是一行文本,点表项只描述"这一行里的第几个字段、最长多少字符"。

这个文件就是接入第二个规约的全部成本,point_table_driver.py 一行都不用改:

- vendor_ascii_reader:一个点表项 = 发一次读指令,address 是应答行里按空白分隔的字段序号。
- PointReadings:Inverter / Battery 的 record_reading 收的是几个具名参数,而通用读取路径
  交过来的是「读数键 → 物理量」映射,这里把键名对上再转调一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from testplatform.config.manifest import Point
from testplatform.domain.exceptions import InvariantViolation
from testplatform.transport.interfaces import ByteStreamTransport

from .point_table_driver import ReadRaw


def vendor_ascii_reader(transport: ByteStreamTransport, command: bytes) -> ReadRaw:
    """读指令本身是设备厂家的,所以由调用方给;这个 reader 只认"应答行里的字段序号"。"""

    def read_raw(point: Point) -> bytes:
        transport.send(command)
        # 应答是一行文本,长度事先不知道:走 transport 的"长度未知"路径(不按 256 截断)。
        return transport.receive(timeout=2.0).split()[point.address]

    return read_raw


@dataclass(frozen=True)
class RecordedReadings:
    """一次读数的全部物理量,键就是点表的读数键(通用读取路径的 read_all 按它取数)。"""

    readings: Mapping[str, float]


class PointReadings:
    """把「读数键 → 物理量」映射翻成一次聚合根的领域方法调用。

    quantities 只有键名对应关系(点表读数键 → record_reading 的参数名),不做任何换算:
    字节怎么变成物理量全在点表项里。点表的键和聚合根要的量必须一一对上——
    少一个会让判据在缺量的映射上判"合格",多一个会让这个读数被悄悄丢掉,所以这里直接报错。
    """

    def __init__(self, device: Any, quantities: Mapping[str, str]) -> None:
        self._device = device
        self._quantities = dict(quantities)

    @property
    def quantities(self) -> Mapping[str, str]:
        """读数键 → 聚合根参数名。record_reading 用的就是这张表,装配期的"这个量判得到吗"
        也从它派生(见 composition/root.py::_validate_quantities_are_reachable)。"""
        return self._quantities

    def mark_connected(self) -> None:
        self._device.mark_connected()

    def mark_disconnected(self) -> None:
        self._device.mark_disconnected()

    def record_reading(self, readings: Mapping[str, float]) -> RecordedReadings:
        if set(readings) != set(self._quantities):
            raise InvariantViolation(
                f"{self._device.id}: 点表读数键 {sorted(readings)} 与聚合根要的量 "
                f"{sorted(self._quantities)} 不一致"
            )
        self._device.record_reading(
            **{arg: readings[key] for key, arg in self._quantities.items()}
        )
        return RecordedReadings(dict(readings))

    def __getattr__(self, name: str) -> Any:
        """state / history / rated_power_w 这些只读查询原样落到聚合根上,不再逐个转发。"""
        return getattr(self._device, name)
