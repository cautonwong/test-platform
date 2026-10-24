"""
Modbus TCP 的字节向量:MBAP 头 + PDU,与 RTU 的差别只在帧头,PDU 逐字节相同。

这一层是纯函数,测试成本接近零,而写错的表现最难查:长度字段算错 = 收帧一方永远收不满
(或者多解出几个字节的假数据),协议号不校验 = 把 RTU 帧当 MBAP 帧解出一帧"看着正常"的帧。
所以每种帧都钉一对向量:拼出来的字节逐字节对得上、解析回来与拼进去的字段一致。

向量来源:Modbus Application Protocol Specification V1.1b3 的 MBAP 例帧(事务号 / 单元号
取 1 与 1),PDU 与 Modbus RTU 的同一功能码相同 —— 这一点本身就是"两种帧只差帧头"的证据。
"""
import pytest

from testplatform.protocol import crc16_modbus, modbus_rtu, modbus_tcp

# 事务号 1、单元号 1、协议号 0。
READ_REQUEST = "00010000000601030000000a"  # 读 0 起的 10 个保持寄存器
READ_RESPONSE = "00010000000701030400050006"  # 4 字节数据:0x0005、0x0006
WRITE_SINGLE_REQUEST = "000100000006010600010003"  # 地址 1 写 0x0003
EXCEPTION_RESPONSE = "000100000003018302"  # 0x83 = 03|0x80,异常码 02 非法数据地址


# ============================== 字节向量 ==============================

def test_read_request_vector():
    """读请求:7 字节头(长度 6 = 单元号 1 + PDU 5)+ 功能码 / 地址 / 个数。"""
    frame = bytes.fromhex(READ_REQUEST)

    assert modbus_tcp.build_read_request(1, 1, 0, 10) == frame
    assert modbus_tcp.parse_read_request(frame) == (1, 1, 0x03, 0, 10)


def test_read_response_vector():
    """读应答:长度 7 = 单元号 1 + (功能码 1 + 字节数 1 + 数据 4)。"""
    frame = bytes.fromhex(READ_RESPONSE)

    assert modbus_tcp.build_read_response(1, 1, bytes.fromhex("00050006")) == frame
    assert modbus_tcp.parse_read_response(frame, 1, expected_transaction=1) == bytes.fromhex(
        "00050006"
    )


def test_write_single_request_vector_and_its_echo_response():
    """写单个寄存器:应答就是请求帧原样回显(除了事务号不变,一个字都不差)。"""
    frame = bytes.fromhex(WRITE_SINGLE_REQUEST)

    assert modbus_tcp.build_write_single_request(1, 1, 1, 3) == frame
    assert modbus_tcp.parse_write_single_request(frame) == (1, 1, 0x06, 1, 3)
    assert modbus_tcp.parse_write_response(frame, 1, 1, expected_transaction=1) == 3


def test_exception_response_vector():
    """异常应答:长度 3(单元号 + 功能码|0x80 + 异常码),没有数据段。"""
    frame = bytes.fromhex(EXCEPTION_RESPONSE)

    assert modbus_tcp.build_exception_response(1, 1, modbus_rtu.ILLEGAL_DATA_ADDRESS) == frame
    # 从站拒绝与 RTU 是同一个异常类型:上层按它降级拆小重试,不按帧格式分叉。
    with pytest.raises(modbus_rtu.SlaveExceptionResponse) as caught:
        modbus_tcp.parse_read_response(frame, 1, expected_transaction=1)
    assert caught.value.code == modbus_rtu.ILLEGAL_DATA_ADDRESS


def test_write_multiple_request_and_its_echo_response():
    """写多个寄存器:请求带寄存器数与数据,应答只回显起始地址与寄存器数(长度 6)。"""
    request = modbus_tcp.build_write_multiple_request(2, 1, 0, bytes.fromhex("000a000b"))

    assert request.hex() == "00020000000b01100000000204000a000b"
    assert modbus_tcp.parse_write_multiple_request(request) == (
        2,
        1,
        0x10,
        0,
        2,
        bytes.fromhex("000a000b"),
    )
    response = modbus_tcp.build_write_multiple_response(2, 1, 0, 2)
    assert response.hex() == "000200000006011000000002"
    assert (
        modbus_tcp.parse_write_multiple_response(response, 1, 0, 2, expected_transaction=2) is None
    )


# ========================= 收帧边界:长度字段 =========================

@pytest.mark.parametrize(
    "frame_hex, expected",
    [
        (READ_REQUEST, 12),  # 6 + 长度(6)
        (EXCEPTION_RESPONSE, 9),  # 6 + 长度(3)
        (READ_RESPONSE, 13),  # 6 + 长度(7)
    ],
)
def test_response_length_reads_the_frame_out_of_the_mbap_header(frame_hex, expected):
    """整帧长度 = 6 + 长度字段:读 / 写 / 异常应答同一条算路,不像 RTU 要按功能码分两套。"""
    assert modbus_tcp.response_length(bytes.fromhex(frame_hex)) == expected


def test_response_length_says_nothing_until_the_header_is_complete():
    """头还不到 7 字节时返回 None:半帧解出的假读数比报错难查。"""
    assert modbus_tcp.response_length(bytes.fromhex("0001000000")) is None


# ============================ 校验必须红 ============================

def test_a_wrong_length_field_is_refused():
    """长度字段说 6、实际 PDU 6 字节(应为 7):宁可报错,也不能按假长度切字节。"""
    broken = bytes.fromhex(READ_RESPONSE)
    assert broken[5] == 0x07
    wrong = broken[:5] + b"\x06" + broken[6:]

    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(wrong, 1, expected_transaction=1)

    message = str(caught.value)
    assert "长度字段" in message
    assert "6" in message and "7" in message  # 声称几字节、实际几字节都要说清


def test_a_non_zero_protocol_id_is_refused():
    """协议号恒 0:非 0 说明这串字节不是 MBAP 帧,不许往下解。"""
    broken = bytes.fromhex(READ_RESPONSE)
    wrong = broken[:2] + b"\x00\x01" + broken[4:]

    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(wrong, 1, expected_transaction=1)

    assert "协议号" in str(caught.value)


@pytest.mark.parametrize(
    "rtu_frame",
    [
        pytest.param(modbus_rtu.build_read_request(1, 0, 10), id="读请求"),
        pytest.param(modbus_rtu.build_read_response(1, bytes.fromhex("00050006")), id="读应答"),
    ],
)
def test_an_rtu_frame_fed_to_mbap_parsing_is_named_as_such(rtu_frame):
    """帧格式错配:RTU 帧没有 7 字节头,不许解出一个看着正常的值 —— 而且要指名是 RTU 帧。

    没有 CRC 的 MBAP 帧与带 CRC 的 RTU 帧长得毫不相干:少 7 个字节,从站地址会落到事务号上、
    寄存器地址会落到协议号上。所以这里断言两件事:抛错(不静默解出值)、消息里点明是 RTU 帧。
    """
    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(rtu_frame, 1, expected_transaction=None)

    message = str(caught.value)
    assert "RTU" in message
    assert modbus_rtu.crc16_modbus(rtu_frame[:-2]).to_bytes(2, "little") == rtu_frame[-2:]


def test_mbap_parsing_refuses_a_frame_shorter_than_its_header():
    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(bytes.fromhex("0001000000"), 1, expected_transaction=None)

    assert "MBAP" in str(caught.value)


def test_a_response_for_another_unit_or_function_is_refused():
    """应答必须是这一问的回答:单元号 / 功能码对不上一律抛错,不做兼容猜测。"""
    frame = bytes.fromhex(READ_RESPONSE)

    with pytest.raises(ValueError) as wrong_unit:
        modbus_tcp.parse_read_response(frame, 2, expected_transaction=1)
    assert "单元号" in str(wrong_unit.value)

    with pytest.raises(ValueError) as wrong_function:
        modbus_tcp.parse_read_response(
            frame, 1, modbus_rtu.READ_INPUT_REGISTERS, expected_transaction=1
        )
    assert "功能码" in str(wrong_function.value)


def test_a_byte_count_that_disagrees_with_the_data_is_refused():
    """字节数说 4、实际给了 2 字节:一样的口径,宁可报错。"""
    wrong = bytes.fromhex("0001000000050103040005")  # 长度 5 = 单元号 + 03 04 00 05

    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(wrong, 1, expected_transaction=1)

    assert "字节" in str(caught.value)


# ======================= 省略事务号 = 不可能 =======================

@pytest.mark.parametrize(
    "parse, args",
    [
        pytest.param(
            modbus_tcp.parse_read_response, (bytes.fromhex(READ_RESPONSE), 1), id="读应答"
        ),
        pytest.param(
            modbus_tcp.parse_write_response,
            (bytes.fromhex(WRITE_SINGLE_REQUEST), 1, 1),
            id="写单个寄存器应答",
        ),
        pytest.param(
            modbus_tcp.parse_write_multiple_response,
            (bytes.fromhex("000200000006011000000002"), 1, 0, 2),
            id="写多个寄存器应答",
        ),
    ],
)
def test_omitting_the_expected_transaction_is_a_type_error(parse, args):
    """"不传就不校验"是静默失效:迟到的应答会被当成这次读数。所以它是 keyword-only 必填。

    没有期望值的调用方显式传 None —— 省略这个参数在调用处就红,而不是现场红在错读数上。
    """
    with pytest.raises(TypeError) as caught:
        parse(*args)

    assert "expected_transaction" in str(caught.value)


def test_a_write_echo_for_another_address_is_refused():
    """写应答校验"回的确实是刚才那一帧":回显地址不对就是错的帧。"""
    frame = bytes.fromhex(WRITE_SINGLE_REQUEST)

    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_write_response(frame, 1, 2, expected_transaction=1)

    assert "回显" in str(caught.value)


def test_read_builders_refuse_a_write_function_code():
    """读请求只收 03/04:拿 0x06 拼一个"读请求"是调用方的错,不许悄悄发出去。"""
    with pytest.raises(ValueError) as caught:
        modbus_tcp.build_read_request(1, 1, 0, 1, modbus_rtu.WRITE_SINGLE_REGISTER)

    assert "03/04" in str(caught.value)


def test_the_mbap_pdu_is_the_rtu_pdu_without_its_crc():
    """两种帧只差帧头:同一个请求的 PDU 逐字节相同,RTU 只是尾部多两个 CRC。"""
    rtu = modbus_rtu.build_read_request(1, 0, 10)
    mbap = modbus_tcp.build_read_request(1, 1, 0, 10)

    assert mbap[modbus_tcp.MBAP_HEADER_BYTES:] == rtu[1:-2]
    assert crc16_modbus(rtu[:-2]).to_bytes(2, "little") == rtu[-2:]
