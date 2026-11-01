"""
北向 E2E 的台架：一个真 broker + 一个观察器。

## 归因纪律

**broker 起不来 = 测试无效**（skip），不是被测设备不合格。
理由与 ADR-0009 的 `SupportUpstreamNotReady` 一致：接入通路不在，
设备的结论无从谈起。把"没装 mosquitto"读成"设备不推送"，会让人去查错地方。

## 为什么 broker 是 session 级

一个 broker 可以承载多条用例（主题按用例名区分即可）。每条用例重起一次
会拖慢一倍以上，而且更容易撞端口。
"""
from __future__ import annotations

import pytest

from testplatform.transport.real_transports import RealMqttTransport
from tests_support.mqtt_broker import BrokerUnavailable, MosquittoBroker
from tests_support.northbound_probe import NorthboundProbe


@pytest.fixture(scope="session")
def mqtt_broker():
    try:
        broker = MosquittoBroker.start()
    except BrokerUnavailable as exc:  # 台架缺件 → 测试无效，不是设备坏
        pytest.skip(str(exc))
    yield broker
    broker.stop()


@pytest.fixture()
def mqtt_transport(mqtt_broker):
    """一条独立的 MQTT 连接，用例用它扮演"云端下发方"。"""
    transport = RealMqttTransport("127.0.0.1", mqtt_broker.port)
    transport.connect()
    yield transport
    transport.disconnect()


@pytest.fixture()
def northbound_probe(mqtt_broker):
    """订阅全部主题的观察器，用例用它断言"设备发了什么"。"""
    transport = RealMqttTransport("127.0.0.1", mqtt_broker.port)
    probe = NorthboundProbe(transport, "#").start()
    yield probe
    probe.stop()
