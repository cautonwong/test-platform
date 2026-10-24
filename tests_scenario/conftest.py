"""
场景测试层自己的 fixture:装配顺序、收尾登记口、周边设备的复位。

场景用例按 id 点名参与方、自己判每一步是否成立,所以它不通过泛型 fixture 拿设备 ——
这一层因此没有 tests/conftest.py 里那一整套按设备类型展开的钩子,只有下面三件事。
"""
from __future__ import annotations

import pytest

from testplatform.cases import case_of
from testplatform.reporting.injection import inject as inject_point
from testplatform.reporting.scenario import ScenarioCleanup, record_interrupted_scenario

from tests_support.support_faults import connect_support_or_skip, restore_support_or_invalid


@pytest.fixture(scope="session", autouse=True)
def _connect_support_before_tests(composition_root):
    """装配顺序:整轮场景开跑前周边设备已经连上。

    收尾要往台架上写(恢复到 initial_setpoint),所以它必须先连着 —— 否则每条场景的
    收尾都会失败成"测试无效",而真正的原因只是没人连它。
    """
    connect_support_or_skip(composition_root)


@pytest.fixture(autouse=True)
def _restore_support_after_test(composition_root):
    """每条场景跑完后把周边设备恢复到清单声明的 initial_setpoint。

    恢复失败 = 测试无效(不是设备不合格):台架停在别的设定上时,这一轮的读数没有有效结论。
    """
    yield
    restore_support_or_invalid(composition_root)


_OUTCOME_STASH = pytest.StashKey[str]()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """把这条用例的结局记在 item 上。

    为什么不用 try/except 包住 fixture 的 yield:pytest **不会**把测试体的异常抛进
    fixture 的生成器(它只在 teardown 阶段抛自己的错误),所以 `except Skipped` 永远
    不触发 —— 跳过的用例会被当成"跑完了但没写记录",也就被写成 invalid。
    """
    outcome = yield
    report = outcome.get_result()
    if report.when in ("setup", "call") and report.outcome != "passed":
        item.stash[_OUTCOME_STASH] = report.outcome


def _outcome_of(item) -> str | None:
    return item.stash.get(_OUTCOME_STASH, None)


@pytest.fixture()
def inject(composition_root, request):
    """这条用例的数据注入口:`inject("meter_01", "有功电量", 1234.56)`。

    证据里带这条用例的身份 —— 否则一轮跑完,没人分得清"读到 1234"是现场值还是用例摆的。
    """

    def _inject(device_id: str, key: str, value: float):
        return inject_point(
            composition_root, device_id, key, value, identity=case_of(request.node)
        )

    return _inject


@pytest.fixture()
def scenario(composition_root, request):
    """这条场景的收尾登记口:

        scenario.on_cleanup(lambda: meter.write("有功计量模式", 0))

    正常路径由 record_scenario(..., cleanups=scenario) 执行,结果进 scenario-*.json 的
    cleanup 字段;用例抛异常时走到这里 —— 收尾照旧执行,结果落进同一份文件,
    结论记 invalid("用例在写出步骤之前就中断了"),不是设备不合格。
    """
    cleanups = ScenarioCleanup()
    yield cleanups

    if _outcome_of(request.node) == "skipped":
        # 跳过不是"崩了":不留场景记录。留一条 invalid 会把归因顶行从 clean 翻成
        # invalid,于是一轮全绿的跑动被报成"有测试无效"(客户清单少一台设备就会踩)。
        return
    if cleanups.ran:
        return
    record_interrupted_scenario(
        composition_root.evidence,
        name=request.node.name,
        identity=case_of(request.node),
        cleanups=cleanups,
    )
