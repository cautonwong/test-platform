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


def test_get_response_requires_the_two_tail_bytes():
    """尾部缺 1 或 2 字节时,最后一个数据字节会被当成时间标签,解出短读数。"""
    good = bytes.fromhex("85 01 01 20000200 01 174367b333 00 00")
    dlt698.parse_get_response(good)
    for cut in (1, 2):
        with pytest.raises(dlt698.FrameError, match="尾部|时间标签|A-XDR"):
            dlt698.parse_get_response(good[:-cut])


def test_get_response_does_not_flatten_an_array_into_its_first_element():
    """数组 Data 是三相值,不是单值;把它拍平后取 [0] 会把 C 相静默成 A 相。"""
    apdu = bytes.fromhex("85 01 01 20000200 01 01 03 12096D 12096D 12096D 00 00")
    res = dlt698.parse_get_response(apdu)

    value = res.values()[0]
    assert value.tag == 0x01
    assert [child.payload for child in value.children] == [bytes.fromhex("096d")] * 3
    with pytest.raises(dlt698.FrameError, match="数组|array|标量"):
        res.scalars()


def test_action_response_requires_the_data_flag_and_two_tail_bytes():
    """ACTION 响应也必须带 Data 标志 + FollowReport + 时间标签。"""
    good = bytes.fromhex("8701050010010000000000")
    dlt698.parse_action_response(good)
    for cut in (1, 2):
        with pytest.raises(dlt698.FrameError, match="尾部|时间标签|Data"):
            dlt698.parse_action_response(good[:-cut])


def test_action_response_with_data_keeps_the_data_bytes():
    apdu = dlt698.action_response(1, 0x0010, 1, 0, data=bytes.fromhex("0F00"))
    res = dlt698.parse_action_response(apdu)
    assert res.data == bytes.fromhex("0F00")


def test_action_response_rejects_unknown_or_empty_data_flag():
    with pytest.raises(dlt698.FrameError, match="标志"):
        dlt698.parse_action_response(bytes.fromhex("87 01 01 0010 01 00 00 7F 00 00"))
    with pytest.raises(dlt698.FrameError, match="为空"):
        dlt698.parse_action_response(bytes.fromhex("87 01 01 0010 01 00 00 01 00 00"))


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x00", [(0x00, b"")]),  # NULL
        (b"\x03\x01", [(0x03, b"\x01")]),  # bool
        (b"\x01\x00", []),  # 空数组:没有元素
    ],
)
def test_get_response_decodes_scalars_and_empty_containers(data, expected):
    apdu = dlt698.get_response(1, 0x20000200, data)
    res = dlt698.parse_get_response(apdu)
    if data[0] == 0x01:
        assert res.values()[0].children == ()
    else:
        assert res.scalars() == expected


@pytest.mark.parametrize(
    "data",
    [
        b"\x7F",  # 不认识的类型
        b"\x12\x00",  # long-unsigned 少 1 字节
        b"\x09\x05\x01",  # octet-string 声明 5 字节,只有 1 字节
        b"\x01\x02\x12",  # 数组声明 2 个元素,只给半个
        b"\x03",  # bool 缺 1 字节
        b"\x09",  # 变长串缺长度字节
        b"\x01",  # 数组缺元素个数
    ],
)
def test_truncated_or_unknown_axdr_is_rejected(data):
    apdu = dlt698.get_response(1, 0x20000200, data)
    with pytest.raises(dlt698.FrameError):
        dlt698.parse_get_response(apdu)


@pytest.mark.parametrize(
    ("parser", "apdu", "message"),
    [
        (dlt698.parse_get_response, bytes.fromhex("85 01 01"), "太短"),
        (
            dlt698.parse_get_response,
            bytes.fromhex("84 01 01 20000200 01 120028 00 00"),
            "不是 GET-Response",
        ),
        (
            dlt698.parse_get_response,
            bytes.fromhex("85 02 01 20000200 01 120028 00 00"),
            r"\[1\]",
        ),
        (dlt698.parse_action_response, bytes.fromhex("87 01 01"), "太短"),
        (
            dlt698.parse_action_response,
            bytes.fromhex("86 01 01 0010 01 00 00 00 00 00"),
            "不是 ACTION-Response",
        ),
        (
            dlt698.parse_action_response,
            bytes.fromhex("87 02 01 0010 01 00 00 00 00 00"),
            r"\[1\]",
        ),
    ],
)
def test_response_parsers_reject_malformed_headers(parser, apdu, message):
    with pytest.raises(dlt698.FrameError, match=message):
        parser(apdu)


def test_get_response_rejects_more_than_one_data_value():
    apdu = dlt698.get_response(1, 0x20000200, b"\x00\x00")
    with pytest.raises(dlt698.FrameError, match="1 个 A-XDR"):
        dlt698.parse_get_response(apdu)


def test_build_frame_rejects_a_bad_address_or_empty_apdu():
    with pytest.raises(dlt698.FrameError, match="地址长度"):
        dlt698.build_frame(dlt698.get_request(1, 0x20000200), sa=b"")
    with pytest.raises(dlt698.FrameError, match="地址长度"):
        dlt698.build_frame(dlt698.get_request(1, 0x20000200), sa=b"\x01" * 17)
    with pytest.raises(dlt698.FrameError, match="APDU"):
        dlt698.build_frame(b"")


def test_build_frame_rejects_an_apdu_over_the_14_bit_length_limit():
    with pytest.raises(dlt698.FrameError, match="帧太长"):
        dlt698.build_frame(b"\x00" * 0x4000)


def test_parse_frame_rejects_short_bad_start_and_bad_end():
    good = dlt698.build_frame(dlt698.get_request(1, 0x20000200))
    with pytest.raises(dlt698.FrameError, match="太短"):
        dlt698.parse_frame(good[:11])
    bad_start = bytearray(good)
    bad_start[0] = 0x69
    with pytest.raises(dlt698.FrameError, match="起始符"):
        dlt698.parse_frame(bytes(bad_start))
    bad_end = bytearray(good)
    bad_end[-1] = 0x17
    with pytest.raises(dlt698.FrameError, match="结束符"):
        dlt698.parse_frame(bytes(bad_end))


def test_parse_frame_rejects_a_bad_hcs_even_when_the_fcs_is_recomputed():
    """FCS 对不代表帧头对:HCS 覆盖 L+C+SA+CA,单独校验。"""
    frame = bytearray(dlt698.build_frame(dlt698.get_request(1, 0x20000200)))
    frame[7] ^= 0xFF
    frame[-3:-1] = dlt698.fcs16(bytes(frame[1:-3])).to_bytes(2, "little")
    with pytest.raises(dlt698.FrameError, match="HCS"):
        dlt698.parse_frame(bytes(frame))


def test_nested_arrays_beyond_the_limit_raise_frame_error_not_recursion_error():
    data = b"\x01\x01" * 1000 + b"\x00"
    result = dlt698.GetResult(piid=1, oad=0x20000200, data=data)

    with pytest.raises(dlt698.FrameError, match="嵌套"):
        result.values()
