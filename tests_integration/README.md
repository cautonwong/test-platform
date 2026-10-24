# `tests_integration/` —— 平台自身在多设备台架上的行为

这一层测的**不是**某台设备合格不合格(那是 `tests/` 的一致性契约层),也不是跨设备的联动结论
(那是 `tests_scenario/` 的场景层)。它测的是:**几台设备同时摆在台架上时,平台自己会不会出问题**。

真机联调时最先撞的都是这一类:

| 关注点 | 长什么样 | 现成用例 |
|---|---|---|
| 装配顺序 | 周边设备必须先于被测设备连上,清单里的书写顺序不能成为隐式依赖 | `test_support_equipment.py::test_assembly_connects_support_equipment_and_leaves_the_dut_to_its_tests`(`tests/`) |
| 资源独占 | 多台被测设备共用一台周边设备时,清单里只声明一个实例、取到的是同一个对象 | `test_bench_resources.py::test_multiple_duts_share_one_support_instance` |
| 初始化互相干扰 | 设备一起装配、一起连接,互不抢占 | `test_energy_system.py::test_system_bootstrap_brings_up_every_configured_device` |
| 收尾 | 一轮结束后台架复位、句柄关闭 | `tests_scenario/conftest.py` 的 `_restore_support_after_test`、`tests/test_bench_restore.py` |

写法上两条:

1. **直接用 `composition_root.get(id)` 点名设备**,不用 `tests/conftest.py` 里那种按类型展开的泛型
   fixture —— 这里关心的是"这几个具体设备放在一起会怎样",不是"这一类设备各自对不对"。
   点名的设备不在清单里时用 `tests_support/named_device.py` 的 `device_or_skip`:跳过并说明,
   不报 `KeyError`(那不是平台 bug)。
2. **不要让一个聚合根拿到另一个聚合根的内部引用**。协调逻辑留在测试/应用层,
   两个聚合根互相之间永远不知道对方的存在(见 `test_energy_system.py` 的模块说明)。

单台设备读不出这个层次的毛病:一台设备单独测全绿,几台一起跑时其中一台连不上、抢总线、
收尾没做干净 —— 都只会在这里露出来。
