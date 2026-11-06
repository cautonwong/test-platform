"""698 主站驱动的应答校验:SA / DIR / 数组 / 截断,四类都能变成串台读数。

驱动层以前只校验 PIID 与 OAD;一条来自别的从站、或方向位不对的迟到帧,
只要 PIID/OAD 形状相同就会被当成这台设备的读数。
"""
from types import SimpleNamespace

import pytest

from testplatform.drivers.dlt698_master_driver import (
    dlt698_reader,
    dlt698_reader_many,
)
from testplatform.protocol import dlt698
from testplatform.protocol.dlt698 import FrameError

OAD_VOLTAGE = 0x20000200
POINT = SimpleNamespace(key="电压", address="20000200", encoding="uint")
POINTS = {"电压": POINT}


class FakeTransport:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.sent: list[bytes] = []

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def receive(self, timeout=None, expected=None) -> bytes:
        return self.frame


def _frame(apdu: bytes, *, sa: bytes = b"\x01", ctrl: int = 0xC3) -> bytes:
    # 0xC3 = DIR=1 / PRM=1 / 功能码 3:标准 H.3.2 的响应帧头就是 C3。
    return dlt698.build_frame(apdu, sa=sa, ctrl=ctrl)


def test_read_many_accepts_a_normal_response():
    apdu = dlt698.get_response_list(1, [(OAD_VOLTAGE, bytes.fromhex("120028"))])
    transport = FakeTransport(_frame(apdu))

    assert dlt698_reader_many(transport, 1)(POINTS) == {"电压": bytes.fromhex("0028")}


def test_read_many_rejects_a_response_from_another_address():
    """共享总线上另一台表的应答不能当成本设备的读数。"""
    apdu = dlt698.get_response_list(1, [(OAD_VOLTAGE, bytes.fromhex("120028"))])
    transport = FakeTransport(_frame(apdu, sa=b"\x02"))

    with pytest.raises(ValueError, match="SA|地址|从站"):
        dlt698_reader_many(transport, 1)(POINTS)


def test_read_many_rejects_a_frame_without_the_response_direction_bit():
    """DIR=1 才是服务器发出;0x43 是客户机请求帧,不能当应答收下。"""
    apdu = dlt698.get_response_list(1, [(OAD_VOLTAGE, bytes.fromhex("120028"))])
    transport = FakeTransport(_frame(apdu, ctrl=0x43))

    with pytest.raises(ValueError, match="方向|DIR|响应"):
        dlt698_reader_many(transport, 1)(POINTS)


def test_read_many_refuses_to_take_one_element_out_of_an_array():
    apdu = dlt698.get_response_list(
        1, [(OAD_VOLTAGE, bytes.fromhex("01 03 12096D 12096D 12096D"))]
    )
    transport = FakeTransport(_frame(apdu))

    with pytest.raises(FrameError, match="数组|array|标量"):
        dlt698_reader_many(transport, 1)(POINTS)


def test_read_one_rejects_a_response_with_the_wrong_address():
    apdu = dlt698.get_response(1, OAD_VOLTAGE, bytes.fromhex("120028"))
    transport = FakeTransport(_frame(apdu, sa=b"\x02"))

    with pytest.raises(ValueError, match="SA|地址|从站"):
        dlt698_reader(transport, 1)(POINT)


def test_read_one_rejects_a_truncated_tail():
    apdu = dlt698.get_response(1, OAD_VOLTAGE, bytes.fromhex("120028"))
    transport = FakeTransport(_frame(apdu[:-1]))

    with pytest.raises(FrameError):
        dlt698_reader(transport, 1)(POINT)


def test_read_one_rejects_an_axdr_type_that_does_not_match_the_point_table():
    """uint 点收到一个 octet-string:字节数是 4,硬解会得到一个"看着正常"的数。"""
    apdu = dlt698.get_response(1, OAD_VOLTAGE, bytes.fromhex("09 04 DEADBEEF"))
    transport = FakeTransport(_frame(apdu))

    with pytest.raises(ValueError, match="类型标签|点表|A-XDR"):
        dlt698_reader(transport, 1)(POINT)


def test_read_one_rejects_an_unknown_axdr_type_instead_of_decoding_it_raw():
    apdu = dlt698.get_response(1, OAD_VOLTAGE, bytes.fromhex("04 02 00 01"))
    transport = FakeTransport(_frame(apdu))

    with pytest.raises(ValueError, match="A-XDR|类型标签"):
        dlt698_reader(transport, 1)(POINT)


def test_read_one_rejects_a_response_with_the_wrong_piid():
    apdu = dlt698.get_response(2, OAD_VOLTAGE, bytes.fromhex("120028"))
    transport = FakeTransport(_frame(apdu))

    with pytest.raises(ValueError, match="PIID"):
        dlt698_reader(transport, 1)(POINT)
