"""
电池的驱动:和 inverter_driver.py 完全对称 —— 同一个通用读取路径,同一份键名对照的写法,
只有聚合根、点表(在 devices.yaml 里)和读指令不同。

    battery.read_all() → 点表项 → 文本行 → 解码 → Battery.record_reading()

没有 read_soc 这种一设备一方法。单读 battery.read("SOC") 只取值、不记读数(不是一次测量)。
"""
from __future__ import annotations

from testplatform.config.manifest import Model
from testplatform.domain.battery import Battery
from testplatform.domain.criteria import CriteriaProfile
from testplatform.transport.interfaces import ByteStreamTransport

from .point_table_driver import PointTableDriver
from .registry import declare_judged_quantities, register_device
from .vendor_ascii import PointReadings, vendor_ascii_reader

BATTERY_QUANTITIES = {
    "SOC": "soc_percent",
    "电压": "voltage_v",
    "电流": "current_a",
}


declare_judged_quantities("battery", "vendor_ascii", BATTERY_QUANTITIES)


@register_device("battery", "vendor_ascii")
def _build_battery(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    battery = Battery(
        device_id=device_id,
        name=spec.get("name", device_id),
        capacity_ah=spec["capacity_ah"],
        criteria=criteria,
    )
    return PointTableDriver(
        PointReadings(battery, BATTERY_QUANTITIES),
        model,
        transport,
        vendor_ascii_reader(transport, b"READ_SOC\n"),
    )
