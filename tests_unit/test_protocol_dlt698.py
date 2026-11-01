"""
698 编解码的黄金向量测试。

向量全部抄自 Q/GDW《面向对象的用电信息数据交换协议》附录 H
（原文 `/workspaces/edgeos/EdgeC/docs/698_parsed.md`）。
用标准字节当期望值，而不是用本实现输出反推 —— 后者只能证明"代码没变"。
"""
import pytest

from testplatform.protocol import dlt698


def test_get_request_matches_standard_example():
    """附录 H.3.6：读通信地址的 GET-Request 明文段是 05 01 01 40 01 02 00 00。"""
    assert dlt698.get_request(0x01, 0x40010200).hex() == "0501014001020000"


def test_action_request_matches_standard_example():
    """附录 H.5.1：执行电能量复位方法 = 07 01 05 00 10 01 00 0F 00 00。"""
    apdu = dlt698.action_request(0x05, 0x0010, 0x01, param=bytes.fromhex("0F00"))
    assert apdu.hex() == "070105001001000f0000"


def test_action_request_rejects_zero_method():
    """方法标识取值 1…255（标准 6.3.3.12）；0 不能编出一帧"看起来正常"的报文。"""
    with pytest.raises(dlt698.FrameError, match="方法标识"):
        dlt698.action_request(0x01, 0x0010, 0x00)


def test_parse_action_response_success():
    """附录 H.5.1 的响应：87 01 05 00 10 01 00 00 00 00 00。"""
    res = dlt698.parse_action_response(bytes.fromhex("8701050010010000000000"))
    assert res.piid == 0x05
    assert (res.oi, res.method, res.mode) == (0x0010, 0x01, 0x00)
    assert res.dar == 0 and res.ok and res.data == b""


def test_parse_action_response_failure_is_visible():
    """执行失败必须能读出来：DAR=15 是"密码错/未授权"。

    这条是**故意红过的**：把 DAR 当无关字节忽略时，设备拒绝执行也会显示成功，
    而主站会据此认为继电器动了 —— 最危险的一类缺陷。
    """
    res = dlt698.parse_action_response(bytes.fromhex("870107800001000f000000"))
    assert not res.ok
    assert res.dar == 15
    assert "密码" in res.error


def test_parse_get_response_octet_string():
    """附录 H.3.6 的响应：85 01 01 40 01 02 00 01 09 06 201601290001 00 00。"""
    apdu = bytes.fromhex("850101400102000109062016012900010000")
    res = dlt698.parse_get_response(apdu)
    assert res.piid == 0x01
    assert res.oad == 0x40010200
    scalars = res.scalars()
    assert scalars == [(0x09, bytes.fromhex("201601290001"))]


def test_parse_get_response_failure_raises():
    """读失败时 flag=0x00，后面跟 DAR。把它当数据会读出垃圾值。"""
    apdu = bytes.fromhex("8501014001020000060000")
    with pytest.raises(dlt698.FrameError, match="DAR=6"):
        dlt698.parse_get_response(apdu)


def test_frame_round_trip_and_length_rule():
    """L = 帧长 − 2（标准 5.1.2）。附录 H.1 的登录帧 32 字节、L=30 就是这个规则。"""
    apdu = dlt698.action_request(0x05, 0x0010, 0x01, param=bytes.fromhex("0F00"))
    frame = dlt698.build_frame(apdu)

    assert frame[0] == 0x68 and frame[-1] == 0x16
    l_field = int.from_bytes(frame[1:3], "little")
    assert l_field == len(frame) - 2

    parsed = dlt698.parse_frame(frame)
    assert parsed.apdu == apdu
    assert parsed.sa == b"\x01"


def test_parse_frame_rejects_corrupted_fcs():
    """FCS 不对就必须报错，不能把坏帧当好消息交给上层。"""
    frame = bytearray(dlt698.build_frame(dlt698.get_request(0x01, 0x40010200)))
    frame[-2] ^= 0xFF
    with pytest.raises(dlt698.FrameError, match="FCS"):
        dlt698.parse_frame(bytes(frame))


def test_parse_frame_rejects_wrong_length_field():
    """长度域与帧长不符时报错：对端按 L 取数据，错了就是解析错位。"""
    frame = bytearray(dlt698.build_frame(dlt698.get_request(0x01, 0x40010200)))
    frame[1] = (frame[1] + 1) & 0xFF
    with pytest.raises(dlt698.FrameError, match="长度域"):
        dlt698.parse_frame(bytes(frame))


def test_response_length_comes_from_the_length_field():
    """整帧 = L + 2（L 不含起始/结束字符，标准 5.1.2）。

    收帧的一方靠它知道该收多少。算错一格的现象是"半帧"或"多吃下一帧的头"，
    解出来的都是假读数。
    """
    frame = bytes.fromhex("68190003000110d8bc8501012000020001174367b333000075de16")
    assert dlt698.response_length(frame) == len(frame)


def test_response_length_returns_none_until_length_field_arrives():
    """头还没收全时不能瞎猜长度，否则收帧逻辑会按垃圾值去凑字节。"""
    assert dlt698.response_length(b"\x68") is None
    assert dlt698.response_length(b"\x68\x19") is None
