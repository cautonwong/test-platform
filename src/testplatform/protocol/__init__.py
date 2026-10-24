"""
协议编解码层:帧格式、校验、字节序、编码解码。

这一层是纯函数式的:不 import transport(不碰字节管道)、不 import domain(不认识聚合根),
也不认识点表 —— 输入原始字节或寄存器地址,输出物理量或帧。所以它能脱离硬件单测,
不需要设备、不需要模拟器、毫秒级跑完。

依赖方向:transport → protocol → 点表 → drivers → domain。
"""
from __future__ import annotations

from . import dlt645, modbus_rtu, modbus_tcp
from .crc import crc16, crc16_modbus
from .values import decode_value, encode_value

__all__ = [
    "crc16",
    "crc16_modbus",
    "decode_value",
    "encode_value",
    "dlt645",
    "modbus_rtu",
    "modbus_tcp",
]
