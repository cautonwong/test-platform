"""
系统级/集成测试:验证多个聚合根"放在一起跑"时的行为,
和 tests/ 下按类型展开的"单设备契约测试"是完全不同的测试目的。

这里刻意不会去"从 Inverter 内部拿到 Battery 的引用"或反过来——
两个聚合根各自独立维护自己的不变量(聚合边界不能被跨聚合打通),
协调/编排逻辑只存在于测试代码这一层,通过各自的公开方法分别调用,
再比较结果是否符合系统级预期。这正是"仓储/聚合粒度对齐"那条原则
在多聚合协作场景下的自然延伸:没有谁需要承担"同时操作两个聚合"的职责。

这个目录下的用例通常直接用 composition_root.get(id),
而不是 tests/conftest.py 里那种按类型展开的泛型 fixture——
因为这里关心的是"这几个具体设备放在一起会怎样",不是"这一类设备各自对不对"。
点名的设备在清单里不存在时用 device_or_skip:跳过并说明,不报 KeyError(那不是平台 bug)。
"""

from tests_support.named_device import device_or_skip


def test_system_bootstrap_brings_up_every_configured_device(composition_root):
    """最基础的系统级检查:devices.yaml 里不同协议、不同类型的设备
    能被同时连接、互不干扰。这类"桩"测试在真实产线里价值很高——
    经常出现"单独测没问题,几个设备一起跑其中一个就连不上"这种
    资源抢占/初始化顺序问题,而这类问题在单设备契约测试里发现不了。"""
    drivers = composition_root.all_drivers()
    assert len(drivers) >= 2, "这条用例的意义依赖于配置里至少有两种以上设备同时在跑"

    for driver in drivers:
        driver.connect()

    for driver in drivers:
        assert driver.device.state.name == "CONNECTED"


def test_inverter_and_battery_scenario_both_report_valid_readings(composition_root):
    """典型集成测试形态:不是测两个设备之间有没有写死的耦合关系,
    而是测"放在同一个测试流程里跑,各自依然对"——比如先读逆变器出力,
    紧接着读电池状态,确认两个独立聚合根各自的读数都合法,
    过程中彼此没有互相污染内部状态。"""
    inverter = device_or_skip(composition_root, "inv_01")
    battery = device_or_skip(composition_root, "bat_01")

    inverter.connect()
    battery.connect()

    power = inverter.read("有功功率")
    soc = battery.read("SOC")

    assert 0 <= power <= inverter.device.rated_power_w * 1.1
    assert 0 <= soc <= 100

    # 跨聚合的校验逻辑只应该出现在这里(测试/应用层),
    # 不应该为了"方便"而让 Inverter 或 Battery 互相认识对方。
