"""
用例声明的收尾:被测设备的收尾只有条款知道,所以由用例显式注册,平台不替它猜。

这条用例改了一个可写点(有功计量模式),注册“写回出厂值 0”作为收尾;场景记录落盘时
收尾已经执行完,结果写进 scenario-*.json 的 cleanup 字段。

异常路径(用例抛异常时收尾照样跑)在 tests_scenario/bench_restore_cases.py 里 ——
那条用例必须真的失败,所以它由嵌套 pytests 跑,不进默认套件。
形状与“不支持什么”见 docs/adr/0005-scenario-layer-shape.md。
"""
import json

import pytest

from testplatform.cases import case, case_of
from testplatform.reporting.scenario import ScenarioStep, record_scenario

from tests_support.named_device import device_or_skip

METER = "meter_01"
PARAMETER = "有功计量模式"
FACTORY_VALUE, WRITTEN_VALUE = 0, 1601


@case(id="S_00_00_02_000001_01", clause="DLT614-设置类条款", tags=["场景", "设置"])
def test_the_case_declared_cleanup_puts_the_parameter_back(composition_root, scenario, request):
    """写 1601 → 声明“写回 0” → 场景记录落盘时收尾已执行,设备回到出厂值。

    收尾不是隐藏的魔法:用例把这条用例动过什么写下来,平台只负责“一定执行”与“写进证据”。
    """
    if composition_root.evidence is None:
        pytest.skip("--no-evidence 下没有证据目录,收尾结果无从落盘")

    meter = device_or_skip(composition_root, METER)
    meter.connect()
    label = f"把 {PARAMETER} 写回 {FACTORY_VALUE}"
    scenario.on_cleanup(lambda: meter.write(PARAMETER, FACTORY_VALUE), label=label)
    written = meter.write(PARAMETER, WRITTEN_VALUE)

    step = ScenarioStep(
        METER,
        f"写 {PARAMETER} = {WRITTEN_VALUE}",
        written == pytest.approx(WRITTEN_VALUE),
        {"read_back": written},
    )
    composition_root.record_evidence(identity=case_of(request.node))
    path = record_scenario(
        composition_root.evidence,
        name=request.node.name,
        identity=case_of(request.node),
        participants={METER: composition_root.role_of(METER)},
        reading_keys={METER: (PARAMETER,)},
        steps=[step],
        cleanups=scenario,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["cleanup"] == [{"action": label, "passed": True}]
    assert record["cleanup_failed"] is False
    # 收尾真的执行了,不是只记了一行:同一实例上的下一次读回到出厂值。
    assert meter.read(PARAMETER) == pytest.approx(float(FACTORY_VALUE))
