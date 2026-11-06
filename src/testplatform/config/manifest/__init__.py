"""
清单各段的解析与校验:criteria_profiles(判据集)+ derived_criteria(跨设备派生判据)+
protocol_items(协议数据项目录)+ models(型号)+ devices(设备实例)。

型号把点表 / 能力 / 判据集声明一次,设备实例只写 id、型号名和接入位置。
整份清单在装配前校验完:型号引用、规约与接入方式、点表字段、判据键、设备角色、设备实例的键。
每条错误都要报出"是哪一条、错在哪"——静默忽略未知字段会变成假通过,
那是这个平台上最危险的 bug。

protocol_items 是**协议固有信息**的目录:一个数据标识(DI)的长度 / 编码 / 字节序 / 倍率
由规约决定,不由型号决定。一个协议对应几万个数据项,型号只声明差异:
点表项写 {item: <DI>, scale: <型号差异>},没写的字段从目录取。
"""

from __future__ import annotations

from . import constants, parsing, schema, validation

# 调用方一直从 testplatform.config.manifest 取名字;包内怎么分不改变这个入口。
for _module in (constants, schema, parsing, validation):
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)
del _module, _name, _value
