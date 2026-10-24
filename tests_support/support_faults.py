"""
周边设备故障 = 测试无效:连不上、恢复不了,两种都在这里转成 skip(不是设备不合格)。

被测设备读到的东西可能已经被污染的台架决定,所以台架出问题时这一轮没有有效结论 ——
归因四类里要能读出"测试无效",而不是被读成"这台设备不合格"。

和 device_or_skip 同一个理由只有一处实现:tests/ 与 tests_scenario/ 各自的 autouse fixture
都调这里,两份实现必然漂移,"周边设备故障"的措辞与结论会一处一套。
收帧阶段的断链不在这个文件里:它在命令运行中被撞到,转换点在插件
(testplatform/plugin.pytest_runtest_call),前缀与结论相同。
"""
from __future__ import annotations

import pytest

from testplatform.composition.root import TEST_INVALID_PREFIX, SupportEquipmentFault


def connect_support_or_skip(composition_root) -> None:
    """周边设备(电网模拟器 / 可调负载 / 标准源)连不上时,被测设备的读数没有意义,
    所以结论是 skip 而不是 fail——被测设备不被判不合格。
    用例自己不写 try/except,证据层靠 reason 里的固定前缀区分"无效"与"不合格"。
    """
    try:
        composition_root.connect_support()
    except SupportEquipmentFault as exc:
        pytest.skip(f"{TEST_INVALID_PREFIX}{exc}")


def restore_support_or_invalid(composition_root) -> None:
    """把周边设备恢复到清单里的 initial_setpoint;恢复不了就转成 skip(测试无效)。

    调用点是用例的收尾阶段:步骤已经跑完,能被改写的只有"这一轮有没有结论"。
    skip 不改退出码,和连不上 / 收帧断链同一个口径、同一个前缀。
    """
    try:
        composition_root.restore_support()
    except SupportEquipmentFault as exc:
        pytest.skip(f"{TEST_INVALID_PREFIX}{exc}")
