"""
点表项的解码:原始字节 → 物理量。纯函数,不 import transport、不 import domain。

支持的编码就是下面 DECODERS 那张表:清单校验(POINT_ENCODINGS)由它派生,
加一种编码只改这里一处,不会出现"装配校验通过、运行时 ValueError"。
倍率、字节序、字序都在这里生效。换算与字节序写错在报告里表现为"读数差 100 倍",
所以这一层单测是唯一允许新增的纯函数缝(不需要设备、不需要传输)。

写方向(encode_value)是读方向(decode_value)的逆运算,两边共用同一个字序重排:
读出来的值原样写回去,寄存器字节必须完全一致。
"""
from __future__ import annotations

import math
import struct

_FLOAT32_FORMAT = {"big": ">f", "little": "<f"}


def _ascii(raw: bytes, byteorder: str) -> float:
    """文本 token。ascii 没有字节序:清单里写了 byteorder 会在装配前被拒。"""
    return float(raw.decode("ascii").strip())


def _uint(raw: bytes, byteorder: str) -> int:
    return int.from_bytes(raw, byteorder)


def _int(raw: bytes, byteorder: str) -> int:
    return int.from_bytes(raw, byteorder, signed=True)


def _bcd(raw: bytes, byteorder: str) -> int:
    """BCD:每字节两个十进制数字(高半字节在前)。字节序 little 表示低字节先发,先倒过来。"""
    ordered = raw[::-1] if byteorder == "little" else raw
    digits = ordered.hex()
    # 0x0A–0x0F 不是十进制数字,静默当成数字会得到"看起来正常"的假读数。
    if any(digit > "9" for digit in digits):
        raise ValueError(f"BCD 字节含非十进制半字节: {raw.hex()}")
    return int(digits)


def _float32(raw: bytes, byteorder: str) -> float:
    return struct.unpack(_FLOAT32_FORMAT[byteorder], raw)[0]


def _encode_whole(value: float, encoding: str, length: int, low: int, high: int) -> int:
    """把已按倍率反算过的值取整,并检查它真能放进 encoding/length 的范围里。

    越界报错而不截断:2 字节写 70000 截成 4464 是一个"看着合理"的值,写下去设备会接受、
    回读也一致,现场没人查得出来。
    """
    if not math.isfinite(value):
        raise ValueError(f"{encoding} 编码只能是有限数,拿到 {value}")
    number = round(value)
    if not low <= number <= high:
        raise ValueError(
            f"{encoding} 编码 length={length} 的范围是 {low}–{high},拿到 {value}(取整后 {number})"
        )
    return number


def _encode_uint(value: float, length: int, byteorder: str) -> bytes:
    return _encode_whole(value, "uint", length, 0, 2 ** (8 * length) - 1).to_bytes(
        length, byteorder
    )


def _encode_int(value: float, length: int, byteorder: str) -> bytes:
    bits = 8 * length
    return _encode_whole(value, "int", length, -(2 ** (bits - 1)), 2 ** (bits - 1) - 1).to_bytes(
        length, byteorder, signed=True
    )


def _encode_bcd(value: float, length: int, byteorder: str) -> bytes:
    """BCD:十进制数字两位一字节,位数不足补前导零(高半字节在前)。

    读回来的值是这串数字当十进制数(int),所以写 120000 与写 "00120000" 是同一件事:
    补几位由 length 决定,不由写进来的字符串字面决定。数字位数超过 2×length 就是
    这个点表示不了的值,报错而不是截掉高位。
    """
    digits = 2 * length
    raw = bytes.fromhex(f"{_encode_whole(value, 'bcd', length, 0, 10 ** digits - 1):0{digits}d}")
    return raw[::-1] if byteorder == "little" else raw


def _encode_float32(value: float, length: int, byteorder: str) -> bytes:
    if length != 4:
        raise ValueError(f"float32 必须是 4 字节,拿到 length={length}")
    try:
        return struct.pack(_FLOAT32_FORMAT[byteorder], value)
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"float32 表示不了 {value}(约 ±3.4e38 之外): {exc}") from None


# "支持哪些编码"只有这一份声明:config/manifest.py 的 POINT_ENCODINGS 由它派生。
DECODERS = {"uint": _uint, "int": _int, "bcd": _bcd, "float32": _float32, "ascii": _ascii}
# 写方向只给数值编码:ascii 文本点什么字符算合法由设备定,没有一条能通用的规则。
ENCODERS = {
    "uint": _encode_uint,
    "int": _encode_int,
    "bcd": _encode_bcd,
    "float32": _encode_float32,
}


def _by_word_order(raw: bytes, word_order: str) -> bytes:
    """按字序重排:little 表示低字先到(寄存器顺序颠倒)。

    字的顺序和字内字节顺序是两条独立的轴:byteorder 只管每个字内部的字节,
    32 位值跨两个寄存器时四种组合都真实存在,只认 byteorder 会读出一个数量级错的值。
    """
    if word_order == "big":
        return raw
    if len(raw) % 2:
        raise ValueError(
            f"字序 little 按整寄存器重排,字节数必须是偶数,拿到 {len(raw)} 字节: {raw.hex()}"
        )
    return b"".join(raw[index : index + 2] for index in range(len(raw) - 2, -2, -2))


def decode_value(
    raw: bytes,
    encoding: str,
    scale: float = 1.0,
    byteorder: str = "big",
    word_order: str = "big",
) -> float:
    """把一段原始字节按点表项声明的编码/字节序/字序解成物理量(已乘倍率)。"""
    try:
        decode = DECODERS[encoding]
    except KeyError:
        raise ValueError(f"未知编码 {encoding!r},可用: {sorted(DECODERS)}") from None
    return decode(_by_word_order(raw, word_order), byteorder) * scale


def encode_value(
    value: float,
    encoding: str,
    length: int,
    scale: float = 1.0,
    byteorder: str = "big",
    word_order: str = "big",
) -> bytes:
    """把物理量按点表项声明的编码/字节序/字序拼回寄存器字节:decode_value 的逆运算。

    字序重排是自反的(两字节一块倒排,倒两次回到原样),所以读怎么拆、写就怎么拼,
    两边共用同一个 _by_word_order —— 不另写一套"反过来",也就不会一边改过另一边没改。
    返回的字节数就是 length:长度不是 2 的倍数的点(如 3 字节 BCD)由规约层补齐到整寄存器。
    """
    try:
        encode = ENCODERS[encoding]
    except KeyError:
        raise ValueError(
            f"写路径不支持编码 {encoding!r},可写编码: {sorted(ENCODERS)}"
        ) from None
    return _by_word_order(encode(value / scale, length, byteorder), word_order)
