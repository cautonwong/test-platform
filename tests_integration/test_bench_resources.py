"""台架资源:几台被测设备共用一台周边设备时,清单里只声明一个实例。

这一层只放"平台自身在多设备台架上的行为"(装配顺序、资源独占、初始化互相干扰、收尾),
不测某一类设备对不对 —— 那是 `tests/` 的契约层。定位与取舍见同目录 README.md。
"""
from tests_support.named_device import device_or_skip
from testplatform.config.manifest import DEFAULT_ROLE, SUPPORT_ROLE


def test_multiple_duts_share_one_support_instance(composition_root):
    """configs/devices.yaml:多台被测设备共用同一台周边设备,清单里只有一个 support
    实例,用例按 id 取到的是同一个对象——不需要"设备依赖关系"字段。"""
    grid_sim_01 = device_or_skip(composition_root, "grid_sim_01")

    assert composition_root.ids_of_role(SUPPORT_ROLE) == ["grid_sim_01"]
    assert {"inv_01", "inv_03"} <= set(composition_root.ids_of_role(DEFAULT_ROLE))
    assert composition_root.get("grid_sim_01") is grid_sim_01
