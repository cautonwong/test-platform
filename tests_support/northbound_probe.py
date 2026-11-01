"""
北向观察器：收集设备往云端发的消息，供 E2E 断言用。

## 为什么需要它

北向用例的断言点都是「**设备发了什么**」：发了没有、名字对不对、顺序对不对、
断网恢复后有没有补发。这些断言要的不是"一次请求一次应答"，而是**一段时间窗内收到的序列**。
`MqttTransport.subscribe(topic, handler)` 只给回调，把"收集 + 等待 + 解码"这三件事
留在每个用例里重复写，写五遍就有五个版本的超时与容错。

所以集中成一个观察器：**只收集，不判断**。判断留给用例（判据是设备手册的事）。

## 边界

- 不启动 broker：broker 是台架基础设施，由 conftest / 外部编排负责。
  这样观察器能对着真 broker 也能对着假 transport 单测。
- 不解业务语义：`points()` 只是把 `{"p":..,"v":..}` 解成 `名字→值`，
  "这个值对不对"是判据集的事。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from testplatform.protocol import mqtt_json

__all__ = [
    "NorthboundProbe",
    "Observed",
    "ProbeTimeout",
]


@dataclass(frozen=True)
class Observed:
    """一条收到的北向消息。"""

    topic: str
    payload: bytes
    at: float  # 单调时钟，只用于比较先后与算延迟，不是墙上时间


class ProbeTimeout(AssertionError):
    """等到超时也没等到满足条件的消息。

    消息里必须带上"实际收到了什么"——否则现场只能猜是设备没发还是条件写错了。
    """

    def __init__(self, expectation: str, seen: Iterable[Observed]) -> None:
        topics = [f"{o.topic} ({len(o.payload)}B)" for o in seen]
        detail = "、".join(topics[-5:]) if topics else "（一条都没收到）"
        super().__init__(f"等待 {expectation} 超时；期间收到：{detail}")
        self.seen = tuple(seen)


class _Subscribable(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[str, bytes], None]) -> None: ...


class NorthboundProbe:
    """订阅一个主题空间，把收到的消息按序攒起来。

    典型用法::

        probe = NorthboundProbe(transport, "edge/#")
        probe.start()
        ...
        msg = probe.wait_for(lambda m: b"VOLTAGE_A" in m.payload, timeout=3)
        assert probe.points()["VOLTAGE_A"] == pytest.approx(230.0)
        probe.stop()
    """

    def __init__(
        self,
        transport: _Subscribable,
        topic: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not topic:
            raise ValueError("订阅主题不能为空")
        self._transport = transport
        self._topic = topic
        # 只给 Observed.at 用（决定先后顺序，可注入以便确定性测试）。
        # 等待/超时**一律用真实单调时钟**：把假时钟用在 deadline 上会和真实
        # time.sleep 打架——假时钟不走，超时永远不到，测试直接挂死。
        self._clock = clock
        self._messages: list[Observed] = []
        self._started = False

    # ---- 生命周期 ----

    def start(self) -> "NorthboundProbe":
        if self._started:
            return self
        self._transport.connect()
        self._transport.subscribe(self._topic, self._on_message)
        self._started = True
        return self

    def stop(self) -> None:
        if not self._started:
            return
        self._transport.disconnect()
        self._started = False

    def __enter__(self) -> "NorthboundProbe":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ---- 收集 ----

    def _on_message(self, topic: str, payload: bytes) -> None:
        self._messages.append(Observed(topic=topic, payload=payload, at=self._clock()))

    @property
    def messages(self) -> tuple[Observed, ...]:
        """已收到的全部消息（快照，外部改动不影响内部）。"""
        return tuple(self._messages)

    def clear(self) -> None:
        """清空已收集的消息。

        **测试分段时用**：比如先验证上线帧，再清空，然后只断言数据帧。
        不要靠"索引 0 是 birth、索引 1 是 data"这种位置假设——补发会打乱位置。
        """
        self._messages.clear()

    # ---- 等待 ----

    def wait_for(
        self,
        predicate: Callable[[Observed], bool],
        timeout: float = 5.0,
        interval: float = 0.02,
    ) -> Observed:
        """等到第一条满足 predicate 的消息；超时抛 ProbeTimeout。

        只看**调用之后**收到的消息还是全部？看全部——补发场景下消息可能在
        调用前就到了，只扫增量会漏。代价是同一个条件可能立刻返回旧消息，
        需要"下一条"时用 `wait_count`。
        """
        deadline = time.monotonic() + timeout
        while True:
            for msg in self._messages:
                if predicate(msg):
                    return msg
            if time.monotonic() >= deadline:
                raise ProbeTimeout("满足条件的消息", self._messages)
            time.sleep(interval)

    def wait_count(self, count: int, timeout: float = 5.0, interval: float = 0.02) -> None:
        """等到至少收到 count 条消息。用于"补发 N 条，一条不少"这类断言。"""
        deadline = time.monotonic() + timeout
        while len(self._messages) < count:
            if time.monotonic() >= deadline:
                raise ProbeTimeout(f"至少 {count} 条消息", self._messages)
            time.sleep(interval)

    def wait_quiet(self, quiet_for: float, timeout: float = 5.0, interval: float = 0.02) -> None:
        """等到"连续 quiet_for 秒没有新消息"。

        用于反证：**不该**再发的时候（比如重连后重复的 BIRTH、已确认的补发）用它。
        只断言"没收到"是不够的——必须等到安静下来才说明"确实没再发"。
        """
        deadline = time.monotonic() + timeout
        last_len = len(self._messages)
        quiet_since = time.monotonic()
        while True:
            now = time.monotonic()
            if len(self._messages) != last_len:
                last_len = len(self._messages)
                quiet_since = now
            if now - quiet_since >= quiet_for:
                return
            if now >= deadline:
                raise ProbeTimeout(f"安静 {quiet_for}s", self._messages)
            time.sleep(interval)

    # ---- 解码 ----

    def points(self) -> Mapping[str, float]:
        """把数据帧解成 `metric 名 → 值`。

        同一个点被多次上报时**后到的覆盖先到的**（最新值语义）——断言"当前值"
        用它；断言"发了几次"用 `messages` 自己数，不要用这个。
        上线帧与解析失败的帧跳过（它们不是点位读数）。
        """
        latest: dict[str, float] = {}
        for msg in self._messages:
            try:
                cmd = mqtt_json.parse_downlink(msg.payload)
            except mqtt_json.MqttJsonFormatError:
                continue
            latest[cmd.point] = cmd.value
        return latest

    def birth_count(self) -> int:
        """上线帧条数。重连后必须重发一次，所以这个数在断网用例里是断言点。"""
        return sum(1 for m in self._messages if mqtt_json.is_birth(m.payload))
