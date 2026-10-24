"""
Modbus TCP 的帧格式:7 字节 MBAP 头 + PDU。和 RTU 的差别只在帧头,PDU 逐字节相同。

    MBAP 头  事务号 2 | 协议号 2(恒 0)| 长度 2(= 单元号 1 + PDU 字节数)| 单元号 1
    PDU      功能码 1 | 数据(读请求 4 字节、读应答 1 + N 字节、写单个寄存器 4 字节、异常应答 1 字节)

帧里没有 CRC:TCP 自己保证字节完整。于是长度字段成了收帧一方唯一的帧边界,算错就等于永远
收不满一帧 —— 这正是"把 RTU 帧喂给这里"必须报错的原因:少了 7 字节头,RTU 的从站地址会落到
事务号上、寄存器地址会落到协议号上,照着一个假的长度字段继续解,解出来的是一帧"看着正常"的假帧。
协议号恒 0 是这一层最强的一条自检,而 RTU 帧的第 2–3 字节是起始寄存器地址,凑不出这个 0。

纯函数:不 import transport、不 import domain,也不认识点表。功能码 / 异常码 / 从站拒绝的
异常类型与 RTU 共用一份定义(见 protocol/modbus_rtu.py)—— 它们是 Modbus 的,不是某一种帧的。
"""
from __future__ import annotations

from typing import Tuple

from .crc import crc16_modbus
from .modbus_rtu import (
    EXCEPTION_FLAG,
    READ_HOLDING_REGISTERS,
    READ_INPUT_REGISTERS,
    WRITE_MULTIPLE_MAX_REGISTERS,
    WRITE_MULTIPLE_REGISTERS,
    WRITE_SINGLE_REGISTER,
    SlaveExceptionResponse,
    is_exception_response,
)

MBAP_HEADER_BYTES = 7
# 事务号 + 协议号 + 长度:这 6 个字节之后剩的就是"单元号 + PDU",也就是长度字段声称的字节数。
_PACKET_HEADER_BYTES = 6
PROTOCOL_ID = 0
_READ_FUNCTIONS = (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS)


class TransactionMismatch(ValueError):
    """应答的事务号对不上当前未完成那个请求:这是上一条请求的迟到应答。

    单独一个类型,因为三种现场结论各不相同:超时是链路没回话、断链是对端没了、从站拒绝是
    设备答了"这个地址我不认"。事务号错位是第四种 —— 从站答了,但答的是上一条请求,
    意味着上位机的时序已经错了。仍然是 ValueError(既有调用方照旧当"应答不可用"),
    但绝不能落进 SlaveExceptionResponse 的处理分支:那不是设备拒绝,拆小重试也变不出数据。
    """

    def __init__(self, expected: int, actual: int, frame: bytes) -> None:
        super().__init__(
            f"应答的事务号是 {actual},当前未完成请求的事务号是 {expected}:"
            f"这是上一条请求的迟到应答(这条请求的应答还没到),不能当成本次读数: {frame.hex()}"
        )
        self.expected = expected
        self.actual = actual


def _check_transaction(transaction: int, expected: int | None, frame: bytes) -> None:
    """应答的事务号必须是当前未完成那个请求的。expected 为 None = 调用方声明这一帧没有期望值。

    这是 MBAP 唯一能认出"错位应答"的手段:迟到的那一帧可能来自同一从站、同一功能码、
    长度也对 —— 单元号 / 功能码 / 字节数三项检查全过。
    """
    if expected is not None and transaction != expected:
        raise TransactionMismatch(expected, transaction, frame)


def response_length(frame: bytes) -> int | None:
    """按 MBAP 头里的长度字段算出整帧应有多少字节,头还没收全时返回 None。

    长度字段 = 单元号 1 + PDU 字节数,所以整帧 = 6 + 长度。读 / 写 / 异常应答同一条算路:
    MBAP 的长度字段覆盖整帧,不像 RTU 要按功能码分两套(见 modbus_rtu.response_length)。
    """
    if len(frame) < MBAP_HEADER_BYTES:
        return None
    return _PACKET_HEADER_BYTES + int.from_bytes(frame[4:6], "big")


def build_read_request(
    transaction: int, unit: int, address: int, count: int, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """请求 PDU:功能码 | 起始寄存器 | 寄存器数 —— 与 RTU 逐字节相同,前面多 7 字节头。"""
    if function not in _READ_FUNCTIONS:
        raise ValueError(
            f"读寄存器只有功能码 03/04,拿到 {function:#04x}(写单个 / 多个寄存器走 build_write_*)"
        )
    pdu = bytes([function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return _frame(transaction, unit, pdu)


def parse_read_request(frame: bytes) -> Tuple[int, int, int, int, int]:
    """从站侧解析请求帧 → (事务号, 单元号, 功能码, 起始寄存器, 寄存器数)。"""
    transaction, unit, pdu = _split(frame)
    if len(pdu) != 5:
        raise ValueError(
            f"读寄存器请求的 PDU 必须是 5 字节(功能码 + 地址 2 + 个数 2),"
            f"收到 {len(pdu)} 字节: {frame.hex()}"
        )
    function = pdu[0]
    if function not in _READ_FUNCTIONS:
        raise ValueError(f"不支持的功能码 {function:#04x},目前只有 03/04 读寄存器: {frame.hex()}")
    return (
        transaction,
        unit,
        function,
        int.from_bytes(pdu[1:3], "big"),
        int.from_bytes(pdu[3:5], "big"),
    )


def build_read_response(
    transaction: int, unit: int, payload: bytes, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """应答 PDU:功能码 | 字节数 | 寄存器数据。"""
    if len(payload) > 0xFF:
        raise ValueError(f"一次应答最多 255 字节寄存器数据,拿到 {len(payload)} 字节")
    return _frame(transaction, unit, bytes([function, len(payload)]) + payload)


def parse_read_response(
    frame: bytes,
    unit: int,
    function: int = READ_HOLDING_REGISTERS,
    *,
    expected_transaction: int | None,
) -> bytes:
    """客户端解析读应答 → 寄存器原始字节。

    异常应答抛 SlaveExceptionResponse(与 RTU 同一个类型:从站拒绝是同一件事,上层照它降级
    拆小重试,不按帧格式分叉)。单元号 / 功能码不符、字节数与实际数据不符一律抛错:
    带着错帧往下解码只会得到"看起来正常"的假读数。

    expected_transaction 是请求侧记住的那个未完成事务号,keyword-only 必填:给默认值就等于
    "不传就不校验",而校验悄悄失效正是迟到应答被当成这次读数的口子。确实没有期望值的场景
    (从站侧解析、帧不是自己发的)显式传 None。事务号先于异常码判:迟到的异常应答不能被
    读成"从站拒绝"(那会触发拆小重试,把错位藏起来)。
    """
    transaction, frame_unit, pdu = _split(frame)
    _check_transaction(transaction, expected_transaction, frame)
    if len(pdu) < 2:
        raise ValueError(f"应答 PDU 太短: {frame.hex()}")
    if is_exception_response(pdu[0], function):
        raise SlaveExceptionResponse(frame_unit, function, pdu[1])
    if frame_unit != unit:
        raise ValueError(f"应答的单元号是 {frame_unit},不是请求的 {unit}: {frame.hex()}")
    if pdu[0] != function:
        raise ValueError(f"应答的功能码是 {pdu[0]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    payload = pdu[2:]
    if pdu[1] != len(payload):
        raise ValueError(f"应答声称 {pdu[1]} 字节,实际 {len(payload)} 字节: {frame.hex()}")
    return payload


def build_write_single_request(
    transaction: int,
    unit: int,
    address: int,
    value: int,
    function: int = WRITE_SINGLE_REGISTER,
) -> bytes:
    """请求 PDU:功能码 | 寄存器地址 | 值。应答是这一帧原样回显(事务号也照拄)。"""
    pdu = bytes([function]) + address.to_bytes(2, "big") + value.to_bytes(2, "big")
    return _frame(transaction, unit, pdu)


def parse_write_single_request(frame: bytes) -> Tuple[int, int, int, int, int]:
    """从站侧解析请求帧 → (事务号, 单元号, 功能码, 寄存器地址, 值)。"""
    transaction, unit, pdu = _split(frame)
    if len(pdu) != 5:
        raise ValueError(
            f"写单个寄存器请求的 PDU 必须是 5 字节(功能码 + 地址 2 + 值 2),"
            f"收到 {len(pdu)} 字节: {frame.hex()}"
        )
    if pdu[0] != WRITE_SINGLE_REGISTER:
        raise ValueError(
            f"不支持的功能码 {pdu[0]:#04x},目前只有 0x06 写单个寄存器: {frame.hex()}"
        )
    return (
        transaction,
        unit,
        pdu[0],
        int.from_bytes(pdu[1:3], "big"),
        int.from_bytes(pdu[3:5], "big"),
    )


def parse_write_response(
    frame: bytes,
    unit: int,
    address: int,
    function: int = WRITE_SINGLE_REGISTER,
    *,
    expected_transaction: int | None,
) -> int:
    """客户端解析 0x06 应答 → 回显的寄存器值。

    与 RTU 同一个口径:写应答是回显不是数据,所以校验"回的确实是刚才那一帧"(事务号、
    单元号、功能码、寄存器地址)。值生不生效由回读判,不在这里判。expected_transaction
    必填,没有期望值就显式传 None(同 parse_read_response)。
    """
    transaction, frame_unit, pdu = _split(frame)
    _check_transaction(transaction, expected_transaction, frame)
    if len(pdu) >= 2 and is_exception_response(pdu[0], function):
        raise SlaveExceptionResponse(frame_unit, function, pdu[1])
    if len(pdu) != 5:
        raise ValueError(f"写单个寄存器应答的 PDU 必须是 5 字节,收到 {len(pdu)}: {frame.hex()}")
    if frame_unit != unit:
        raise ValueError(f"应答的单元号是 {frame_unit},不是请求的 {unit}: {frame.hex()}")
    if pdu[0] != function:
        raise ValueError(f"应答的功能码是 {pdu[0]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    echoed = int.from_bytes(pdu[1:3], "big")
    if echoed != address:
        raise ValueError(f"应答回显的寄存器地址是 {echoed},不是写入的 {address}: {frame.hex()}")
    return int.from_bytes(pdu[3:5], "big")


def build_write_multiple_request(
    transaction: int,
    unit: int,
    address: int,
    registers: bytes,
    function: int = WRITE_MULTIPLE_REGISTERS,
) -> bytes:
    """请求 PDU:功能码 | 起始寄存器 | 寄存器数 | 字节数 | 寄存器数据(每寄存器 2 字节)。"""
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
    pdu = (
        bytes([function])
        + address.to_bytes(2, "big")
        + count.to_bytes(2, "big")
        + bytes([len(registers)])
        + registers
    )
    return _frame(transaction, unit, pdu)


def parse_write_multiple_request(frame: bytes) -> Tuple[int, int, int, int, bytes]:
    """从站侧解析请求帧 → (事务号, 单元号, 功能码, 起始寄存器, 寄存器数, 寄存器数据)。"""
    transaction, unit, pdu = _split(frame)
    if len(pdu) < 6:
        raise ValueError(f"写多个寄存器请求的 PDU 至少 6 字节,收到 {len(pdu)}: {frame.hex()}")
    if pdu[0] != WRITE_MULTIPLE_REGISTERS:
        raise ValueError(
            f"不支持的功能码 {pdu[0]:#04x},目前只有 0x10 写多个寄存器: {frame.hex()}"
        )
    count = int.from_bytes(pdu[3:5], "big")
    payload = pdu[6:]
    if pdu[5] != len(payload):
        raise ValueError(
            f"请求声称 {pdu[5]} 字节寄存器数据,实际 {len(payload)} 字节: {frame.hex()}"
        )
    if len(payload) != count * 2:
        raise ValueError(
            f"请求写 {count} 个寄存器(应为 {count * 2} 字节),"
            f"实际 {len(payload)} 字节: {frame.hex()}"
        )
    return transaction, unit, pdu[0], int.from_bytes(pdu[1:3], "big"), count, payload


def build_write_multiple_response(
    transaction: int,
    unit: int,
    address: int,
    count: int,
    function: int = WRITE_MULTIPLE_REGISTERS,
) -> bytes:
    """应答 PDU:功能码 | 起始寄存器 | 寄存器数 —— 没有数据段。"""
    pdu = bytes([function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return _frame(transaction, unit, pdu)


def parse_write_multiple_response(
    frame: bytes,
    unit: int,
    address: int,
    count: int,
    function: int = WRITE_MULTIPLE_REGISTERS,
    *,
    expected_transaction: int | None,
) -> None:
    """客户端解析 0x10 应答:校验它是刚才那一帧的回声(事务号 / 单元号 / 功能码 / 地址 / 个数)。

    expected_transaction 必填,没有期望值就显式传 None(同 parse_read_response)。
    """
    transaction, frame_unit, pdu = _split(frame)
    _check_transaction(transaction, expected_transaction, frame)
    if len(pdu) >= 2 and is_exception_response(pdu[0], function):
        raise SlaveExceptionResponse(frame_unit, function, pdu[1])
    if len(pdu) != 5:
        raise ValueError(f"写多个寄存器应答的 PDU 必须是 5 字节,收到 {len(pdu)}: {frame.hex()}")
    if frame_unit != unit:
        raise ValueError(f"应答的单元号是 {frame_unit},不是请求的 {unit}: {frame.hex()}")
    if pdu[0] != function:
        raise ValueError(f"应答的功能码是 {pdu[0]:#04x},不是请求的 {function:#04x}: {frame.hex()}")
    echoed_address = int.from_bytes(pdu[1:3], "big")
    if echoed_address != address:
        raise ValueError(
            f"应答回显的起始地址是 {echoed_address},不是写入的 {address}: {frame.hex()}"
        )
    echoed_count = int.from_bytes(pdu[3:5], "big")
    if echoed_count != count:
        raise ValueError(f"应答回显的寄存器数是 {echoed_count},不是写入的 {count}: {frame.hex()}")


def build_exception_response(
    transaction: int, unit: int, code: int, function: int = READ_HOLDING_REGISTERS
) -> bytes:
    """异常应答 PDU:功能码|0x80 | 异常码 —— 长度字段是 3(单元号 + 这两个字节)。"""
    return _frame(transaction, unit, bytes([function | EXCEPTION_FLAG, code]))


def _frame(transaction: int, unit: int, pdu: bytes) -> bytes:
    """MBAP 头 + PDU。长度字段 = 单元号 + PDU 字节数,算错就是收帧的那一方永远收不满。"""
    return (
        transaction.to_bytes(2, "big")
        + PROTOCOL_ID.to_bytes(2, "big")
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit])
        + pdu
    )


def _split(frame: bytes) -> Tuple[int, int, bytes]:
    """校验 MBAP 头并交出 (事务号, 单元号, PDU)。

    两条硬校验:协议号必须 0,长度字段必须与帧里实际的 PDU 字节数一致。不符就抛错,
    并在像 RTU 帧时点明是哪一种帧 —— 否则现场只会看到"协议号不对",然后去怀疑设备固件。
    """
    if len(frame) < MBAP_HEADER_BYTES:
        raise ValueError(
            f"不是 MBAP 帧: 头要 {MBAP_HEADER_BYTES} 字节,只收到 {len(frame)} 字节: {frame.hex()}"
        )
    transaction = int.from_bytes(frame[0:2], "big")
    protocol = int.from_bytes(frame[2:4], "big")
    declared = int.from_bytes(frame[4:6], "big")
    unit = frame[6]
    pdu = frame[MBAP_HEADER_BYTES:]
    if protocol != PROTOCOL_ID:
        raise ValueError(
            f"不是 MBAP 帧: 协议号必须是 {PROTOCOL_ID},帧里是 {protocol:#06x}"
            f"{_format_hint(frame)}"
        )
    if declared != len(pdu) + 1:
        raise ValueError(
            f"MBAP 长度字段声称 {declared} 字节(单元号 + PDU),帧里实际 {len(pdu) + 1} 字节"
            f"{_format_hint(frame)}"
        )
    return transaction, unit, pdu


def _format_hint(frame: bytes) -> str:
    """错帧的现场提示:尾部两字节能按 CRC 校验上的短帧是 RTU 帧,不是 MBAP 帧。

    少了 7 字节 MBAP 头,RTU 的从站地址落在事务号上、起始寄存器落在协议号上 —— 不点明这一句,
    "协议号 / 长度字段不对"会被读成设备固件有问题,而实际上是清单里规约写错了。
    """
    if len(frame) >= 4 and crc16_modbus(frame[:-2]).to_bytes(2, "little") == frame[-2:]:
        return (
            f"(尾部两字节是 RTU 的 CRC:这是 RTU 帧,没有 MBAP 头,"
            f"别用 modbus_tcp 解析它): {frame.hex()}"
        )
    return f": {frame.hex()}"
