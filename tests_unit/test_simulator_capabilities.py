"""
模拟器能力声明(票 06):声明的故障必须真的造得出来,声明的种类必须是白名单里的。

"声明支持超时"这句话只有两种下场:真的能造出超时,或者把型号在报告里报成"可跑"而它其实跑不了。
所以这里既验声明本身,也验每一种声明的行为 —— 删掉一条声明,断言立刻红。
"""
from __future__ import annotations

import time

import pytest

from testplatform.protocol import modbus_rtu
from testplatform.transport.registry import (
    SIMULATOR_FAULT_KINDS,
    simulator_capabilities,
    simulator_profile,
)
from testplatform.transport.simulators import SimulatedModbusSlave

REGISTERS = {0: 0x0001, 1: 0xE240, 2: 2300}
READ_ENERGY = modbus_rtu.build_read_request(1, 0, 2)


def _slave(**kwargs) -> SimulatedModbusSlave:
    return SimulatedModbusSlave(dict(REGISTERS), **kwargs)


# ------------------------------------------------------------ 声明本身 ----

def test_the_slave_declares_exactly_the_faults_it_can_produce():
    """能力声明与实现一一对应:这四条删掉任何一条,这条断言就红(而不是悄悄少一个能力)。"""
    slave = _slave()

    assert slave.supported_faults == ("timeout", "slow", "bad-crc", "link-lost")
    assert set(slave.supported_faults) <= set(SIMULATOR_FAULT_KINDS)


def test_mbap_frames_do_not_claim_bad_crc():
    """MBAP 帧里没有 CRC:声明坏 CRC 就是编一个造不出来的能力。"""
    slave = _slave(frames="modbus_tcp")

    assert "bad-crc" not in slave.supported_faults
    assert set(slave.supported_faults) == {"timeout", "slow", "link-lost"}

    with pytest.raises(ValueError) as error:
        _slave(frames="modbus_tcp", faults={"bad-crc": True})
    message = str(error.value)
    assert "bad-crc" in message
    assert "timeout" in message and "slow" in message, f"报错要列出实际支持的种类:{message}"


def test_an_unknown_fault_kind_is_rejected_at_assembly_with_the_allowed_kinds():
    """清单里写了一个不存在的故障种类 → 装配期报错,并列出白名单(不是静默忽略)。"""
    with pytest.raises(ValueError) as error:
        _slave(faults={"blow-up": True})

    message = str(error.value)
    assert "blow-up" in message
    for kind in SIMULATOR_FAULT_KINDS:
        assert kind in message, f"白名单里少了 {kind}:{message}"


def test_a_slow_fault_must_be_given_seconds():
    with pytest.raises(ValueError) as error:
        _slave(faults={"slow": "soon"})
    assert "slow" in str(error.value)


# ------------------------------------------------------ 声明的故障是真的 ----

def test_the_timeout_fault_answers_nothing():
    slave = _slave(faults={"timeout": True})
    slave.connect()

    slave.send(READ_ENERGY)
    with pytest.raises(TimeoutError) as error:
        slave.receive(timeout=0.1, expected=9)
    assert "timeout" in str(error.value)


def test_the_link_lost_fault_drops_the_link():
    slave = _slave(faults={"link-lost": True})
    slave.connect()

    slave.send(READ_ENERGY)
    with pytest.raises(ConnectionError) as error:
        slave.receive(timeout=0.1, expected=9)
    assert "link-lost" in str(error.value)


def test_the_slow_fault_really_waits():
    """慢是"应答来得晚",不是"解析得慢":延迟必须量在收帧这一步上。"""
    slow = _slave(faults={"slow": 0.3})
    slow.connect()

    started = time.monotonic()
    slow.send(READ_ENERGY)
    answer = slow.receive(timeout=2, expected=None)
    waited = time.monotonic() - started

    # 门槛低于声明值:Windows 的 sleep 与 monotonic 各有粒度(实测 0.2s 的睡眠量到过 0.188s),
    # 这条断言要证明的是"真的等了",不是"等得准"。
    assert waited >= 0.2, f"声明了 slow: 0.3 却只等了 {waited:.3f}s"
    assert modbus_rtu.parse_read_response(answer, 1) == bytes.fromhex("0001e240")


def test_the_bad_crc_fault_breaks_the_frame_instead_of_the_length():
    good = _slave()
    broken = _slave(faults={"bad-crc": True})
    for slave in (good, broken):
        slave.connect()
        slave.send(READ_ENERGY)

    expected_frame = good.receive(timeout=1, expected=9)
    corrupted = broken.receive(timeout=1, expected=9)

    assert len(corrupted) == len(expected_frame), "坏的只能是 CRC,帧长度照旧"
    assert corrupted != expected_frame
    with pytest.raises(ValueError, match="CRC"):
        modbus_rtu.parse_read_response(corrupted, 1)


# ------------------------------------------------- 能力 → 型号的 CI 画像 ----

def test_simulator_profile_reports_faults_and_data_injection_for_a_model():
    """报告与目录读这一个入口:"这个型号在 CI 能跑吗、能注入哪些故障、能不能按点注入"。"""
    profile = simulator_profile("tcp", {"simulated_registers": dict(REGISTERS)})

    assert profile == {
        "runnable": True,
        "faults": ["timeout", "slow", "bad-crc", "link-lost"],
        "data_injection": True,
        "faults_are_static": True,
    }


def test_a_fixed_text_simulator_declares_no_faults_and_no_injection():
    """文本模拟器只回一行固定文本:它造不出这四类故障,也接不了按点注入 —— 如实报。"""
    profile = simulator_profile("tcp", {"simulated_response": "3000.0 230.0 50.0"})

    assert profile["runnable"] is True
    assert profile["faults"] == []
    assert profile["data_injection"] is False


def test_a_protocol_without_a_simulator_is_reported_as_not_runnable():
    """没有模拟器的接入方式不算错:如实报不可跑 + 原因(排任务时踩空比多一条记录难查)。"""
    profile = simulator_profile("no-such-access")

    assert profile["runnable"] is False
    assert "no-such-access" in profile["reason"]
    assert profile["faults"] == [] and profile["data_injection"] is False


def test_capabilities_are_read_from_the_object_not_a_second_table():
    """能力 = 实现:一个没声明的对象报空,不需要另外维护一份名单。"""
    assert simulator_capabilities(object()) == {
        "faults": [],
        "data_injection": False,
        "faults_are_static": True,
    }
