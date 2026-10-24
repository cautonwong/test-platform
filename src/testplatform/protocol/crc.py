"""
CRC-16:一个参数化的反射式实现,默认参数就是标准 Modbus。

上游 legacy 平台的 `common/checkCRC.py` 里出现过的变体在这里只是"换参数":
- `_CRC16_IBM`:多项式 0x8005、初值 0x0000、最终异或 0x0000 —— **不是**标准 Modbus 参数
- `_CRC16_X25`:多项式 0x1021
它们只用来当编解码的单测向量。真实 Modbus 设备按 `crc16_modbus()` 校验,
初值差 0xFFFF 这一个字节,所有真实设备都会回"校验失败"。
"""
from __future__ import annotations

# 标准 Modbus:多项式 0x8005(反射算)、初值 0xFFFF、无最终异或。
MODBUS_POLYNOMIAL = 0x8005
MODBUS_INIT = 0xFFFF
MODBUS_XOROUT = 0x0000


def crc16(
    data: bytes,
    polynomial: int = MODBUS_POLYNOMIAL,
    init: int = MODBUS_INIT,
    xorout: int = MODBUS_XOROUT,
) -> int:
    """反射式(低位先算)CRC-16。polynomial 传正常形式,函数内部反射(0x8005 → 0xA001)。"""
    reflected = _reflect16(polynomial)
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ reflected if crc & 1 else crc >> 1
    return crc ^ xorout


def crc16_modbus(data: bytes) -> int:
    """标准 Modbus CRC-16。上线时要低字节先发,见 protocol/modbus_rtu.py。"""
    return crc16(data)


def _reflect16(value: int) -> int:
    result = 0
    for _ in range(16):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result
