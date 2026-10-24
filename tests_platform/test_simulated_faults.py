"""
模拟器故障注入(票 06):清单里声明的故障在装配期就定死,装错了当场报。

这里走的是清单 → 组合根 → 模拟器这条完整装配路,不是直接构造模拟器:
"清单键接上了没有"只有走这条路才证明得了。
"""
from __future__ import annotations

import pytest

from tests_support.manifest_factory import build, manifest

METER = "meter_01"


def _with_faults(faults):
    def _mutate(manifest_dict):
        manifest_dict["devices"][0]["connection"]["simulated_faults"] = faults

    return _mutate


def test_a_declared_timeout_fault_really_times_out(tmp_path):
    """清单写 `simulated_faults: {timeout: true}` → 这台表整轮不回答,读超时。"""
    root = build(tmp_path, manifest(meters=[METER], mutate=_with_faults({"timeout": True})))
    try:
        meter = root.get(METER)
        meter.connect()
        with pytest.raises(TimeoutError) as error:
            meter.read_all()
    finally:
        root.teardown()

    assert "timeout" in str(error.value)


def test_an_unknown_fault_kind_in_the_manifest_is_rejected_at_assembly(tmp_path):
    """清单里写了不存在的故障种类 → 装配期报错,并列出白名单。"""
    with pytest.raises(ValueError) as error:
        build(tmp_path, manifest(meters=[METER], mutate=_with_faults({"blow-up": True})))

    message = str(error.value)
    assert "blow-up" in message
    assert "bad-crc" in message and "link-lost" in message, f"报错没列可用种类:{message}"


def test_a_fault_the_simulator_cannot_produce_is_rejected_at_assembly(tmp_path):
    """MBAP 帧没有 CRC:清单给它声明 bad-crc 就是声明一个造不出来的能力,装配期报错。"""
    def _mbap_with_bad_crc(manifest_dict):
        manifest_dict["devices"][0]["connection"].update(
            simulated_frames="modbus_tcp", simulated_faults={"bad-crc": True}
        )

    with pytest.raises(ValueError) as error:
        build(tmp_path, manifest(meters=[METER], mutate=_mbap_with_bad_crc))

    message = str(error.value)
    assert "bad-crc" in message
    assert "timeout" in message, f"报错要列出这个模拟器实际支持什么:{message}"


def test_a_simulator_without_fault_support_rejects_a_fault_declaration(tmp_path):
    """文本模拟器造不出故障:清单里写 simulated_faults 必须装配期报错,不能静默忽略。"""
    def _text_with_timeout(manifest_dict):
        manifest_dict["devices"][0]["connection"] = {
            "simulated_response": "3000.0 230.0 50.0",
            "simulated_faults": {"timeout": True},
        }

    with pytest.raises(ValueError) as error:
        build(tmp_path, manifest(inverters=["inv_01"], mutate=_text_with_timeout))

    message = str(error.value)
    assert "不支持故障注入" in message
    assert "simulated_registers" in message, f"报错要指出该怎么改:{message}"
