"""
周边设备也可以挂在 `via:` 链上(标准表经被测终端那条总线访问)。

`connect_support()` 只按角色挑设备,不管上游通没通 —— 上游没连上时它直接失败,
而失败消息还读成"这台周边设备坏了"。这三条钉住的是同一件事的三个面:

- 装配时先把上游连好,再连周边设备本身(断言实际连接顺序,不是"没报错");
- 上游连不上 → 测试无效,消息指向**上游没就绪**,不是设备本身故障;
- via 成环 → 加载期就报错,不许跑到一半(清单层已有环检测,这里钉住它别掉)。
"""
from __future__ import annotations

import copy

import pytest

from tests_support.manifest_factory import build, dlt645_bench

from testplatform.cases import case
from testplatform.composition.root import SupportEquipmentFault, SupportUpstreamNotReady

TERMINAL_ADDRESS = "000000000001"
METER_ADDRESS = "000000000002"

SIMULATED_645 = {
    TERMINAL_ADDRESS: {"02800085": "80510100"},
    METER_ADDRESS: {"02010100": "0022", "00010000": "00000000"},
}


def _bench_with_support_meter() -> dict:
    """一台被测终端 + 挂在它下面的标准表(role: support)。

    清单里**下游写在终端前面**:顺序不能靠书写顺序碰巧成立。
    """
    manifest = dlt645_bench(copy.deepcopy(SIMULATED_645), meters=["meter_a"])
    manifest["devices"][-1]["role"] = "support"
    manifest["devices"] = [manifest["devices"][-1], manifest["devices"][0]]
    return manifest


def _record_connects(root, device_ids):
    """把每台设备的 connect 记进一个列表,原方法照旧执行。"""
    order = []
    for device_id in device_ids:
        driver = root.get(device_id)
        connect = driver.connect
        driver.connect = lambda did=device_id, bound=connect: (order.append(did), bound())
    return order


@case(id="T_00_00_14_000000_01", clause="free: 平台:装配", tags=["装配"])
def test_connect_support_connects_the_upstream_before_the_support_device(tmp_path):
    root = build(tmp_path, _bench_with_support_meter())
    try:
        order = _record_connects(root, ("terminal_01", "meter_a"))

        root.connect_support()

        assert order == ["terminal_01", "meter_a"], f"连接顺序不对: {order}"
        assert root.get("terminal_01").device.state.name == "CONNECTED"
        assert root.get("meter_a").device.state.name == "CONNECTED"
    finally:
        root.teardown()


@case(id="T_00_00_14_000000_02", clause="free: 平台:装配", tags=["装配"])
def test_unready_upstream_is_a_test_invalid_and_names_the_upstream(tmp_path):
    """上游连不上:结论是测试无效,但麻烦在上游那条总线上,不是这块周边设备坏了。"""
    root = build(tmp_path, _bench_with_support_meter())

    def _refuse():
        raise ConnectionError("串口 /dev/ttyUSB2 打不开")

    root.get("terminal_01").connect = _refuse
    try:
        with pytest.raises(SupportEquipmentFault) as excinfo:
            root.connect_support()

        assert isinstance(excinfo.value, SupportUpstreamNotReady)
        assert excinfo.value.device_id == "meter_a"
        message = str(excinfo.value)
        assert "上游" in message, message
        assert "terminal_01" in message, message  # 指得出是哪条通路没通
    finally:
        root.teardown()


@case(id="T_00_00_14_000000_03", clause="free: 平台:装配", tags=["装配"])
def test_via_cycle_is_rejected_at_load_time_with_the_cycle_path(tmp_path):
    """A via B、B via A:装配前就停,消息里带完整环路径。"""
    manifest = dlt645_bench(copy.deepcopy(SIMULATED_645), meters=[])
    terminal = manifest["devices"][0]
    manifest["devices"] = [
        dict(terminal, id="term_a", via="term_b"),
        dict(terminal, id="term_b", via="term_a"),
    ]
    for spec in manifest["devices"]:
        spec.pop("connection", None)

    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, manifest)

    message = str(excinfo.value)
    assert "成环" in message, message
    assert "term_a → term_b → term_a" in message, message


def test_support_devices_may_not_use_another_support_device_as_their_upstream(tmp_path):
    """接入通路只能是被测设备(终端):周边设备当上游在加载期就拒 —— 那种"链"
    没有总线可借,排序也就无从谈起。"""
    manifest = dlt645_bench(copy.deepcopy(SIMULATED_645), meters=["meter_a", "meter_b"])
    manifest["devices"][1]["role"] = "support"
    manifest["devices"][2]["role"] = "support"
    manifest["devices"][2]["via"] = "meter_a"
    manifest["devices"][2].pop("connection", None)

    with pytest.raises(ValueError) as excinfo:
        build(tmp_path, manifest)

    assert "meter_a" in str(excinfo.value)
    assert "role" in str(excinfo.value)
