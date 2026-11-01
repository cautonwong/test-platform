"""
DL/T645-2007 的帧格式:请求帧与应答帧的拼装与解析,含 CS 校验。

帧结构(字节数):
    68 | 地址域 6 | 68 | C 1 | L 1 | 数据域 L | CS 1 | 16
    地址域  6 字节 BCD,低字节先发:表地址 "000000000001" 上线是 01 00 00 00 00 00
    数据域  每字节在链路上 +0x33(所以线上看不见 00 字节),数据标识也在这层偏移里
    CS      从第一个 68 起到数据域末字节的算术和,取低字节

一条真实报文(上游 legacy 平台 common/dlt645.py 里的透传示例,读正向有功总电能):
    68 01 00 00 00 00 00 68 11 04 33 33 34 33 B3 16
    C=11 读数据,L=04,数据域 33 33 34 33 减 0x33 得 00 00 01 00,低字节先 → 数据标识 00010000

纯函数:不 import transport、不 import domain,也不认识点表 —— 只认识表地址与数据标识。
点表项的 address 就是这里的数据标识(DI,4 字节,写成 8 位十六进制,和上游
common/p645dict.py 的键同一个写法);长度、倍率、字节序都在点表里。
"""
from __future__ import annotations

from typing import Any, Tuple

FRAME_START = 0x68
FRAME_END = 0x16
READ_DATA = 0x11
# 跳合闸/报警/保电（标准 7.13）：请求 1CH、正常应答 9CH、异常应答 DCH。
CONTROL = 0x1C
CONTROL_RESPONSE = 0x9C
CONTROL_ERROR = 0xDC
# 数据域 N1：控制命令类型（标准 7.13 注 1）
CMD_TRIP = 0x1A
CMD_ALLOW_CLOSE = 0x1B
CMD_ALARM = 0x2A
CMD_ALARM_CLEAR = 0x2B
CMD_KEEP_POWER = 0x3A
CMD_KEEP_CLEAR = 0x3B
_RESPONSE_FLAG = 0x80
_EXCEPTION_FLAG = 0x40
DATA_OFFSET = 0x33
# 数据域每字节 +0x33 转义,能上线的数据字节就最多 0xFF-0x33 = 0xCC。
# TODO(来源): 仓库里没有 DL/T645-2007 转义条文摘录,这条上界是从本文件开头
# "数据域每字节在链路上 +0x33" 推的,不是抄的标准原文。
MAX_DATA_BYTE = 0xFF - DATA_OFFSET
ADDRESS_BYTES = 6
DATA_ID_BYTES = 4
# 这是帧内 L(长度)上限,不是数据域单字节上界(见 MAX_DATA_BYTE)。
_MAX_FRAME_DATA_LEN = 0xFF
# 68 + 地址 6 + 68 + C + L + CS + 16,数据域为空时的最短帧长
_MIN_FRAME = ADDRESS_BYTES + 6
# 68 | 地址域 6 | 68 | C | L —— 收到这 10 字节才知道整帧有多长
RESPONSE_HEADER_BYTES = 10


def response_length(frame: bytes) -> int | None:
    """按已收到的应答字节算出整帧应有多长,还没收到 L 时返回 None。

    帧长 = 最短帧 12 + L(数据域字节数)。收帧的一方靠它知道该收满多少字节。
    """
    if len(frame) < RESPONSE_HEADER_BYTES:
        return None
    return _MIN_FRAME + frame[9]


def build_read_request(meter_address: str, data_id: str) -> bytes:
    """读数据请求帧:地址域 | C=11 | L=04 | 数据标识。"""
    return _build_frame(meter_address, READ_DATA, _escape(_data_id_bytes(data_id)))


def build_read_response(meter_address: str, data_id: str, value: bytes) -> bytes:
    """读数据正常应答:地址域 | C=91(11|80) | 数据标识 + 值字节(低字节先)。"""
    return _build_frame(
        meter_address, READ_DATA | _RESPONSE_FLAG, _escape(_data_id_bytes(data_id) + value)
    )


def parse_read_request(frame: bytes) -> Tuple[str, str]:
    """从站侧解析请求帧 → (表地址, 数据标识)。"""
    address, control, raw = _parse_frame(frame)
    if control != READ_DATA:
        raise ValueError(f"不是读数据命令(控制码 {control:#04x}): {frame.hex()}")
    data = _unescape(raw)
    if len(data) != DATA_ID_BYTES:
        raise ValueError(
            f"读数据请求的数据域只有数据标识 {DATA_ID_BYTES} 字节,收到 {len(data)} 字节: {frame.hex()}"
        )
    return _address_text(address), _data_id_text(data)


def parse_read_response(frame: bytes, meter_address: str, data_id: str) -> bytes:
    """客户端解析应答帧 → 值字节(已去掉 0x33 偏移,仍是线路上低字节先的顺序)。

    CS 错、表地址不是这块表、数据标识不是请求的那个、表回异常应答,一律抛错:
    带着错帧往下解码只会得到"看起来正常"的假读数,那比报错难查得多。
    返回的第一段就是低位字节,正好对上点表里 byteorder: little + 读长度切片的语义。
    """
    address, control, raw = _parse_frame(frame)
    if address != _address_bytes(meter_address):
        raise ValueError(
            f"应答的表地址是 {_address_text(address)},不是请求的 {meter_address}: {frame.hex()}"
        )
    if control == READ_DATA | _RESPONSE_FLAG | _EXCEPTION_FLAG:
        # 异常应答的数据域是错误信息字,不是读数。标准里它是否加 0x33 各家实现不一,
        # 这里只把原始字节交出去,不去猜它的位含义(shortcut: 只报原始错误信息字,要解位时按设备实测再补)。
        raise ValueError(f"表 {meter_address} 返回异常应答,错误信息字原始字节 {raw.hex()}")
    if control != READ_DATA | _RESPONSE_FLAG:
        raise ValueError(f"不是读数据应答(控制码 {control:#04x}): {frame.hex()}")
    data = _unescape(raw)
    if len(data) < DATA_ID_BYTES or data[:DATA_ID_BYTES] != _data_id_bytes(data_id):
        raise ValueError(
            f"应答的数据标识是 {_data_id_text(data[:DATA_ID_BYTES])},不是请求的 {data_id}: {frame.hex()}"
        )
    return data[DATA_ID_BYTES:]


def _build_frame(meter_address: str, control: int, data: bytes) -> bytes:
    if len(data) > _MAX_FRAME_DATA_LEN:
        raise ValueError(f"L 只有 1 字节,数据域最多 {_MAX_FRAME_DATA_LEN} 字节,拿到 {len(data)} 字节")
    body = (
        bytes([FRAME_START])
        + _address_bytes(meter_address)
        + bytes([FRAME_START, control, len(data)])
        + data
    )
    return body + bytes([sum(body) & 0xFF, FRAME_END])


def parse_control_request(frame: bytes) -> Tuple[str, int, bytes]:
    """从站侧解析跳合闸/报警/保电请求（控制码 1CH，标准 7.13）。

    帧：`68 A0..A5 68 1C L PA P0P1P2 C0..C3 N1..Nm CS 16`，数据域每字节 +33H。

    返回 `(表地址, N1 命令类型, 数据域原始字节已减 33H)`。
    N1 在密码(4) + 操作者代码(4) 之后，即数据域第 9 个字节。

    单独一个函数而不是塞进 `parse_read_request`：读帧的数据域以 4 字节数据标识开头，
    控制帧以密码开头 —— 用同一个解析器会把密码当数据标识，读出一个"看着像"的 DI。
    """
    address_bytes, control, raw = _parse_frame(frame)
    if control != CONTROL:
        raise ValueError(f"不是控制命令帧(控制码 {control:#04x}): {frame.hex()}")

    data = bytes((b - DATA_OFFSET) & 0xFF for b in raw)
    if len(data) < 10:
        raise ValueError(
            f"控制命令数据域至少要 密码4+操作者4+N1+N2 = 10 字节,收到 {len(data)}: {frame.hex()}"
        )
    address = "".join(f"{b:02x}" for b in reversed(address_bytes))
    return address, data[8], data


def build_control_response(meter_address: str) -> bytes:
    """跳合闸正常应答：`68 地址 68 9C 00 CS 16`（L=00，无数据域）。"""
    return _build_frame(meter_address, CONTROL_RESPONSE, b"")


def _parse_frame(frame: bytes) -> Tuple[bytes, int, bytes]:
    """校验帧定界符、长度与 CS,返回 (地址域原始字节, 控制码, 数据域的原始字节)。

    **先丢掉前导符 FE**：标准 5.3.1 规定主站发帧前先发 4 个 FEH 唤醒接收方，
    真实电表收到后直接丢弃。台架如果不去掉，控制命令帧会被当成"帧定界符不对"
    —— 现象是产品明明发出去了、台架记成 `<unparsed>`。
    """
    frame = frame.lstrip(b"\xfe") if frame.startswith(b"\xfe") else frame
    if len(frame) < _MIN_FRAME:
        raise ValueError(f"帧太短,DL/T645 最短 {_MIN_FRAME} 字节: {frame.hex()}")
    if frame[0] != FRAME_START or frame[7] != FRAME_START or frame[-1] != FRAME_END:
        raise ValueError(f"帧定界符不对(应为 68 … 68 … 16): {frame.hex()}")
    length = frame[9]
    if len(frame) != _MIN_FRAME + length:
        raise ValueError(
            f"帧长 {len(frame)} 字节与 L={length} 对不上,应为 {_MIN_FRAME + length} 字节: {frame.hex()}"
        )
    body, received = frame[:-2], frame[-2]
    expected = sum(body) & 0xFF
    if received != expected:
        raise ValueError(
            f"CS 校验失败: 帧里是 {received:#04x},按帧体算应为 {expected:#04x}(帧 {frame.hex()})"
        )
    return body[1 : 1 + ADDRESS_BYTES], body[ADDRESS_BYTES + 2], body[ADDRESS_BYTES + 4 :]


def _address_bytes(meter_address: str) -> bytes:
    """表地址 6 字节 BCD,低字节先发。"""
    if (
        isinstance(meter_address, str)
        and len(meter_address) == ADDRESS_BYTES * 2
        and meter_address.isascii()
        and meter_address.isdigit()
    ):
        return bytes.fromhex(meter_address)[::-1]
    raise ValueError(
        f'表地址必须是 {ADDRESS_BYTES * 2} 位 BCD 数字(如 "000000000001"),收到 {meter_address!r}'
    )


def _address_text(raw: bytes) -> str:
    return raw[::-1].hex()


def _data_id_bytes(data_id: Any) -> bytes:
    """数据标识 4 字节,低字节先发。"""
    if isinstance(data_id, str) and len(data_id) == DATA_ID_BYTES * 2:
        try:
            return bytes.fromhex(data_id)[::-1]
        except ValueError:
            pass
    raise ValueError(
        f'数据标识必须是 {DATA_ID_BYTES * 2} 位十六进制(如 "00010000"),收到 {data_id!r}'
    )


def _data_id_text(raw: bytes) -> str:
    return raw[::-1].hex()


def _escape(data: bytes) -> bytes:
    """数据域每字节 +0x33。编码不了的字节报范围错,不让 bytes() 抛那句看不懂的。"""
    for index, byte in enumerate(data):
        if byte > MAX_DATA_BYTE:
            raise ValueError(
                f"数据域第 {index} 字节是 {byte:#04x},超了上界 {MAX_DATA_BYTE:#04x}"
                f"(每字节 +{DATA_OFFSET:#04x} 后不能过 0xff);"
                f"DL/T645 数据域只允许 0x00–{MAX_DATA_BYTE:#04x}"
            )
    return bytes(byte + DATA_OFFSET for byte in data)


def _unescape(raw: bytes) -> bytes:
    if any(byte < DATA_OFFSET for byte in raw):
        raise ValueError(
            f"数据域每字节都 +{DATA_OFFSET:#04x},出现更小的字节说明帧不可信: {raw.hex()}"
        )
    return bytes(byte - DATA_OFFSET for byte in raw)
