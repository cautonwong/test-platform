"""GET-Request/Response-NormalList（[2]）的字节级测试。

依据：DL/T 698.45 6.3.8.1.2 表66 / 6.3.8.2.2 表73。

    GetRequestNormalList  = SEQUENCE { PIID, SEQUENCE OF OAD }
    GetResponseNormalList = SEQUENCE { PIID-ACD, SEQUENCE OF A-ResultNormal }
    A-ResultNormal        = SEQUENCE { OAD, Get-Result }

附录 H 只给了 [1] Normal 的报文向量，没有 [2] 的。所以这里锁的是
**本仓库按 A-XDR 既有约定推出的字节布局**：拿到现场 [2] 报文后要回头核一次，
别把"自洽"当成"合标"。
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
    with pytest.raises(FrameError, match="还剩"):
        d.parse_get_response_list(good + b"\xaa\xbb\xcc")


def test_response_refuses_array_data_instead_of_guessing_a_length():
    """列表项里遇到 array/structure 时直接报错，不猜长度。

    猜错会把后续所有 OAD 的值错位，而帧校验依然通过。
    """
    with pytest.raises(FrameError, match="array/structure"):
        d.get_response_list(1, [(OAD_VOLTAGE, bytes.fromhex("01 01 12 0028"))])


def test_single_normal_response_still_parses():
    """[2] 的加入不能碰坏 [1] 的既有路径。"""
    apdu = bytes.fromhex("85 01 01 20000200 01 174367b333 00 00")
    res = d.parse_get_response(apdu)
    assert res.oad == OAD_VOLTAGE
    assert res.scalars() == [(0x17, bytes.fromhex("4367b333"))]
