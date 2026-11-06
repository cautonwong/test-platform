"""GET-Request/Response-NormalList（[2]）的字节级测试。

依据：DL/T 698.45 6.3.8.1.2 表66 / 6.3.8.2.2 表73。

    GetRequestNormalList  = SEQUENCE { PIID, SEQUENCE OF OAD }
    GetResponseNormalList = SEQUENCE { PIID-ACD, SEQUENCE OF A-ResultNormal }
    A-ResultNormal        = SEQUENCE { OAD, Get-Result }

附录 H.3.2 给了 [2] 的完整请求与响应向量(响应里的 Data 是数组)，
所以字节布局与数组定界都按标准向量锁，不靠"自洽"推断。
"""
import pytest

from testplatform.protocol import dlt698 as d
from testplatform.protocol.dlt698 import FrameError

OAD_VOLTAGE = 0x20000200
OAD_COUNT = 0x60000200
FLOAT32_231P7 = bytes.fromhex("17 4367b333")  # tag 0x17 + IEEE754 大端
LONG_U_40 = bytes.fromhex("12 0028")  # tag 0x12 + 40


def test_request_layout_is_service_choice_piid_count_oads():
    req = d.get_request_list(0x01, [OAD_VOLTAGE, OAD_COUNT])
    assert req == bytes.fromhex("05 02 01 02 20000200 60000200 00")


def test_request_rejects_empty_or_too_many_oads():
    # 空请求会被从站判成坏帧，主站只能等超时 —— 不如在发出去之前拒绝
    with pytest.raises(ValueError):
        d.get_request_list(1, [])
    with pytest.raises(ValueError):
        d.get_request_list(1, list(range(256)))


def test_response_two_different_axdr_types():
    """两项的类型不同（float32 / long-unsigned）—— 定界错一格就会整体错位。

    这类错最阴：HCS/FCS 全对、帧合法，只有值是错的。
    """
    apdu = d.get_response_list(1, [(OAD_VOLTAGE, FLOAT32_231P7), (OAD_COUNT, LONG_U_40)])
    res = d.parse_get_response_list(apdu)

    assert res.piid == 1
    assert len(res.results) == 2
    assert res.results[0].oad == OAD_VOLTAGE
    assert res.results[1].oad == OAD_COUNT
    assert res.by_oad(OAD_VOLTAGE).scalars() == [(0x17, bytes.fromhex("4367b333"))]
    assert res.by_oad(OAD_COUNT).scalars() == [(0x12, bytes.fromhex("0028"))]


def test_response_with_dar_item_is_not_silently_dropped():
    """某一项读失败时**必须报错**，不能只把成功的项交出去。

    主站请求 2 个 OAD 只拿回 1 个读数，还判"通过"＝典型的假绿。
    flag=0x00 后面是 DAR=2（暂时失效）。
    """
    apdu = bytes.fromhex("85 02 01 02 20000200 00 02 60000200 01 120028 00 00")
    with pytest.raises(FrameError, match="DAR=2"):
        d.parse_get_response_list(apdu)


def test_response_rejects_wrong_choice_and_trailing_garbage():
    with pytest.raises(FrameError, match=r"\[2\]"):
        d.parse_get_response_list(bytes.fromhex("85 01 01 20000200 01 174367b333 00 00"))
    good = d.get_response_list(1, [(OAD_VOLTAGE, FLOAT32_231P7)])
    with pytest.raises(FrameError, match="尾部|还剩"):
        d.parse_get_response_list(good + b"\xaa\xbb\xcc")


def test_response_builder_accepts_array_data_and_round_trips_it():
    """列表项里的数组要能定界(标准 H.3.2 的响应就是数组 Data)。"""
    data = bytes.fromhex("01 03 12 0028 12 0029 12 002A")
    apdu = d.get_response_list(1, [(OAD_VOLTAGE, data)])
    res = d.parse_get_response_list(apdu)
    value = res.results[0].values()[0]
    assert value.tag == 0x01
    assert [child.payload for child in value.children] == [
        bytes.fromhex("0028"),
        bytes.fromhex("0029"),
        bytes.fromhex("002A"),
    ]


def test_single_normal_response_still_parses():
    """[2] 的加入不能碰坏 [1] 的既有路径。"""
    apdu = bytes.fromhex("85 01 01 20000200 01 174367b333 00 00")
    res = d.parse_get_response(apdu)
    assert res.oad == OAD_VOLTAGE
    assert res.scalars() == [(0x17, bytes.fromhex("4367b333"))]


# ---------------------------------------------------------------------------
# 标准附录 H.3.2 的 [2] 向量:两相电压/电流都是数组 Data。
# 之前这里写着"附录 H 没有 [2] 向量",是错的;仓库里的 698 原文有。
# ---------------------------------------------------------------------------

H32_REQUEST = bytes.fromhex("05 02 02 02 20000200 20010200 00")
H32_RESPONSE = bytes.fromhex(
    "85 02 02 02"
    " 20000200 01 01 03 12096D 12096D 12096D"
    " 20010200 01 01 03 05000003E8 05000003E8 05000003E8"
    " 00 00"
)


def test_standard_h32_request_vector_matches():
    assert d.get_request_list(0x02, [0x20000200, 0x20010200]) == H32_REQUEST


def test_standard_h32_response_parses_two_oads_of_three_element_arrays():
    """H.3.2:两个 OAD,各 3 个元素。定界错一格后面的 OAD 就整体错位。"""
    res = d.parse_get_response_list(H32_RESPONSE)

    assert [item.oad for item in res.results] == [0x20000200, 0x20010200]
    first = res.results[0].values()
    assert len(first) == 1
    assert first[0].tag == 0x01
    assert [child.tag for child in first[0].children] == [0x12, 0x12, 0x12]
    assert [child.payload for child in first[0].children] == [bytes.fromhex("096d")] * 3
    second = res.results[1].values()
    assert len(second) == 1
    assert [child.tag for child in second[0].children] == [0x05, 0x05, 0x05]
    assert [child.payload for child in second[0].children] == [bytes.fromhex("000003e8")] * 3


def test_an_array_item_followed_by_a_scalar_item_stays_aligned():
    apdu = bytes.fromhex(
        "85 02 01 02"
        " 20000200 01 01 03 12096D 12096D 12096D"
        " 60000200 01 120028"
        " 00 00"
    )
    res = d.parse_get_response_list(apdu)

    assert res.results[0].values()[0].tag == 0x01
    assert res.by_oad(OAD_COUNT).scalars() == [(0x12, bytes.fromhex("0028"))]


def test_scalar_only_api_refuses_to_pick_one_element_out_of_an_array():
    """数组不能被当成"第一个元素"交给主站 —— 那会把 C 相读数静默成 A 相。"""
    res = d.parse_get_response_list(H32_RESPONSE)
    with pytest.raises(FrameError, match="数组|array|标量"):
        res.results[0].scalars()


def test_missing_tail_bytes_are_rejected():
    """标准 APDU 尾部是 FollowReport + 时间标签两字节;缺一字节不是完整响应。"""
    good = d.get_response_list(1, [(OAD_VOLTAGE, FLOAT32_231P7)])
    for cut in (1, 2):
        with pytest.raises(FrameError, match="尾部|时间标签"):
            d.parse_get_response_list(good[:-cut])


def test_a_truncated_axdr_value_is_rejected_instead_of_read_as_a_shorter_value():
    """浮点 5 字节被截成 4 字节时,旧实现仍返回一个"看着正常"的值。"""
    good = d.get_response_list(1, [(OAD_VOLTAGE, FLOAT32_231P7)])
    with pytest.raises(FrameError):
        d.parse_get_response_list(good[:-3])


def test_response_builder_refuses_multiple_values_in_one_data():
    with pytest.raises(ValueError, match="1 个 A-XDR"):
        d.get_response_list(1, [(OAD_VOLTAGE, b"\x00\x00")])


def test_response_builder_refuses_an_empty_result_list():
    with pytest.raises(ValueError, match="结果个数"):
        d.get_response_list(1, [])


def test_result_lookup_refuses_an_oad_that_is_not_in_the_response():
    res = d.parse_get_response_list(d.get_response_list(1, [(OAD_VOLTAGE, LONG_U_40)]))
    with pytest.raises(KeyError, match="没有 OAD"):
        res.by_oad(OAD_COUNT)


@pytest.mark.parametrize(
    ("apdu", "message"),
    [
        (bytes.fromhex("85 02 01 00 00"), "太短"),
        (bytes.fromhex("85 02 01 00 00 00 00 00 00 00 00"), "个数是 0"),
        (bytes.fromhex("86 02 01 01 20000200 01 120028 00 00"), "不是 GET-Response"),
        (bytes.fromhex("85 01 01 01 20000200 01 120028 00 00"), r"\[2\]"),
        (bytes.fromhex("85 02 01 02 20000200 01 120028 00 00"), "截断"),
    ],
)
def test_list_parser_rejects_malformed_apdus(apdu, message):
    with pytest.raises(FrameError, match=message):
        d.parse_get_response_list(apdu)
