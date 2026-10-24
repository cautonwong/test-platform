"""tests_scenario 的接线守卫:该目录有无用例,必须和 testpaths 一致。

空目录留在 testpaths 里会收 0 条、静默报绿;有场景用例却没加回 testpaths 会永远跑不到。
两个方向都是静默的,所以这里各断言一次。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scenario_layer_wiring_matches_its_cases():
    in_testpaths = "tests_scenario" in next(
        line for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith("testpaths")
    )
    has_cases = any((ROOT / "tests_scenario").glob("test_*.py"))

    if has_cases:
        assert in_testpaths, "tests_scenario/ 已有场景用例,把它加回 pyproject 的 testpaths,否则默认套件跑不到"
    else:
        assert not in_testpaths, "tests_scenario/ 没有用例,别留在 testpaths(收 0 条是静默假绿);有真实场景需求时删掉这条断言"
