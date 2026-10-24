"""
注册清单 —— **只做注册,不装配**。

`@register_transport` / `@register_device` 这些装饰器在"模块被 import 的那一刻"执行,所以
"平台认识哪些具体实现"这件事,实际上是由**谁被 import 过**决定的。以前这份清单只写在
`composition/root.py` 里,于是任何"只想知道注册表里有什么"的消费者(资产目录要问
"这个型号有没有模拟器""能注入哪些故障")都被迫 import 组合根 —— 而组合根为了一个常量
会连带 import 报告层:目录 → 组合根 → reporting 的连带依赖就是这么来的。

这个模块把"触发注册"从"装配"里拆出来:
    * **消费者**(目录、以后的工具)`import testplatform.composition.registrations`
    * **装配者**(组合根)照旧 import 它,再干自己的事

只搬 import,不改行为:注册出来的内容与以前完全一样(同一批模块、同一批装饰器)。
这里**不许**出现 `build_device` / `build_transport` / 任何 `CompositionRoot` 逻辑 ——
一旦有,它就变成了第二个装配点。
"""
from __future__ import annotations

# --- 触发注册:这是平台"认识所有具体类型"的唯一位置 ---
# 传输层:真实链路 + 进程内模拟器 + 经网关转发。import 它们 = 让 @register_transport 跑一遍。
from testplatform.transport import gateway_transport  # noqa: F401
from testplatform.transport import real_transports  # noqa: F401
from testplatform.transport import simulators  # noqa: F401

# 驱动层:每种 (设备类型, 规约) 一个工厂;顺带登记"读数键 → 判据量"的映射(见 drivers/registry.py)。
from testplatform.drivers import battery_driver  # noqa: F401
from testplatform.drivers import grid_simulator_driver  # noqa: F401
from testplatform.drivers import inverter_driver  # noqa: F401
from testplatform.drivers import meter_driver  # noqa: F401
from testplatform.drivers import terminal_driver  # noqa: F401
