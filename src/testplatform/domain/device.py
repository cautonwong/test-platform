"""
Device —— 所有具体设备类型(Inverter/Battery/未来的 PCS、充电桩……)的聚合根基类。

职责边界:
- 只保护"连接生命周期"这一条所有设备都共有的不变量
  (未连接不能读写、故障态必须显式清除才能恢复)。
- 具体设备的业务规则(功率范围、SOC 范围……)留给子类自己扩展,
  但子类必须先调用 ensure_connected(),这是不变量真正被强制执行的地方。

外部代码(包括驱动层)只能通过这里导出的方法改变状态,
绝不允许直接写 self._state —— 这是这个类作为"聚合根"的核心含义:
它是外部访问这团内部状态的唯一入口。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Deque, List, Optional, Tuple

from .events import DeviceFaulted, DomainEvent
from .exceptions import DeviceFaultError, DeviceNotConnectedError, InvariantViolation

# 历史读数只留最近这么多条:它是窗口,不是全量。一次真机巡检跑一整天、每 5 秒一条,
# 265 台设备全留着是几百 MB 且还在涨;最近 1000 条(约 232 B/条,每台 230 KB)够现场回溯。
# "这台设备一共记了多少条"看 readings_recorded —— 那个数字只增不减,裁剪不动它。
HISTORY_WINDOW = 1000


class DeviceState(Enum):
    DISCONNECTED = auto()
    CONNECTED = auto()
    FAULT = auto()


class Device:
    def __init__(self, device_id: str, name: str) -> None:
        self.id = device_id
        self.name = name
        self._state: DeviceState = DeviceState.DISCONNECTED
        self._last_error: Optional[str] = None
        self._connected_at: Optional[datetime] = None
        self._pending_events: List[DomainEvent] = []
        self._history: Deque[Any] = deque(maxlen=HISTORY_WINDOW)
        self._readings_recorded = 0

    # ---- 状态迁移:唯一允许修改 _state 的地方 ----

    def mark_connected(self) -> None:
        if self._state == DeviceState.FAULT:
            raise DeviceFaultError(f"{self.id} 处于故障态,须先 clear_fault 才能连接")
        self._state = DeviceState.CONNECTED
        self._connected_at = datetime.now(timezone.utc)

    def mark_disconnected(self) -> None:
        self._state = DeviceState.DISCONNECTED
        self._connected_at = None

    def mark_fault(self, reason: str) -> None:
        self._state = DeviceState.FAULT
        self._last_error = reason
        self._raise_event(DeviceFaulted(device_id=self.id, reason=reason))

    def clear_fault(self) -> None:
        if self._state != DeviceState.FAULT:
            raise InvariantViolation(f"{self.id} 当前不处于故障态,无需 clear_fault")
        self._state = DeviceState.DISCONNECTED
        self._last_error = None

    def ensure_connected(self) -> None:
        """子类的每一个"读/写设备"方法在真正动作前都必须调用这个检查——
        不变量在这里被真正强制执行,而不是停留在文档约定层面。"""
        if self._state != DeviceState.CONNECTED:
            raise DeviceNotConnectedError(
                f"{self.id} 当前状态为 {self._state.name},必须先连接才能操作"
            )

    # ---- 历史读数:固定容量的窗口 + 只增不减的计数 ----

    def _record(self, reading: Any) -> None:
        """子类每记一条读数都走这里。窗口满了 deque 自己丢最旧的一条,
        计数器照旧加一 —— 裁剪不会让"一共记了多少条"变小。"""
        self._history.append(reading)
        self._readings_recorded += 1

    @property
    def history(self) -> Tuple[Any, ...]:
        """最近 HISTORY_WINDOW 条读数 —— 窗口,不是全量。"""
        return tuple(self._history)

    @property
    def readings_recorded(self) -> int:
        """这台设备一共记过多少条读数,只增不减。
        取增量只能用这个差值,不能用 len(history):后者封顶在 HISTORY_WINDOW。"""
        return self._readings_recorded

    # ---- 事件向上:子类通过 _raise_event 产生事件,外部通过 pull_events 取走 ----

    def _raise_event(self, event: DomainEvent) -> None:
        """只有聚合根自己(包括子类)能往队列里放事件,
        外部代码没有任何方法可以伪造一个事件塞进来。"""
        self._pending_events.append(event)

    def pull_events(self) -> List[DomainEvent]:
        """取走当前所有待发事件,并清空队列——
        调用一次就清空,保证同一个事件不会被重复上报两次。
        什么时候调用、调用的频率,完全是外部(应用层/pytest 插件)的决定,
        聚合根自己从不主动"推"给任何人。"""
        events = self._pending_events
        self._pending_events = []
        return events

    # ---- 只读访问 ----

    @property
    def state(self) -> DeviceState:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def connected_at(self) -> Optional[datetime]:
        return self._connected_at
