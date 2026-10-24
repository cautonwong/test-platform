"""
数据注入 —— 场景中途把模拟设备的读数设成一个指定值,并留一条证据。

它回答的问题是"设备报 X,产品做什么":把电量摆成 1234,看产品下一次读到之后怎么办。

三条边界,写在这里因为它们每一条都能变成假绿:

1. **只对 mode: simulator 生效**。真设备上的读数只能由设备自己改变;能"设"就说明
   设的不是设备,而是我们的假设。
2. **不是写路径**。注入直接改模拟器的状态,不发帧、不产生 write 记录 ——
   "产品能不能写设备"必须走协议写(0x06 / 0x10),"产品读到被设的值之后做什么"才是注入。
   这两件事混起来,一条绿用例就分不清产品是会写、还是测试替它写了。
3. **注入的值必须读得回来**。倍率与长度会把表示不了的值悄悄改成另一个数
   (scale 0.01 上注 1234.567 读回来是 1234.57)。所以注入后立刻用读路径解码一次,
   对不上就在注入时报错,而不是等用例断言"产品读到了 1234.567"时才发现设错了数。
"""
from __future__ import annotations

from typing import Any

from testplatform.protocol.values import decode_value, encode_value

# 注入值读回来的容差 = 一个 quantum 的百万分之一。它要严到能抓住"倍率取整吃掉小数位"
# (scale=0.01 时 1234.567 → 1234.57,差 0.003),又要松到不被浮点噪声误判
# (123456 * 0.01 这类乘法误差在 1e-13 量级)。
_TOLERANCE_IN_QUANTA = 1e-6


def inject(
    root: Any, device_id: str, key: str, value: float, *, identity: Any = None
) -> dict | None:
    """把模拟设备 `device_id` 的读数键 `key` 设成 `value`,返回证据记录(没开证据时为 None)。

    root 是组合根(duck 类型:只用到 model_of / mode_of / transports / evidence 与
    可选地 record_evidence)—— 这里不 import composition,免得证据层反向依赖装配层。
    identity 是"谁设的":用例身份,写进证据。
    """
    mode = root.mode_of(device_id)
    if mode != "simulator":
        raise ValueError(
            f"设备 {device_id!r} 是 mode: {mode},不能数据注入"
            f" —— 真设备上的读数只能由设备自己改变"
        )

    model = root.model_of(device_id)
    if key not in model.point_table:
        raise KeyError(
            f"型号 {model.name!r} 没有读数键 {key!r},可注入的读数键: "
            f"{sorted(model.point_table)}(注入点必须是点表里已声明的量)"
        )
    point = model.point_table[key]

    # 先看这个模拟器能不能注入,再谈怎么编码:反过来的话,文本模拟器会先报
    # "写路径不支持 ascii",把人引到编码上,而真正的原因是它根本接不了注入。
    #
    # 判据是 duck 类型(它有没有 inject_point),不是"它是不是某个具体类":
    # 认识具体模拟器是组合根的事,reporting 这一层只认能力。新增一个可注入的模拟器、
    # 只要它实现了 inject_point,这里自动就支持 —— 没有第二份名单要回来改。
    transport = root.transports[device_id]
    inject_point = getattr(transport, "inject_point", None)
    if inject_point is None:
        raise ValueError(
            f"设备 {device_id!r} 的模拟器 {type(transport).__name__} 不支持按点注入"
            f"(没有 inject_point);能注入的模拟器在能力声明里 data_injection 为 True"
            f"(见 transport.registry.simulator_capabilities)"
        )
    raw = encode_value(
        value, point.encoding, point.length, point.scale, point.byteorder, point.word_order
    )

    read_back_raw = inject_point(point, raw)
    read_back = decode_value(
        read_back_raw, point.encoding, point.scale, point.byteorder, point.word_order
    )
    if abs(read_back - value) > point.scale * _TOLERANCE_IN_QUANTA:
        raise ValueError(
            f"注入 {device_id}.{key} = {value} 读不回来:编码成 {raw.hex()},"
            f"按点表解出 {read_back}。该点 scale={point.scale} / length={point.length}"
            f" / encoding={point.encoding},表示不了这个值 —— 换一个它表示得了的值。"
        )

    evidence = getattr(root, "evidence", None)
    if evidence is None:
        return None
    return evidence.record_injection(
        device_id, key=key, value=value, raw=raw, identity=identity, mode=mode
    )
