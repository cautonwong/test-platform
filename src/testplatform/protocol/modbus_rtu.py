"""
Modbus RTU 的帧格式:请求帧与应答帧的拼装与解析,含 CRC 校验。

帧结构(字节数):
    请求  从站地址 1 | 功能码 1 | 起始寄存器 2 | 寄存器数 2 | CRC 2(低字节先)
    应答  从站地址 1 | 功能码 1 | 字节数 1 | 寄存器数据 N | CRC 2(低字节先)
    写请求  从站地址 1 | 功能码 1 | 寄存器地址 2 | 值 2 | CRC 2(低字节先)
    写应答  与写请求逐字节相同(回显地址与值,不回数据)
    多写请求 从站地址 1 | 功能码 1 | 起始寄存器 2 | 寄存器数 2 | 字节数 1 | 寄存器数据 N | CRC 2
    多写应答 从站地址 1 | 功能码 1 | 起始寄存器 2 | 寄存器数 2 | CRC 2(回显地址与个数,没有数据段)
    异常应答  从站地址 1 | 功能码|0x80 1 | 异常码 1 | CRC 2

纯函数:不 import transport、不 import domain,也不认识点表 —— 只认识寄存器地址与寄存器个数,
"某个读数键对应哪几个寄存器"是点表的事。收发字节由驱动层调用这里的结果去完成。
"""
from __future__ import annotations

from typing import Tuple

from .crc import crc16_modbus

READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
# 写单个寄存器:请求和应答都只有 8 字节,应答是请求帧原样回显(地址 + 值),不回数据。
WRITE_SINGLE_REGISTER = 0x06
# 写多个寄存器:请求带寄存器数与数据,应答只回显起始地址与寄存器数(同样 8 字节,没有数据段)。
WRITE_MULTIPLE_REGISTERS = 0x10
WRITE_SINGLE_REGISTER_BYTES = 8
# 0x10 一次最多 123 个寄存器(0x7B):协议 PDU 上限 253 字节,扣掉头尾剩下的就是这个数。
WRITE_MULTIPLE_MAX_REGISTERS = 0x7B
# 异常应答里那个字节是从站的理由。02 = 非法数据地址、03 = 非法数据值:
# 合并成块读时顺带读了空隙里没人要、从站也没实现的寄存器,回来的就是这两个。
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
_READ_FUNCTIONS = (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS)
# 异常应答的功能码字节是「功能码 | 0x80」。这个标志位与 is_exception_response 是 Modbus
# 全协议共用的(帧格式只是外面的壳),所以只在这里定义一处,MBAP 那边 import 过去用。
EXCEPTION_FLAG = 0x80
# 从站地址 | 功能码 | 字节数(异常应答里这个位置是异常码)
RESPONSE_HEADER_BYTES = 3


def is_exception_response(response_function: int, function: int) -> bool:
    """应答里的功能码字节是不是「功能码|0x80」—— 即从站用异常应答拒了这次请求。

    RTU 与 MBAP 的 PDU 逐字节相同,所以这个判断只有这一份:两份实现各判一次,
    迟早有一边漏掉(漏掉的一边把异常应答当普通应答继续解,读出一坨错误数据)。
    """
    return response_function == function | EXCEPTION_FLAG


def response_length(frame: bytes) -> int | None:
    """按已收到的应答字节算出整帧应有多长,还没看清功能码时返回 None。

    正常应答:3 + 字节数 + CRC 2;异常应答固定 5 字节(地址|功能码|0x80|异常码|CRC)。
    收帧的一方靠它知道该收满多少字节,而不必猜——半帧解出来的假读数比报错难查得多。
    """
    if len(frame) < RESPONSE_HEADER_BYTES:
        return None
    if frame[1] & EXCEPTION_FLAG:
        return 5
    return RESPONSE_HEADER_BYTES + frame[2] + 2


def build_read_request(
    slave: int, address: int, count: int, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """请求帧:从站地址 | 功能码 | 起始寄存器 | 寄存器数 | CRC。"""
    body = bytes([slave, function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + _crc_bytes(body)


def parse_read_request(frame: bytes) -> Tuple[int, int, int, int]:
    """从站侧解析请求帧 → (从站地址, 功能码, 起始寄存器, 寄存器数)。"""
    body = _check_crc(frame)
    if len(body) != 6:
        raise ValueError(f"读寄存器请求帧必须是 8 字节,收到 {len(frame)}: {frame.hex()}")
    if body[1] not in _READ_FUNCTIONS:
        raise ValueError(f"不支持的功能码 {body[1]:#04x},目前只有 03/04 读寄存器: {frame.hex()}")
    return body[0], body[1], int.from_bytes(body[2:4], "big"), int.from_bytes(body[4:6], "big")


def build_read_response(
    slave: int, payload: bytes, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """应答帧:从站地址 | 功能码 | 字节数 | 寄存器数据 | CRC。"""
    if len(payload) > 0xFF:
        raise ValueError(f"一次应答最多 255 字节寄存器数据,拿到 {len(payload)} 字节")
    body = bytes([slave, function, len(payload)]) + payload
    return body + _crc_bytes(body)


def build_write_single_request(
    slave: int, address: int, value: int, function: int = WRITE_SINGLE_REGISTER
) -> bytes:
    """请求帧:从站地址 | 功能码 | 寄存器地址 | 值 | CRC。"""
    body = bytes([slave, function]) + address.to_bytes(2, "big") + value.to_bytes(2, "big")
    return body + _crc_bytes(body)


def parse_write_single_request(frame: bytes) -> Tuple[int, int, int, int]:
    """从站侧解析请求帧 → (从站地址, 功能码, 寄存器地址, 值)。"""
    body = _check_crc(frame)
    if len(body) != 6:
        raise ValueError(f"写单个寄存器请求帧必须是 8 字节,收到 {len(frame)}: {frame.hex()}")
    if body[1] != WRITE_SINGLE_REGISTER:
        raise ValueError(
            f"不支持的功能码 {body[1]:#04x},目前只有 0x06 写单个寄存器: {frame.hex()}"
        )
    return body[0], body[1], int.from_bytes(body[2:4], "big"), int.from_bytes(body[4:6], "big")


def write_response_length(frame: bytes) -> int | None:
    """写应答:正常应答固定 8 字节(0x06 / 0x10 一样),异常应答固定 5 字节。

    不能复用 response_length:0x06 应答的第三个字节是寄存器地址的高字节,不是字节数。
    """
    if len(frame) < 2:
        return None
    if frame[1] & EXCEPTION_FLAG:
        return 5
    return WRITE_SINGLE_REGISTER_BYTES


def parse_write_response(
    frame: bytes, slave: int, address: int, function: int = WRITE_SINGLE_REGISTER
) -> int:
    """客户端解析写应答 → 回显的寄存器值。

    写应答是回显,不是数据,所以这里校验的是"这确实是刚才那一帧的回声":从站地址、
    功能码、寄存器地址都得对上。值对不对不在这里判 —— 那是回读的事,写下去了不等于生效了。
    """
    body = _check_crc(frame)
    if len(body) >= 3 and is_exception_response(body[1], function):
        raise SlaveExceptionResponse(body[0], function, body[2])
    if len(body) != 6:
        raise ValueError(f"写单个寄存器应答必须是 8 字节,收到 {len(frame)}: {frame.hex()}")
    if body[0] != slave:
        raise ValueError(f"应答的从站地址是 {body[0]},不是请求的 {slave}: {frame.hex()}")
    if body[1] != function:
        raise ValueError(f"应答的功能码是 {body[1]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    echoed = int.from_bytes(body[2:4], "big")
    if echoed != address:
        raise ValueError(
            f"应答回显的寄存器地址是 {echoed},不是写入的 {address}: {frame.hex()}"
        )
    return int.from_bytes(body[4:6], "big")


def build_write_multiple_request(
    slave: int, address: int, registers: bytes, function: int = WRITE_MULTIPLE_REGISTERS
) -> bytes:
    """请求帧:从站地址 | 功能码 | 起始寄存器 | 寄存器数 | 字节数 | 寄存器数据 | CRC。

    registers 是已经拼好的寄存器字节(每个寄存器 2 字节,高字节先)。
    """
    if not registers or len(registers) % 2:
        raise ValueError(
            f"写多个寄存器的数据必须是 2 字节的整数倍(每寄存器 2 字节),"
            f"拿到 {len(registers)} 字节: {registers.hex()}"
        )
    count = len(registers) // 2
    if count > WRITE_MULTIPLE_MAX_REGISTERS:
        raise ValueError(
            f"一次最多写 {WRITE_MULTIPLE_MAX_REGISTERS} 个寄存器,拿到 {count} 个"
            f"(分几次写,或先改点表项的 length)"
        )
    body = (
        bytes([slave, function])
        + address.to_bytes(2, "big")
        + count.to_bytes(2, "big")
        + bytes([len(registers)])
        + registers
    )
    return body + _crc_bytes(body)


def parse_write_multiple_request(frame: bytes) -> Tuple[int, int, int, int, bytes]:
    """从站侧解析请求帧 → (从站地址, 功能码, 起始寄存器, 寄存器数, 寄存器数据)。"""
    body = _check_crc(frame)
    if len(body) < 7:
        raise ValueError(f"写多个寄存器请求帧至少 9 字节,收到 {len(frame)}: {frame.hex()}")
    if body[1] != WRITE_MULTIPLE_REGISTERS:
        raise ValueError(
            f"不支持的功能码 {body[1]:#04x},目前只有 0x10 写多个寄存器: {frame.hex()}"
        )
    count = int.from_bytes(body[4:6], "big")
    payload = body[7:]
    if body[6] != len(payload):
        raise ValueError(
            f"请求声称 {body[6]} 字节寄存器数据,实际 {len(payload)} 字节: {frame.hex()}"
        )
    if len(payload) != count * 2:
        raise ValueError(
            f"请求写 {count} 个寄存器(应为 {count * 2} 字节),实际 {len(payload)} 字节: {frame.hex()}"
        )
    return body[0], body[1], int.from_bytes(body[2:4], "big"), count, payload


def build_write_multiple_response(
    slave: int, address: int, count: int, function: int = WRITE_MULTIPLE_REGISTERS
) -> bytes:
    """应答帧:从站地址 | 功能码 | 起始寄存器 | 寄存器数 | CRC —— 没有数据段。

    写应答只确认"这几个寄存器收下了",写进去的值不在应答里(要值就回读)。
    """
    body = bytes([slave, function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return body + _crc_bytes(body)


def parse_write_multiple_response(
    frame: bytes,
    slave: int,
    address: int,
    count: int,
    function: int = WRITE_MULTIPLE_REGISTERS,
) -> None:
    """客户端解析 0x10 应答:校验它是刚才那一帧的回声(从站 / 功能码 / 起始地址 / 寄存器数)。

    与 0x06 同一个口径:值有没有生效由回读判,这里只管"回的确实是这次写"。
    """
    body = _check_crc(frame)
    if len(body) >= 3 and is_exception_response(body[1], function):
        raise SlaveExceptionResponse(body[0], function, body[2])
    if len(body) != 6:
        raise ValueError(f"写多个寄存器应答必须是 8 字节,收到 {len(frame)}: {frame.hex()}")
    if body[0] != slave:
        raise ValueError(f"应答的从站地址是 {body[0]},不是请求的 {slave}: {frame.hex()}")
    if body[1] != function:
        raise ValueError(f"应答的功能码是 {body[1]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    echoed_address = int.from_bytes(body[2:4], "big")
    if echoed_address != address:
        raise ValueError(
            f"应答回显的起始地址是 {echoed_address},不是写入的 {address}: {frame.hex()}"
        )
    echoed_count = int.from_bytes(body[4:6], "big")
    if echoed_count != count:
        raise ValueError(f"应答回显的寄存器数是 {echoed_count},不是写入的 {count}: {frame.hex()}")


class SlaveExceptionResponse(ValueError):
    """从站用异常应答拒绝了这次请求:功能码|0x80 + 异常码。

    仍然是 ValueError(既有调用方照旧当"应答不可用"处理),单独一个类型是因为上层要能
    把它和超时 / CRC 错分开:异常应答是设备对这次请求范围的回答(拆小可以重试),
    超时是链路没回话(重试只会更慢)。
    """

    def __init__(self, slave: int, function: int, code: int) -> None:
        super().__init__(
            f"从站 {slave} 返回异常应答: 功能码 {function | EXCEPTION_FLAG:#04x} "
            f"异常码 {code:#04x}"
        )
        self.slave = slave
        self.function = function
        self.code = code


def build_exception_response(
    slave: int, code: int, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """异常应答帧:从站地址 | 功能码|0x80 | 异常码 | CRC。"""
    body = bytes([slave, function | EXCEPTION_FLAG, code])
    return body + _crc_bytes(body)


def parse_read_response(
    frame: bytes, slave: int, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """客户端解析应答帧 → 寄存器原始字节。

    CRC 错、从站地址不对、功能码不对、字节数不符、从站返回异常应答,一律抛错:
    带着错帧往下解码只会得到"看起来正常"的假读数,那比报错难查得多。
    """
    body = _check_crc(frame)
    if len(body) < 3:
        raise ValueError(f"应答帧太短: {frame.hex()}")
    if is_exception_response(body[1], function):
        raise SlaveExceptionResponse(body[0], function, body[2])
    if body[0] != slave:
        raise ValueError(f"应答的从站地址是 {body[0]},不是请求的 {slave}: {frame.hex()}")
    if body[1] != function:
        raise ValueError(f"应答的功能码是 {body[1]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    payload = body[3:]
    if body[2] != len(payload):
        raise ValueError(f"应答声称 {body[2]} 字节,实际 {len(payload)} 字节: {frame.hex()}")
    return payload


def _crc_bytes(body: bytes) -> bytes:
    """Modbus 的 CRC 上线是低字节先。"""
    crc = crc16_modbus(body)
    return bytes([crc & 0xFF, crc >> 8])


def _check_crc(frame: bytes) -> bytes:
    """校验并去掉尾部 CRC,返回帧体。"""
    if len(frame) < 4:
        raise ValueError(f"帧太短,连 CRC 都不够: {frame.hex()}")
    body, received = frame[:-2], frame[-2:]
    expected = _crc_bytes(body)
    if received != expected:
        raise ValueError(
            f"CRC 校验失败: 帧里是 {received.hex()},按帧体算应为 {expected.hex()}(帧 {frame.hex()})"
        )
    return body
