"""Modbus 坏帧守卫:RTU 与 MBAP 的请求/应答校验。

这些分支平时不走,一旦走了就是"坏帧被当成正常应答"的入口。
"""
import pytest

from testplatform.protocol import modbus_rtu, modbus_tcp


def _rtu(body: bytes) -> bytes:
    crc = modbus_rtu.crc16_modbus(body)
    return body + bytes([crc & 0xFF, crc >> 8])


# ------------------------------------------------------------ RTU 请求 ----

def test_rtu_read_request_rejects_a_wrong_length_and_unknown_function():
    with pytest.raises(ValueError, match="8 字节"):
        modbus_rtu.parse_read_request(_rtu(bytes([1, 3, 0, 0, 0])))
    with pytest.raises(ValueError, match="不支持的功能码"):
        modbus_rtu.parse_read_request(_rtu(bytes([1, 6, 0, 0, 0, 1])))


def test_rtu_write_single_request_round_trips_and_rejects_bad_frames():
    frame = modbus_rtu.build_write_single_request(1, 7, 0x1234)
    assert modbus_rtu.parse_write_single_request(frame) == (1, 6, 7, 0x1234)

    with pytest.raises(ValueError, match="不支持的功能码"):
        modbus_rtu.parse_write_single_request(_rtu(bytes([1, 3, 0, 0, 0, 1])))
    with pytest.raises(ValueError, match="8 字节"):
        modbus_rtu.parse_write_single_request(_rtu(bytes([1, 6, 0, 0])))


def test_rtu_write_multiple_request_rejects_a_byte_count_mismatch():
    good = modbus_rtu.build_write_multiple_request(1, 7, b"\x00\x01\x00\x02")
    slave, function, address, count, payload = modbus_rtu.parse_write_multiple_request(good)
    assert (slave, function, address, count, payload) == (1, 16, 7, 2, b"\x00\x01\x00\x02")

    body = bytearray(good[:-2])
    body[6] = 5
    with pytest.raises(ValueError, match="声称"):
        modbus_rtu.parse_write_multiple_request(_rtu(bytes(body)))


# ------------------------------------------------------------ RTU 应答 ----

def test_rtu_write_single_response_rejects_wrong_length_and_slave():
    good = modbus_rtu.build_write_single_response(1, 7, 0x1234)
    assert modbus_rtu.parse_write_response(good, 1, 7) == 0x1234

    with pytest.raises(ValueError, match="8 字节"):
        modbus_rtu.parse_write_response(_rtu(bytes([1, 6, 0])), 1, 7)
    with pytest.raises(ValueError, match="从站地址"):
        modbus_rtu.parse_write_response(modbus_rtu.build_write_single_response(2, 7, 1), 1, 7)


def test_rtu_write_multiple_response_rejects_wrong_slave_and_function():
    good = modbus_rtu.build_write_multiple_response(1, 7, 2)
    modbus_rtu.parse_write_multiple_response(good, 1, 7, 2)

    body = bytearray(good[:-2])
    body[0] = 2
    with pytest.raises(ValueError, match="从站地址"):
        modbus_rtu.parse_write_multiple_response(_rtu(bytes(body)), 1, 7, 2)

    body = bytearray(good[:-2])
    body[1] = 3
    with pytest.raises(ValueError, match="功能码"):
        modbus_rtu.parse_write_multiple_response(_rtu(bytes(body)), 1, 7, 2)


def test_rtu_read_response_rejects_short_frames():
    with pytest.raises(ValueError, match="帧太短"):
        modbus_rtu.parse_read_response(b"\x01", 1)
    with pytest.raises(ValueError, match="应答帧太短"):
        modbus_rtu.parse_read_response(_rtu(bytes([1, 3])), 1)


def test_both_frames_reject_an_oversized_read_payload():
    with pytest.raises(ValueError, match="255"):
        modbus_rtu.build_read_response(1, b"\x00" * 256)
    with pytest.raises(ValueError, match="255"):
        modbus_tcp.build_read_response(1, 1, b"\x00" * 256)


# ----------------------------------------------------------- MBAP 校验 ----

def test_mbap_read_request_rejects_an_unknown_function():
    frame = modbus_tcp.build_write_single_request(1, 1, 7, 1)

    with pytest.raises(ValueError, match="不支持的功能码"):
        modbus_tcp.parse_read_request(frame)


def test_mbap_write_response_rejects_wrong_unit_function_and_length():
    good = modbus_tcp.build_write_single_request(1, 1, 7, 0x1234)
    assert modbus_tcp.parse_write_response(good, 1, 7, expected_transaction=1) == 0x1234

    bad_unit = bytearray(good)
    bad_unit[6] = 2
    with pytest.raises(ValueError, match="单元号"):
        modbus_tcp.parse_write_response(bytes(bad_unit), 1, 7, expected_transaction=1)

    bad_function = bytearray(good)
    bad_function[7] = modbus_tcp.READ_HOLDING_REGISTERS
    with pytest.raises(ValueError, match="功能码"):
        modbus_tcp.parse_write_response(bytes(bad_function), 1, 7, expected_transaction=1)

    with pytest.raises(ValueError, match="PDU"):
        modbus_tcp.parse_write_response(
            modbus_tcp.build_read_response(1, 1, b"\x00\x01"), 1, 7, expected_transaction=1
        )


def test_mbap_write_multiple_response_rejects_a_count_mismatch():
    good = modbus_tcp.build_write_multiple_response(1, 1, 7, 2)
    modbus_tcp.parse_write_multiple_response(good, 1, 7, 2, expected_transaction=1)

    with pytest.raises(ValueError, match="寄存器数"):
        modbus_tcp.parse_write_multiple_response(
            modbus_tcp.build_write_multiple_response(1, 1, 7, 3),
            1,
            7,
            2,
            expected_transaction=1,
        )


def test_mbap_write_multiple_request_rejects_a_byte_count_mismatch():
    good = modbus_tcp.build_write_multiple_request(1, 1, 7, b"\x00\x01\x00\x02")
    modbus_tcp.parse_write_multiple_request(good)

    body = bytearray(good)
    body[12] = 5  # 字节数说 5,实际 4
    with pytest.raises(ValueError, match="声称"):
        modbus_tcp.parse_write_multiple_request(bytes(body))


def test_rtu_foreign_exception_is_not_this_slave_rejection():
    frame = modbus_rtu.build_exception_response(2, 3, function=3)

    with pytest.raises(ValueError) as caught:
        modbus_rtu.parse_read_response(frame, slave=1, function=3)

    assert not isinstance(caught.value, modbus_rtu.SlaveExceptionResponse)
    assert "从站" in str(caught.value)


def test_tcp_foreign_exception_is_not_this_unit_rejection():
    frame = modbus_tcp.build_exception_response(1, 2, 3, function=3)

    with pytest.raises(ValueError) as caught:
        modbus_tcp.parse_read_response(
            frame, unit=1, function=3, expected_transaction=1
        )

    assert not isinstance(caught.value, modbus_rtu.SlaveExceptionResponse)
    assert "单元号" in str(caught.value)
