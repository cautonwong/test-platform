"""
逆变器的驱动:Inverter 聚合根 + 型号点表 + vendor_ascii 的读取器,没有别的。

读数和电表走的是同一条通用读取路径(drivers/point_table_driver.py):

    读数键("有功功率") → 点表项(应答行的第几个字段、最长几个字符) → 文本行
    → 解码成物理量 → Inverter.record_reading()

这条链是 read_all() 的(一次可判的读数);单读 inverter.read("有功功率") 只取值、不记读数。

所以这个文件里没有 read_output_power 这种一设备一方法:读哪些量、字段在第几个、
怎么编码,全在型号的点表里;这里只声明「点表读数键 ↔ Inverter 参数名」的键名对照。
要读的量在点表里写全了,判据集才拿得到量——PointReadings 会为对不上的点表直接报错。
"""
from __future__ import annotations

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.inverter import Inverter
from testplatform.transport.interfaces import ByteStreamTransport

from .point_table_driver import PointTableDriver
from .registry import declare_judged_quantities, register_device
from .vendor_ascii import PointReadings, vendor_ascii_reader

INVERTER_QUANTITIES = {
    "有功功率": "output_power_w",
    "电压": "grid_voltage_v",
    "频率": "grid_freq_hz",
}


declare_judged_quantities("inverter", "vendor_ascii", INVERTER_QUANTITIES)


@register_device("inverter", "vendor_ascii")
def _build_inverter(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    inverter = Inverter(
        device_id=device_id,
        name=spec.get("name", device_id),
        rated_power_w=spec["rated_power_w"],
        criteria=criteria,
    )
    return PointTableDriver(
        PointReadings(inverter, INVERTER_QUANTITIES),
        model,
        transport,
        vendor_ascii_reader(transport, b"READ_POWER\n"),
    )
