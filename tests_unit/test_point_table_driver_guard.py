"""点表解码的短帧守卫:应答比点表声明的 length 短时,不能解出一个"看着正常"的数字。"""
from types import SimpleNamespace

import pytest

from testplatform.drivers.point_table_driver import PointTableDriver


def _driver(raw: bytes, *, length: int = 4) -> PointTableDriver:
    point = SimpleNamespace(
        key="电压",
        address=0,
        encoding="uint",
        length=length,
        scale=1.0,
        byteorder="big",
        word_order="big",
    )
    device = SimpleNamespace(ensure_connected=lambda: None)
    model = SimpleNamespace(name="m", point_table={"电压": point})
    return PointTableDriver(device, model, None, lambda _point: raw)


def test_a_short_payload_is_rejected_instead_of_decoded():
    """4 字节点只收到 1 字节:旧实现返回 1.0,现场看不出这是半帧。"""
    with pytest.raises(ValueError, match="长度|字节|短"):
        _driver(b"\x01").read("电压")


def test_register_padding_after_the_declared_length_is_still_accepted():
    """规约按整寄存器应答:3 字节 BCD 点会收到 4 字节,多出来的补齐字节不归它。"""
    assert _driver(bytes.fromhex("00000005ffff")).read("电压") == 5.0
