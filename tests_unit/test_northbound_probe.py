"""
北向观察器的单测：纯逻辑，不需要 broker、不需要设备。

## 为什么这些用例值得写

观察器是**所有北向 E2E 断言的公共底座**。它错一次，上面所有用例的结论都不可信 ——
而且错法很隐蔽：漏收一条消息，表现为"设备没发"；`wait_quiet` 判定太松，
表现为"重复发送"这类不存在的缺陷。

所以这里锁的是观察器自己的语义，不是设备行为。
"""
import pytest

from testplatform.protocol.mqtt_json import MqttJsonFormatError
from tests_support.northbound_probe import NorthboundProbe, Observed, ProbeTimeout


class FakeTransport:
    """可控的订阅传输：测试自己决定什么时候"收到"消息。"""

    def __init__(self) -> None:
        self.connected = 0
        self.disconnected = 0
        self.topic = None
        self._handler = None

    def connect(self) -> None:
        self.connected += 1

    def disconnect(self) -> None:
        self.disconnected += 1

    def subscribe(self, topic, handler) -> None:
        self.topic = topic
        self._handler = handler

    def deliver(self, topic: str, payload: bytes) -> None:
        assert self._handler is not None, "还没 subscribe"
        self._handler(topic, payload)


# ============================== 生命周期 ==============================

def test_start_connects_and_subscribes():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#")
    probe.start()
    assert t.connected == 1
    assert t.topic == "edge/#"


def test_start_is_idempotent():
    """重复 start 不该重复连（否则每调一次就多一个连接）。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#")
    probe.start()
    probe.start()
    assert t.connected == 1


def test_stop_disconnects_once():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    probe.stop()
    probe.stop()
    assert t.disconnected == 1


def test_context_manager_starts_and_stops():
    t = FakeTransport()
    with NorthboundProbe(t, "edge/#") as probe:
        assert t.connected == 1
        t.deliver("edge/n/NCMD", b'{"p":"X","v":1}')
        assert len(probe.messages) == 1
    assert t.disconnected == 1


def test_empty_topic_is_rejected():
    with pytest.raises(ValueError):
        NorthboundProbe(FakeTransport(), "")


# ============================== 收集 ==============================

def test_messages_preserve_order_and_payload():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')
    t.deliver("edge/n/NDATA", b'{"p":"B","v":2}')

    assert [m.payload for m in probe.messages] == [b'{"p":"A","v":1}', b'{"p":"B","v":2}']
    assert all(isinstance(m, Observed) for m in probe.messages)


def test_messages_snapshot_is_not_live():
    """返回快照：调用方拿着它做断言时，后面再来的消息不该悄悄改变它。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')
    snapshot = probe.messages
    t.deliver("edge/n/NDATA", b'{"p":"B","v":2}')
    assert len(snapshot) == 1


def test_clear_drops_previous_messages():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')
    probe.clear()
    assert probe.messages == ()


# ============================== 等待 ==============================

def test_wait_for_returns_matching_message():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"SOC","v":55}')

    msg = probe.wait_for(lambda m: b"SOC" in m.payload, timeout=1)
    assert msg.topic == "edge/n/NDATA"


def test_wait_for_times_out_with_diagnostics():
    """超时消息必须带"实际收到了什么"，否则现场只能猜是设备没发还是条件写错。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')

    with pytest.raises(ProbeTimeout) as exc:
        probe.wait_for(lambda m: b"NOPE" in m.payload, timeout=0.05, interval=0.005)
    assert "edge/n/NDATA" in str(exc.value)


def test_wait_for_reports_empty_case():
    probe = NorthboundProbe(FakeTransport(), "edge/#").start()
    with pytest.raises(ProbeTimeout, match="一条都没收到"):
        probe.wait_for(lambda m: True, timeout=0.05, interval=0.005)


def test_wait_count_waits_for_enough_messages():
    """补发场景要断言"一条不少"，靠条数而不是某条内容。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')

    with pytest.raises(ProbeTimeout):
        probe.wait_count(2, timeout=0.05, interval=0.005)


def test_wait_quiet_returns_after_silence():
    """反证"不该再发"时用它：必须先安静下来才能说明确实没再发。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    probe.wait_quiet(quiet_for=0.05, timeout=2.0, interval=0.005)


def test_wait_quiet_keeps_waiting_while_messages_arrive():
    """有消息进来就不算安静——否则"还在发"会被误判成"已经停了"。"""
    import threading

    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()

    def _keep_sending():
        for _ in range(5):
            t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')
            threading.Event().wait(0.02)

    th = threading.Thread(target=_keep_sending)
    th.start()
    probe.wait_quiet(quiet_for=0.05, timeout=2.0, interval=0.005)
    th.join()

    # 安静判定必须发生在最后一条消息之后
    assert len(probe.messages) == 5


# ============================== 解码 ==============================

def test_points_decodes_data_frames():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"VOLTAGE_A","v":230.5}')
    t.deliver("edge/n/NDATA", b'{"p":"SOC","v":55}')

    assert probe.points() == {"VOLTAGE_A": 230.5, "SOC": 55.0}


def test_points_keeps_latest_value_per_name():
    """同一个点多次上报是正常的；断言"当前值"取最新，数次数请用 messages。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NDATA", b'{"p":"SOC","v":10}')
    t.deliver("edge/n/NDATA", b'{"p":"SOC","v":42}')

    assert probe.points() == {"SOC": 42.0}
    assert len(probe.messages) == 2


def test_points_skips_birth_and_garbage():
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NBIRTH", b'{"online":true,"ts":1}')
    t.deliver("edge/n/NDATA", b'not json')
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')

    assert probe.points() == {"A": 1.0}


def test_birth_count_counts_online_frames():
    """重连后必须重发一次上线帧，这个数是断网用例的断言点。"""
    t = FakeTransport()
    probe = NorthboundProbe(t, "edge/#").start()
    t.deliver("edge/n/NBIRTH", b'{"online":true}')
    t.deliver("edge/n/NDATA", b'{"p":"A","v":1}')
    t.deliver("edge/n/NBIRTH", b'{"online":true}')

    assert probe.birth_count() == 2
