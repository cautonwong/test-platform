"""
北向观测链路：broker + 传输 + 观察器。

## 这一层在证什么

它**不测设备**——它测的是「平台能不能可靠地看见北向消息」。
这是所有北向 E2E 的前提：观察链路本身不可靠，上面每条用例的结论都不可信，
而且错法很隐蔽——漏收一条，表现为"设备没发"。

所以这里先证明三件事：
1. 发出去的消息能被收到（连通）
2. 收到的东西能被正确解码（`points()`）
3. 订阅的主题空间是隔离的（`#` 不该收到别的主题树）

设备行为（断网补发、重连重发 BIRTH）等 DUT 壳接上之后再写，
那些用例会直接复用这三个 fixture。
"""
from __future__ import annotations

import pytest

from testplatform.protocol import mqtt_json


def test_published_point_is_received_and_decoded(mqtt_transport, northbound_probe):
    mqtt_transport.publish("edge/node1/NDATA", b'{"p":"VOLTAGE_A","v":230.5}', qos=1)

    msg = northbound_probe.wait_for(lambda m: b"VOLTAGE_A" in m.payload, timeout=5)

    assert msg.topic == "edge/node1/NDATA"
    assert northbound_probe.points()["VOLTAGE_A"] == pytest.approx(230.5)


def test_birth_is_counted_but_not_a_point(mqtt_transport, northbound_probe):
    """上线帧不是读数：`points()` 里不该出现它，`birth_count()` 要数到它。"""
    mqtt_transport.publish("edge/node1/NBIRTH", b'{"online":true,"ts":1}', qos=1)
    mqtt_transport.publish("edge/node1/NDATA", b'{"p":"SOC","v":55}', qos=1)

    northbound_probe.wait_count(2, timeout=5)

    assert northbound_probe.birth_count() == 1
    assert dict(northbound_probe.points()) == {"SOC": 55.0}


def test_message_order_is_preserved(mqtt_transport, northbound_probe):
    """顺序是断言点（补发要按时间序）。broker 同一条连接内保序，这里把它钉住。"""
    for i in range(3):
        mqtt_transport.publish("edge/node1/NDATA", f'{{"p":"SEQ","v":{i}}}'.encode(), qos=1)

    northbound_probe.wait_count(3, timeout=5)

    values = [mqtt_json.parse_downlink(m.payload).value for m in northbound_probe.messages]
    assert values == [0.0, 1.0, 2.0]


def test_probe_does_not_see_other_topic_trees(mqtt_broker, mqtt_transport):
    """订阅 `edge/#` 就不该收到 `spBv1.0/...`：主题空间隔离是"只看自己的"前提。

    这条**不能用 `northbound_probe` fixture**——它订阅的是 `#`（全部主题），
    当然会收到 `spBv1.0/...`。要验隔离就得自己指定主题。
    """
    from testplatform.transport.real_transports import RealMqttTransport
    from tests_support.northbound_probe import NorthboundProbe

    own = RealMqttTransport("127.0.0.1", mqtt_broker.port)
    probe = NorthboundProbe(own, "edge/#").start()
    try:
        mqtt_transport.publish("edge/node1/NDATA", b'{"p":"MINE","v":1}', qos=1)
        probe.wait_count(1, timeout=5)

        mqtt_transport.publish("spBv1.0/G/NDATA/N", b'{"p":"OTHER","v":2}', qos=1)

        # 只断言"没收到"不够——必须等到安静下来才说明确实没再发
        probe.wait_quiet(quiet_for=0.3, timeout=5)
        assert "OTHER" not in probe.points()
        assert "MINE" in probe.points()
    finally:
        probe.stop()


def test_garbage_payload_does_not_break_the_probe(mqtt_transport, northbound_probe):
    """设备发坏帧时观察器不该崩：它只是跳过，让用例去断言"这条不该出现"。"""
    mqtt_transport.publish("edge/node1/NDATA", b"not json", qos=1)
    mqtt_transport.publish("edge/node1/NDATA", b'{"p":"OK","v":1}', qos=1)

    northbound_probe.wait_count(2, timeout=5)

    assert dict(northbound_probe.points()) == {"OK": 1.0}
