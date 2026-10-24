"""
DL/T645 数据域的 +0x33 转义:能编码的字节必须编得进去又解得回来。

数据域每字节加 0x33 上线,所以 0xCD 及以上的字节加完就过 0xFF —— 那不是"转义没实现",
是 DL/T645 数据域装不下这个值(bytes() 只会抛一句 `bytes must be in range(0, 256)`,
看不出问题出在数据域范围上)。这里钉住两条:
    0x00–0xCC 逐值 round-trip 相等
    0xCD / 0xFF 报范围错,消息里有字节值、上界、允许范围
"""
import pytest

from testplatform.protocol import dlt645

METER = "000000000001"
DI = "00010000"  # 正向有功总电能


def _round_trip(value: bytes) -> bytes:
    frame = dlt645.build_read_response(METER, DI, value)
    return dlt645.parse_read_response(frame, METER, DI)


def test_every_encodable_data_byte_round_trips():
    """0x00–0xCC 都在转义范围里,一个不落地编出去再解回来。"""
    value = bytes(range(dlt645.MAX_DATA_BYTE + 1))
    assert _round_trip(value) == value


@pytest.mark.parametrize("value", [b"\xcc", b"\xcd", b"\xff"], ids=["0xcc", "0xcd", "0xff"])
def test_boundary_bytes_are_encodable_or_rejected_as_range_error(value):
    if value[0] <= dlt645.MAX_DATA_BYTE:
        assert _round_trip(value) == value
        return
    with pytest.raises(ValueError) as excinfo:
        dlt645.build_read_response(METER, DI, value)
    message = str(excinfo.value)
    assert f"{value[0]:#04x}" in message  # 哪个字节
    assert f"{dlt645.MAX_DATA_BYTE:#04x}" in message  # 超了哪个上界
    assert "0x00-0xcc" in message.lower().replace("–", "-")  # 允许范围
    assert "bytes must be in range" not in message
