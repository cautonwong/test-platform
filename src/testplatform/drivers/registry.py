"""
设备工厂注册表。注册键是 (设备类型, 规约) 二元组,和传输层的 (协议, 模式) 键对称。

为什么要规约这一维:同一个设备类型在不同规约上的读法完全不同(电表走 Modbus RTU 还是
DL/T645),而同一规约下的不同型号只差一份点表。键里带规约,厂家和型号的差异就不会让
注册表线性膨胀 —— 新增同规约型号只改 yaml,新增规约才多一个工厂。

新增一种被测对象(逆变器、电池、电表、以后可能的充电桩):
新建一个 drivers/xxx_driver.py 文件,用 @register_device("xxx", "规约") 注册,
composition/root.py 不需要任何改动。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Tuple

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile

DeviceFactory = Callable[[str, dict, Model, Any, CriteriaProfile], Any]

_FACTORIES: Dict[Tuple[str, str], DeviceFactory] = {}
# 驱动自述的「读数键 → 判据量名」(vendor_ascii 这类把读数键记成域量的驱动)。登记的是驱动
# **已经在用的同一个 dict 对象**,不是第二份字面量:能力 = 实现,和传输层的能力声明同形。
# 消费者是资产目录(票 08):"判据量在不在该型号能判的量里"要算上这层映射,否则正常的域量
# 会被误报成缺口。装配期校验仍由组合根按驱动实例自述来判,两边读的是同一份内容。
_JUDGED: Dict[Tuple[str, str], Mapping[str, str]] = {}


def declare_judged_quantities(device_type: str, protocol: str, mapping: Mapping[str, str]) -> None:
    _JUDGED[(device_type, protocol)] = mapping


def judged_quantities(device_type: str, protocol: str) -> Mapping[str, str]:
    """这个 (设备类型, 规约) 把读数键映射成哪些判据量名。没有映射的驱动返回空。"""
    return _JUDGED.get((device_type, protocol), {})


def register_device(device_type: str, protocol: str):
    def decorator(factory: DeviceFactory):
        key = (device_type, protocol)
        if key in _FACTORIES:
            raise ValueError(f"设备类型重复注册: 设备类型={device_type!r}, 规约={protocol!r}")
        _FACTORIES[key] = factory
        return factory

    return decorator


def build_device(
    model: Model,
    device_id: str,
    spec: dict,
    transport: Any,
    criteria: CriteriaProfile,
) -> Any:
    key = (model.type, model.protocol)
    if key not in _FACTORIES:
        raise KeyError(
            f"未找到设备工厂: 设备类型={model.type!r}, 规约={model.protocol!r},"
            f"已注册: {sorted(_FACTORIES)}"
        )
    return _FACTORIES[key](device_id, spec, model, transport, criteria)


def registered_types() -> list[str]:
    """当前已注册的设备类型(去重)——测试层按类型参数化用它。"""
    return sorted({device_type for device_type, _ in _FACTORIES})


def registered_keys() -> list[Tuple[str, str]]:
    """已注册的 (设备类型, 规约) 组合,测试/调试用。"""
    return sorted(_FACTORIES.keys())
