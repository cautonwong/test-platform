"""场景结论由步骤结果算出来:任一步不成立场景就不通过,失败落在 role 上决定是"不合格"还是"无效"。

这条纯规则不经过任何设备,所以放在单测层:真场景跑一遍要装配清单与传输,判不了三种结论的分支。
"""
from testplatform.reporting.scenario import ScenarioStep, verdict_of

PARTICIPANTS = {"grid_sim_01": "support", "inv_01": "dut"}

GRID_STEP = ScenarioStep("grid_sim_01", "设定电网", True, {})
DUT_STEP = ScenarioStep("inv_01", "读读数", True, {})


def test_verdict_passes_only_when_every_step_holds():
    assert verdict_of([GRID_STEP, DUT_STEP], PARTICIPANTS) == "passed"
    assert verdict_of([], PARTICIPANTS) == "passed"


def test_failed_dut_step_fails_the_scenario():
    drifted = ScenarioStep("inv_01", "读读数", False, {"breaches": ["grid_freq_hz=60.0 越过 55"]})

    assert verdict_of([GRID_STEP, drifted], PARTICIPANTS) == "failed"


def test_failed_support_step_makes_the_scenario_invalid_not_the_dut_failed():
    """周边设备没按设定输出时结论是"测试无效":被测设备这一轮没有有效结论,不能被判不合格。"""
    unapplied = ScenarioStep("grid_sim_01", "设定电网", False, {"freq_hz": 60.0})

    assert verdict_of([unapplied, DUT_STEP], PARTICIPANTS) == "invalid"
