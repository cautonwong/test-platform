"""
TerminalDriver —— 采集终端:读终端自己的量,而不是把自己当电表读。

终端上行规约也是 DL/T645,所以和电表共用同一条点表驱动的通用读取路径
(PointTableDriver)和同一份 645 帧读写(drivers/meter_driver.py 的 dlt645_reader);
区别只在型号:终端型号有自己的 capabilities 与 point_table,点表项是终端自己的数据标识。
读数键不经电表型号,终端因此不再"冒充电表被测"。

注册键是 (设备类型, 规约):("terminal", "dlt645")。
下游电表用 `via:` 挂在终端下面,那是组合根的事,这个文件不认识 via、不认识拓扑。
"""
from __future__ import annotations

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.terminal import Terminal
from testplatform.transport.interfaces import ByteStreamTransport

from .meter_driver import dlt645_reader
from .point_table_driver import PointTableDriver
from .registry import register_device


@register_device("terminal", "dlt645")
def _build_terminal(
    device_id: str, spec: dict, model: Model, transport: ByteStreamTransport, criteria: CriteriaProfile
) -> PointTableDriver:
    """criteria 收下但不用:终端没有上游确认的验收判据,不判合格(与周边设备同一个工厂签名)。"""
    terminal = Terminal(device_id=device_id, name=spec.get("name", device_id))
    return PointTableDriver(
        terminal, model, transport, dlt645_reader(transport, spec["bus_address"])
    )
