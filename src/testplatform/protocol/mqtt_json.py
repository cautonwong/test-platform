"""
北向 MQTT 的裸 JSON 载荷。

这一层只回答「字节里说了什么」，不回答「这条命令该不该执行」——
后者是判据集与聚合根的事（见 docs/adr/0004-protocol-knowledge-is-its-own-layer.md）。

## 为什么北向载荷也要有编解码层

北向的字节和南向一样是**外部契约**：设备发的 JSON 由设备实现决定。
平台若在用例里 `json.loads` 一把梭，格式一改就要改所有用例，而且各家容错程度不同。
集中在这里，改一处；而且它是纯函数，能脱离 broker 单测。

## 与设备侧的一致性

格式与 EdgeC 的 `driver_mqtt_json.c` 一一对应（那边是 C 实现）。两边必须同意同一套规则，
否则「平台觉得合法、设备不认」这种错会在联调时表现成「命令没反应」，极难定位。
规则清单见 `DOWNLINK_RULES`——改一边就要改另一边。

## 上行

- 数据帧 `{"p": "<metric 名>", "v": <数值>}`
- 上线帧 `{"online": true, "ts": <毫秒>}`

## 下行

同一套字段：`{"p": "<metric 名>", "v": <数值>}`。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "DOWNLINK_RULES",
    "BirthFrame",
    "DownlinkCommand",
    "MqttJsonFormatError",
    "is_birth",
    "parse_birth",
    "parse_downlink",
]

# 与 EdgeC driver_mqtt_json.c 的 _name_is_safe 保持一致：
# 只允许标识符字符；带引号/空格/反斜杠的一律拒绝，而不是转义。
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")

DOWNLINK_RULES = (
    "载荷必须是 JSON 对象",
    "必须同时有 p 与 v，缺一即判错（缺 v 不能当 0 用：'关到 0' 与 '没给值' 是两件事）",
    "p 必须匹配 ^[A-Za-z0-9_.-]+$",
    "v 必须是数字（int / float，含负数与科学计数）",
    "其它键忽略；嵌套对象/数组一律判错（没有语义，出现即发错）",
)


class MqttJsonFormatError(ValueError):
    """载荷不符合约定。消息里必须写清是哪一条规则，否则现场只能靠猜。"""


@dataclass(frozen=True)
class DownlinkCommand:
    point: str
    value: float


@dataclass(frozen=True)
class BirthFrame:
    online: bool
    ts_ms: int | None


def _load_object(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, (bytes, bytearray)):
        raise MqttJsonFormatError("载荷必须是 bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MqttJsonFormatError(f"载荷不是合法 UTF-8：{exc}") from exc

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MqttJsonFormatError(f"载荷不是合法 JSON：{exc}") from exc

    if not isinstance(obj, dict):
        raise MqttJsonFormatError("载荷必须是 JSON 对象，不能是数组或标量")
    return obj


def parse_downlink(payload: bytes) -> DownlinkCommand:
    """解析一条下行命令。失败一律抛 MqttJsonFormatError，不返回半成品。"""
    obj = _load_object(payload)

    if "p" not in obj:
        raise MqttJsonFormatError("缺 p：不知道该控制哪个点")
    if "v" not in obj:
        raise MqttJsonFormatError("缺 v：没有值就不能当 0 处理")

    point = obj["p"]
    if not isinstance(point, str) or not _IDENTIFIER.match(point):
        raise MqttJsonFormatError(f"p 不是标识符：{point!r}")

    value = obj["v"]
    # bool 是 int 的子类，必须单独排除：true 会被当成 1，静默把开关打开。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MqttJsonFormatError(f"v 不是数字：{value!r}")

    for key, item in obj.items():
        if key in ("p", "v"):
            continue
        if isinstance(item, (dict, list)):
            raise MqttJsonFormatError(f"键 {key!r} 是嵌套结构，北向载荷只允许标量")

    return DownlinkCommand(point=point, value=float(value))


def is_birth(payload: bytes) -> bool:
    """上线帧判定：只看有没有 online 键，不做完整校验（给等待用）。"""
    try:
        return "online" in _load_object(payload)
    except MqttJsonFormatError:
        return False


def parse_birth(payload: bytes) -> BirthFrame:
    obj = _load_object(payload)
    if "online" not in obj:
        raise MqttJsonFormatError("不是上线帧：缺 online")
    online = obj["online"]
    if not isinstance(online, bool):
        raise MqttJsonFormatError(f"online 必须是布尔：{online!r}")

    ts = obj.get("ts")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, int)):
        raise MqttJsonFormatError(f"ts 必须是整数毫秒：{ts!r}")

    return BirthFrame(online=online, ts_ms=ts)
