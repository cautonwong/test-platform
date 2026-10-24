"""
协议编解码层的单测:纯函数,不需要设备、不需要传输、不需要清单。

这是票 5a 唯一新增的测试缝。加它的理由只有一条:换算与字节序写错在报告里
表现为"读数差 100 倍",最难查,而这一层是纯函数,测试成本接近零。

向量里有两个来源:
- 标准 Modbus CRC-16 的已知向量(多项式 0x8005 反射、初值 0xFFFF、无最终异或)
- 上游 legacy 平台 common/checkCRC.py 里的校验变体(IBM / X25),
  它们只当向量用 —— 参数和标准 Modbus 不同,不能拿来当 Modbus 的校验实现
"""
import pytest

from testplatform.config.manifest import POINT_ENCODINGS, Point
from testplatform.protocol import (
    crc16,
    crc16_modbus,
    decode_value,
    dlt645,
    encode_value,
    modbus_rtu,
)
from testplatform.protocol.values import DECODERS, ENCODERS


# ============================== CRC ==============================

def test_modbus_crc_matches_the_known_vector():
    """01 03 00 00 00 0A 的 CRC 是 C5 CD:帧尾发的两个字节是 c5 cd,也就是 CRC 值 0xCDC5 低字节先发。"""
    frame_body = bytes.fromhex("01030000000A")

    crc = crc16_modbus(frame_body)

    assert crc == 0xCDC5
    assert crc.to_bytes(2, "little").hex() == "c5cd"  # 上线顺序


def test_modbus_crc_matches_the_check_value():
    """CRC-16/MODBUS 的标准校验值:ASCII "123456789" → 4B37。"""
    assert crc16_modbus(b"123456789") == 0x4B37


@pytest.mark.parametrize(
    "polynomial, init, xorout, expected",
    [
        # checkCRC.py 的 _CRC16_IBM:initCrc=0x0000、xorOut=0x0000
        pytest.param(0x8005, 0x0000, 0x0000, 0xBB3D, id="上游IBM变体"),
        # checkCRC.py 的 _CRC16_X25:多项式 0x1021、initCrc=0x0000、xorOut=0xFFFF
        pytest.param(0x1021, 0x0000, 0xFFFF, 0xDE76, id="上游X25变体"),
    ],
)
def test_upstream_crc_variants_are_only_parameter_changes(polynomial, init, xorout, expected):
    """上游的校验变体和标准 Modbus 是同一个反射式算法换参数。
    它们是测试向量,不是 Modbus 的实现:初值差 0xFFFF,所有真实设备都会校验失败。"""
    assert crc16(b"123456789", polynomial, init, xorout) == expected


# ========================== 点表编码解码 ==========================

@pytest.mark.parametrize(
    "raw_hex, encoding, scale, byteorder, expected",
    [
        pytest.param("0001E240", "uint", 0.01, "big", 1234.56, id="uint32大端带倍率"),
        pytest.param("E240", "uint", 1.0, "big", 57920.0, id="uint16无倍率"),
        pytest.param("FF9C", "int", 1.0, "big", -100.0, id="int16负值大端"),
        pytest.param("9CFF", "int", 1.0, "little", -100.0, id="int16负值小端"),
        pytest.param("34120000", "bcd", 0.01, "little", 12.34, id="bcd电表电量低字节先"),
        pytest.param("00001234", "bcd", 0.01, "big", 12.34, id="bcd大端"),
        pytest.param("43668000", "float32", 1.0, "big", 230.5, id="float32大端"),
        pytest.param("00806643", "float32", 1.0, "little", 230.5, id="float32小端"),
        pytest.param("20333230302e35", "ascii", 1.0, "big", 3200.5, id="ascii文本行"),  # b" 3200.5"
    ],
)
def test_decode_value(raw_hex, encoding, scale, byteorder, expected):
    assert decode_value(bytes.fromhex(raw_hex), encoding, scale, byteorder) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw_hex, encoding, scale, word_order, expected",
    [
        # 高字先:寄存器顺序就是字节顺序
        pytest.param("0001E240", "uint", 0.01, "big", 1234.56, id="uint32高字先"),
        # 低字先:同一串字节,字序一换就差了四个数量级
        pytest.param("0001E240", "uint", 0.01, "little", 37958451.21, id="uint32低字先"),
        pytest.param("E2400001", "uint", 0.01, "little", 1234.56, id="uint32低字先上线顺序"),
        pytest.param("43668000", "float32", 1.0, "big", 230.5, id="float32高字先"),
        pytest.param("80004366", "float32", 1.0, "little", 230.5, id="float32低字先"),
    ],
)
def test_decode_value_follows_word_order(raw_hex, encoding, scale, word_order, expected):
    """字的顺序与字内字节顺序是两条独立的轴:32 位值跨两个寄存器时四种组合都真实存在。"""
    raw = bytes.fromhex(raw_hex)

    assert decode_value(raw, encoding, scale, "big", word_order) == pytest.approx(expected)


def test_word_order_changes_the_value_of_the_same_bytes():
    """同一串字节,只换字序:两个结果必须不同 —— 只认 byteorder 的实现会解出前者。"""
    raw = bytes.fromhex("0001E240")

    high_word_first = decode_value(raw, "uint", 0.01, "big", "big")
    low_word_first = decode_value(raw, "uint", 0.01, "big", "little")

    assert high_word_first == pytest.approx(1234.56)
    assert low_word_first == pytest.approx(37958451.21)


def test_word_order_little_rejects_an_odd_byte_count():
    """字序按整寄存器重排:奇数字节说不出哪个是"低字",静默截断会得到假读数。"""
    with pytest.raises(ValueError) as excinfo:
        decode_value(bytes.fromhex("000102"), "uint", 1.0, "big", "little")

    assert "000102" in str(excinfo.value)


def test_bcd_rejects_non_decimal_nibbles():
    """0x0A–0x0F 不是十进制数字:静默当成数字会得到"看起来正常"的假读数。"""
    with pytest.raises(ValueError) as excinfo:
        decode_value(bytes.fromhex("3412AB00"), "bcd", 0.01, "little")

    assert "3412ab00" in str(excinfo.value)


def test_point_encodings_are_derived_from_the_decoders():
    """清单校验认哪些编码,必须就是解码器实现得起的那几种:两份手写集合迟早漂成
    "装配校验通过、运行时 ValueError"。加一种编码只改 DECODERS 一处。"""
    assert POINT_ENCODINGS == frozenset(DECODERS)


# ========================== Modbus RTU 帧 ==========================

def test_build_read_request_produces_a_valid_frame():
    """读保持寄存器请求帧:从站 1、起始寄存器 0、读 2 个寄存器。"""
    assert modbus_rtu.build_read_request(1, 0, 2).hex(" ") == "01 03 00 00 00 02 c4 0b"


def test_read_request_round_trips_through_the_slave_side():
    frame = modbus_rtu.build_read_request(7, 0x0010, 4, modbus_rtu.READ_INPUT_REGISTERS)

    assert modbus_rtu.parse_read_request(frame) == (7, 4, 0x0010, 4)


def test_build_read_response_produces_a_valid_frame():
    """应答帧拼装:从站 1、功能码 03、4 字节数据 0001E240 → 帧尾 CRC 是 e2 a3。"""
    frame = modbus_rtu.build_read_response(1, bytes.fromhex("0001E240"))

    assert frame.hex(" ") == "01 03 04 00 01 e2 40 e2 a3"


def test_point_entry_and_raw_bytes_are_the_same_fact_in_both_directions():
    """点表项 + 物理量 ↔ 帧,两个方向都对得上:电量 1234.56 kWh 这个点表项
    (地址 0、4 字节、uint、倍率 0.01)在链路上就是请求 01 03 00 00 00 02 c4 0b
    和应答 01 03 04 00 01 e2 40 e2 a3。"""
    point = Point(key="有功电量", address=0, length=4, encoding="uint", scale=0.01, unit="kWh")

    count = (point.length + 1) // 2
    assert modbus_rtu.build_read_request(1, point.address, count).hex(" ") == "01 03 00 00 00 02 c4 0b"

    frame = modbus_rtu.build_read_response(1, bytes.fromhex("0001E240"))
    payload = modbus_rtu.parse_read_response(frame, slave=1)
    decoded = decode_value(payload[: point.length], point.encoding, point.scale, point.byteorder)
    assert decoded == pytest.approx(1234.56)


def test_response_bytes_decode_to_the_physical_value():
    """给定应答帧的原始字节(点表项:地址 0、长度 4、uint、倍率 0.01),解出物理量。"""
    frame = modbus_rtu.build_read_response(1, bytes.fromhex("0001E240"))

    payload = modbus_rtu.parse_read_response(frame, slave=1)

    assert payload.hex() == "0001e240"
    assert decode_value(payload, "uint", 0.01, "big") == pytest.approx(1234.56)


def test_corrupted_crc_fails_loudly():
    """CRC 不对必须抛错:带着错帧往下解码只会得到"看起来正常"的假读数。"""
    frame = bytearray(modbus_rtu.build_read_response(1, bytes.fromhex("0001E240")))
    frame[-1] ^= 0xFF

    with pytest.raises(ValueError) as excinfo:
        modbus_rtu.parse_read_response(bytes(frame), slave=1)

    assert "CRC" in str(excinfo.value)


def test_response_from_the_wrong_slave_or_function_fails_loudly():
    """同一总线上别的从站的应答不能被当成本设备的读数。"""
    with pytest.raises(ValueError):
        modbus_rtu.parse_read_response(modbus_rtu.build_read_response(2, b"\x00\x01"), slave=1)
    with pytest.raises(ValueError):
        modbus_rtu.parse_read_response(
            modbus_rtu.build_read_response(1, b"\x00\x01", modbus_rtu.READ_INPUT_REGISTERS),
            slave=1,
            function=modbus_rtu.READ_HOLDING_REGISTERS,
        )


def test_slave_exception_response_is_raised_with_the_exception_code():
    """异常应答单独一个类型、把异常码带出来:上层要能把它和超时 / CRC 错分开
    (异常应答可以拆小重试,超时不行)。它仍是 ValueError,既有调用方不受影响。"""
    frame = modbus_rtu.build_exception_response(1, modbus_rtu.ILLEGAL_DATA_ADDRESS)

    with pytest.raises(ValueError) as excinfo:
        modbus_rtu.parse_read_response(frame, slave=1)

    assert isinstance(excinfo.value, modbus_rtu.SlaveExceptionResponse)
    assert excinfo.value.code == modbus_rtu.ILLEGAL_DATA_ADDRESS
    assert excinfo.value.function == modbus_rtu.READ_HOLDING_REGISTERS
    assert "异常码" in str(excinfo.value)
    # 异常应答固定 5 字节:收帧一方按 response_length 就能把整帧收完再交上来
    assert len(frame) == modbus_rtu.response_length(frame) == 5


def test_byte_count_mismatch_fails_loudly():
    body = bytes([1, modbus_rtu.READ_HOLDING_REGISTERS, 4]) + b"\x00\x01"  # 声称 4 字节,只给 2
    crc = crc16_modbus(body)

    with pytest.raises(ValueError) as excinfo:
        modbus_rtu.parse_read_response(body + bytes([crc & 0xFF, crc >> 8]), slave=1)

    assert "字节" in str(excinfo.value)


def test_build_write_single_request_produces_a_valid_frame():
    """写单个寄存器请求帧:从站 1、寄存器 0x0010、值 0x1601 → 8 字节逐字节比对。

    帧尾 CRC 是 47 AF,也就是 CRC 值 0xAF47 低字节先发。
    """
    frame = modbus_rtu.build_write_single_request(1, 0x0010, 0x1601)

    assert frame.hex(" ") == "01 06 00 10 16 01 47 af"
    assert len(frame) == modbus_rtu.WRITE_SINGLE_REGISTER_BYTES == 8


def test_write_request_round_trips_through_the_slave_side():
    frame = modbus_rtu.build_write_single_request(7, 4, 1601)

    assert modbus_rtu.parse_write_single_request(frame) == (7, 0x06, 4, 1601)


def test_write_response_is_an_echo_and_parse_gives_the_value_back():
    """0x06 的应答就是请求帧原样回显:解析回的就是写下去的那个值。"""
    request = modbus_rtu.build_write_single_request(1, 0x0010, 0x1601)

    assert modbus_rtu.parse_write_response(request, slave=1, address=0x0010) == 0x1601
    # 收帧一方靠 write_response_length 知道该收满 8 字节:0x06 应答的第三字节是地址高字节,
    # 不是读应答里的字节数。
    assert modbus_rtu.write_response_length(request) == 8


def test_write_response_echoing_another_address_fails_loudly():
    """同一总线上别的从站 / 别的寄存器的回声不能当成本次写入的确认。"""
    other_address = modbus_rtu.build_write_single_request(1, 0x0011, 0x1601)
    other_slave = modbus_rtu.build_write_single_request(2, 0x0010, 0x1601)

    with pytest.raises(ValueError) as address_error:
        modbus_rtu.parse_write_response(other_address, slave=1, address=0x0010)
    assert "回显的寄存器地址" in str(address_error.value)

    with pytest.raises(ValueError) as slave_error:
        modbus_rtu.parse_write_response(other_slave, slave=1, address=0x0010)
    assert "从站地址" in str(slave_error.value)


def test_write_rejected_by_the_slave_is_its_own_exception_type():
    """设备拒了这一写(异常应答)= SlaveExceptionResponse,与超时分开:
    一个是设备在回答、一个链路根本没回话,归因不同。"""
    frame = modbus_rtu.build_exception_response(
        1, modbus_rtu.ILLEGAL_DATA_VALUE, modbus_rtu.WRITE_SINGLE_REGISTER
    )

    with pytest.raises(modbus_rtu.SlaveExceptionResponse) as excinfo:
        modbus_rtu.parse_write_response(frame, slave=1, address=0x0010)

    assert excinfo.value.code == modbus_rtu.ILLEGAL_DATA_VALUE
    assert modbus_rtu.write_response_length(frame) == 5


def test_write_request_carries_the_function_code_and_crc_like_reads():
    """写帧的 CRC 与读帧同一套:改一字节就校验失败。"""
    frame = bytearray(modbus_rtu.build_write_single_request(1, 0x0010, 0x1601))
    frame[-1] ^= 0xFF

    with pytest.raises(ValueError) as excinfo:
        modbus_rtu.parse_write_response(bytes(frame), slave=1, address=0x0010)

    assert "CRC" in str(excinfo.value)


def test_build_write_multiple_request_produces_a_valid_frame():
    """0x10 写 2 个寄存器:从站 1、起始 0x0010、值 0x0001 / 0xE240 → 13 字节逐字节比对。

    比 0x06 多的就是寄存器数(2 字节)与字节数(1 字节)。
    """
    frame = modbus_rtu.build_write_multiple_request(1, 0x0010, bytes.fromhex("0001e240"))

    assert frame.hex(" ") == "01 10 00 10 00 02 04 00 01 e2 40 ea 33"


def test_write_multiple_request_round_trips_through_the_slave_side():
    frame = modbus_rtu.build_write_multiple_request(7, 4, bytes.fromhex("202601022359"))

    assert modbus_rtu.parse_write_multiple_request(frame) == (
        7,
        0x10,
        4,
        3,
        bytes.fromhex("202601022359"),
    )


def test_write_multiple_response_is_an_echo_without_a_data_section():
    """0x10 的正常应答只回显起始地址与寄存器数:8 字节,没有数据段。

    写应答是回显而不是数据 —— 值到底生效了没有要看回读,不在应答里。
    """
    frame = modbus_rtu.build_write_multiple_response(1, 0x0010, 2)

    assert frame.hex(" ") == "01 10 00 10 00 02 40 0d"
    assert len(frame) == modbus_rtu.write_response_length(frame) == 8
    assert modbus_rtu.parse_write_multiple_response(frame, slave=1, address=0x0010, count=2) is None


def test_write_multiple_response_echoing_another_count_or_address_fails_loudly():
    """回显的寄存器数少一个就意味着少写了一个寄存器:不能当成本次写入的确认。"""
    with pytest.raises(ValueError) as count_error:
        modbus_rtu.parse_write_multiple_response(
            modbus_rtu.build_write_multiple_response(1, 0x0010, 1), slave=1, address=0x0010, count=2
        )
    assert "回显的寄存器数" in str(count_error.value)

    with pytest.raises(ValueError) as address_error:
        modbus_rtu.parse_write_multiple_response(
            modbus_rtu.build_write_multiple_response(1, 0x0011, 2), slave=1, address=0x0010, count=2
        )
    assert "回显的起始地址" in str(address_error.value)


def test_write_multiple_rejected_by_the_slave_is_the_same_exception_type():
    """设备拒了这一写:异常应答 5 字节,与 0x06 同一个异常类型、同一个归因。"""
    frame = modbus_rtu.build_exception_response(
        1, modbus_rtu.ILLEGAL_DATA_ADDRESS, modbus_rtu.WRITE_MULTIPLE_REGISTERS
    )

    with pytest.raises(modbus_rtu.SlaveExceptionResponse) as excinfo:
        modbus_rtu.parse_write_multiple_response(frame, slave=1, address=0x0010, count=2)

    assert excinfo.value.code == modbus_rtu.ILLEGAL_DATA_ADDRESS
    assert modbus_rtu.write_response_length(frame) == 5


def test_write_multiple_request_refuses_data_that_is_not_whole_registers():
    """寄存器是 2 字节:奇数字节的请求写下去会把边界弄错一个字节,不猜。"""
    with pytest.raises(ValueError) as excinfo:
        modbus_rtu.build_write_multiple_request(1, 0x0010, b"\x12\x00\x00")
    assert "2 字节" in str(excinfo.value)

    with pytest.raises(ValueError):
        modbus_rtu.build_write_multiple_request(1, 0x0010, b"")


# ========================= 写方向的编码 =========================

# (物理量, encoding, length, scale, byteorder, word_order, 寄存器字节)
WRITE_VECTORS = [
    pytest.param(1601, "uint", 2, 1.0, "big", "big", "0641", id="uint16"),
    pytest.param(160.1, "uint", 2, 0.1, "big", "big", "0641", id="uint16带倍率"),
    pytest.param(1234.56, "uint", 4, 0.01, "big", "big", "0001e240", id="uint32高字先"),
    pytest.param(1234.56, "uint", 4, 0.01, "big", "little", "e2400001", id="uint32低字先"),
    pytest.param(-2, "int", 2, 1.0, "big", "big", "fffe", id="int16负数"),
    pytest.param(120000, "bcd", 3, 1.0, "big", "big", "120000", id="bcd密码3字节"),
    pytest.param(202601022359, "bcd", 6, 1.0, "big", "big", "202601022359", id="bcd时钟6字节"),
    pytest.param(12.34, "bcd", 4, 0.01, "little", "big", "34120000", id="bcd低字节先"),
    pytest.param(230.5, "float32", 4, 1.0, "big", "big", "43668000", id="float32大端"),
    pytest.param(230.5, "float32", 4, 1.0, "little", "big", "00806643", id="float32小端"),
    pytest.param(230.5, "float32", 4, 1.0, "big", "little", "80004366", id="float32低字先"),
]


@pytest.mark.parametrize("value, encoding, length, scale, byteorder, word_order, raw_hex", WRITE_VECTORS)
def test_encode_value_is_the_inverse_of_decode_value(
    value, encoding, length, scale, byteorder, word_order, raw_hex
):
    """读写互逆,两个方向都锁:值 → 字节、字节 → 值 → 字节。

    第二条是往返不动点:读出来的值原样写回去,寄存器字节完全一致。
    """
    raw = bytes.fromhex(raw_hex)

    assert encode_value(value, encoding, length, scale, byteorder, word_order) == raw
    read_back = decode_value(raw, encoding, scale, byteorder, word_order)
    assert encode_value(read_back, encoding, length, scale, byteorder, word_order) == raw


@pytest.mark.parametrize("value, encoding, length, scale, byteorder, word_order, raw_hex", WRITE_VECTORS)
def test_every_write_vector_decodes_to_the_value_it_was_encoded_from(
    value, encoding, length, scale, byteorder, word_order, raw_hex
):
    assert decode_value(bytes.fromhex(raw_hex), encoding, scale, byteorder, word_order) == pytest.approx(
        value
    )


def test_a_value_that_does_not_fit_its_length_is_refused_not_truncated():
    """2 字节写 70000:截断成 4464 是一个看着合理的值,设备会接受、回读也一致 —— 必须报错。"""
    with pytest.raises(ValueError) as excinfo:
        encode_value(70000, "uint", 2)

    message = str(excinfo.value)
    assert "70000" in message and "0–65535" in message
    assert "uint" in message and "length=2" in message


def test_signed_and_bcd_range_checks_name_their_own_range():
    with pytest.raises(ValueError) as int_error:
        encode_value(32768, "int", 2)
    assert "-32768–32767" in str(int_error.value)

    # 3 字节 BCD 只有 6 位十进制数字:1200000 是 7 位,放下不。
    with pytest.raises(ValueError) as bcd_error:
        encode_value(1200000, "bcd", 3)
    assert "0–999999" in str(bcd_error.value)


def test_bcd_pads_leading_zeros_and_ignores_how_the_string_was_written():
    """BCD 的值是十进制数:补几位由 length 决定,不由写进来的字符串字面决定。"""
    assert encode_value(120000, "bcd", 4) == bytes.fromhex("00120000")
    assert encode_value(12, "bcd", 3) == bytes.fromhex("000012")


def test_float32_beyond_its_range_fails_instead_of_becoming_inf():
    with pytest.raises(ValueError) as excinfo:
        encode_value(1e40, "float32", 4)

    assert "float32" in str(excinfo.value) and "1e+40" in str(excinfo.value)


def test_ascii_is_not_writable_and_says_so():
    """文本 token 什么字符算合法由设备定(补空格?补几个?),没有一条能通用的写规则。"""
    with pytest.raises(ValueError) as excinfo:
        encode_value(1600, "ascii", 8)

    assert "ascii" in str(excinfo.value) and "uint" in str(excinfo.value)


def test_write_encodings_are_the_numeric_ones_the_decoders_cover():
    """写方向只给数值编码:与 DECODERS 的差集就是 ascii 一个,不是另一份凭空的集合。"""
    assert frozenset(ENCODERS) == frozenset(DECODERS) - {"ascii"}


# ========================== DL/T645 帧 ==========================

# 上游 legacy 平台 common/dlt645.py 的透传命令示例(原样抄自那里的一行):
# 读表 000000000001 的「正向有功总电能」(数据标识 00010000)。
# C=11 读数据,L=04,数据域 33 33 34 33 减 0x33 得 00 00 01 00(低字节先发),
# CS 是从第一个 68 到数据域末字节的算术和 B3。
UPSTREAM_READ_REQUEST = "6801000000000068110433333433b316"


def test_dlt645_builds_the_upstream_read_request():
    """真实的请求帧:表地址 000000000001 + 数据标识 00010000 → 上游那条报文。"""
    frame = dlt645.build_read_request("000000000001", "00010000")

    assert frame.hex() == UPSTREAM_READ_REQUEST


def test_dlt645_parses_address_and_data_id_out_of_the_upstream_frame():
    """地址域 6 字节 BCD 低字节先发:01 00 00 00 00 00 解析回 000000000001。"""
    assert dlt645.parse_read_request(bytes.fromhex(UPSTREAM_READ_REQUEST)) == (
        "000000000001",
        "00010000",
    )


def test_dlt645_response_carries_the_reading_back_in_both_directions():
    """应答帧 编码与解码双向对得上:电量 1234.56 kWh 在线路上就是
    68 01 00 00 00 00 00 68 91 08 33 33 34 33 89 67 45 33 9F 16。

    C=91(11|80 正常应答),数据域 = 数据标识(00010000)+ 值(56 34 12 00,BCD 低字节先),
    每字节 +0x33;CS=9F 由本帧的算术和独立算出,和实际报文一致。
    """
    frame = dlt645.build_read_response("000000000001", "00010000", bytes.fromhex("56341200"))

    assert frame.hex(" ") == "68 01 00 00 00 00 00 68 91 08 33 33 34 33 89 67 45 33 9f 16"

    value = dlt645.parse_read_response(frame, "000000000001", "00010000")
    assert value.hex() == "56341200"
    assert decode_value(value, "bcd", 0.01, "little") == pytest.approx(1234.56)


def test_dlt645_rejects_a_corrupted_cs():
    """CS 不对必须抡错:带上这条帧往下解码只会得到"看起来正常"的假读数。"""
    frame = bytearray(dlt645.build_read_response("000000000001", "00010000", b"\x56\x34\x12\x00"))
    frame[-2] ^= 0xFF

    with pytest.raises(ValueError) as excinfo:
        dlt645.parse_read_response(bytes(frame), "000000000001", "00010000")

    assert "CS" in str(excinfo.value)


def test_dlt645_rejects_another_meter_or_another_data_id():
    """同一总线上别的表、别的数据项的应答都不能被当成本次读数。"""
    frame = dlt645.build_read_response("000000000002", "00010000", b"\x56\x34\x12\x00")
    with pytest.raises(ValueError) as address_error:
        dlt645.parse_read_response(frame, "000000000001", "00010000")
    assert "000000000002" in str(address_error.value)

    with pytest.raises(ValueError) as data_id_error:
        dlt645.parse_read_response(frame, "000000000002", "02010100")
    assert "02010100" in str(data_id_error.value)


def test_dlt645_exception_response_names_the_error_bit():
    """表回异常应答(C=D1)时把错误信息字说出来,而不是当成数据解码。

    68 01 00 00 00 00 00 68 D1 01 04 A7 16:C=D1(11|80|40 异常应答)。
    """
    frame = bytes.fromhex("6801000000000068d10104a716")

    with pytest.raises(ValueError) as excinfo:
        dlt645.parse_read_response(frame, "000000000001", "00010000")

    assert "异常应答" in str(excinfo.value)


def test_dlt645_rejects_malformed_addresses_and_data_ids():
    """表地址与数据标识的写法是清单里最容易写错的地方,错了要当场报出期望的写法。"""
    with pytest.raises(ValueError, match="12 位 BCD"):
        dlt645.build_read_request("1", "00010000")
    with pytest.raises(ValueError, match="8 位十六进制"):
        dlt645.build_read_request("000000000001", 0x00010000)
