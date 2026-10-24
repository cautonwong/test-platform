"""
协议工厂注册表。

组合根(composition/root.py)读取 devices.yaml 后,就是靠这个注册表
把一条配置里的 "protocol: tcp, mode: real" 翻译成一个真正可用的 Transport 实例。

新增一种协议、或者给已有协议新增一种实现方式(比如以后要接 Modbus-TCP,
或者给 CAN 换一款板卡驱动),只需要在对应模块里写一个
用 @register_transport(...) 装饰的工厂函数——composition/root.py
不需要改一行代码。这是"对扩展开放、对修改关闭"在这里的直接体现。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Tuple

_FACTORIES: Dict[Tuple[str, str], Callable[[dict], Any]] = {}

# 模拟器能造出来的故障种类。清单里写别的名字 = 装配期报错,不是我在这里悄悄忽略。
# 这四类与 tools/mock_modbus_meter.py 的 --fail 选项同名同义:进程内模拟器与独立进程模拟器
# 用同一套词汇,换实现不换说法。
SIMULATOR_FAULT_KINDS = ("timeout", "bad-crc", "slow", "link-lost")


def register_transport(protocol: str, mode: str):
    """mode 固定取值 "real" 或 "simulator"。"""

    def decorator(factory: Callable[[dict], Any]):
        key = (protocol, mode)
        if key in _FACTORIES:
            raise ValueError(f"协议工厂重复注册: protocol={protocol!r}, mode={mode!r}")
        _FACTORIES[key] = factory
        return factory

    return decorator


def build_transport(protocol: str, mode: str, connection_cfg: dict) -> Any:
    key = (protocol, mode)
    if key not in _FACTORIES:
        raise KeyError(
            f"未找到协议工厂: protocol={protocol!r}, mode={mode!r}。"
            f"已注册的组合: {sorted(_FACTORIES.keys())}"
        )
    return _FACTORIES[key](connection_cfg)


def registered_keys() -> list[Tuple[str, str]]:
    """主要用于测试/调试,确认某个协议+模式是否已经注册。"""
    return sorted(_FACTORIES.keys())


def simulator_capabilities(transport: Any) -> dict:
    """这个模拟器能干什么:可注入的故障种类、能不能按点注入数据。

    报告与资产目录(08)只读这一个入口 —— 能力就长在实现它的那个对象上,不另立一份声明表,
    所以"删掉声明"等于"能力真的没有",不会出现声明与实现各说一套。

    返回里的 `faults_are_static: True` 是刻意的诚实:**故障在装配时定死**(清单里
    connection.simulated_faults),不支持运行中切换。它做不了"先正常三帧、第四帧超时";
    时序类用例要这种能力,得先给模拟器加运行中开关,而不是在用例里假装有。
    """
    return {
        "faults": list(getattr(transport, "supported_faults", ())),
        "data_injection": hasattr(transport, "inject_point"),
        "faults_are_static": True,
    }


def simulator_profile(access: str, connection: Mapping[str, Any] | None = None) -> dict:
    """型号画像:有没有模拟器(CI 能不能跑)、支持哪些故障注入、能不能按点注入。

    键是**接入方式**(serial / tcp / gateway_ssh / can …),不是规约:模拟器工厂按
    (接入方式, simulator) 注册(规约决定的是驱动,见 drivers/registry.py)。
    资产目录(08)对型号的每种允许接入方式各问一次,就知道这个型号在 CI 上可不可跑;
    没有注册的模拟器不算错,如实报 runnable=False 与原因 —— 排任务时踩空比多一条记录难查得多。
    """
    try:
        transport = build_transport(access, "simulator", dict(connection or {}))
    except KeyError as exc:
        return {
            "runnable": False,
            "reason": str(exc),
            "faults": [],
            "data_injection": False,
            "faults_are_static": True,
        }
    return {"runnable": True, **simulator_capabilities(transport)}
