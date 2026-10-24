"""
用例声明的收尾:执行顺序、执行一次、失败不上抛,以及它怎么落到场景结论上。

三条口径:
    步骤都成立、收尾失败   → 这条用例仍然失败(cleanup_failed),但失败不在判据上
    用例崩在写出步骤之前   → 结论 invalid(测试无效);空步骤不许算出 passed
    收尾自己抛异常         → 记下来,不顶掉用例手上的原异常

真设备、真 fixture 的部分在 tests_scenario/(正常路径)与 tests_scenario/bench_restore_cases.py
(异常路径)里;这一层只钉规则。
"""
import json
import os
import pathlib
import subprocess

import pytest

from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.scenario import (
    ScenarioCleanup,
    ScenarioStep,
    record_interrupted_scenario,
    record_scenario,
)

PARTICIPANTS = {"inv_01": "dut"}
PASSING = [ScenarioStep("inv_01", "读读数", True, {})]


def _explode() -> None:
    raise RuntimeError("收尾失败")


def _read(evidence, name) -> dict:
    return json.loads((evidence.directory / f"scenario-{name}.json").read_text(encoding="utf-8"))


@pytest.fixture()
def evidence(tmp_path):
    return EvidenceSession(tmp_path / "evidence")


def test_cleanups_run_in_order_and_only_once():
    calls = []
    cleanups = ScenarioCleanup()
    cleanups.on_cleanup(lambda: calls.append("第一条"), label="第一条")
    cleanups.on_cleanup(lambda: calls.append("第二条"), label="第二条")

    assert cleanups.ran is False
    assert cleanups.run() == [{"action": "第一条", "passed": True}, {"action": "第二条", "passed": True}]
    assert cleanups.ran is True
    assert cleanups.run() == cleanups.run(), "重复调用不重复执行收尾"
    assert calls == ["第一条", "第二条"]


def test_a_failing_cleanup_does_not_stop_the_next_one_and_does_not_raise():
    """收尾之间互不牵连,而且这里不许上抛:它是在用例已经出错时跑的,抛出去就顶掉了原异常。"""
    calls = []
    cleanups = ScenarioCleanup()
    cleanups.on_cleanup(_explode, label="抛异常的那条")
    cleanups.on_cleanup(lambda: calls.append("还是跑了"), label="后面那条")

    results = cleanups.run()

    assert results == [
        {"action": "抛异常的那条", "passed": False, "error": "RuntimeError", "message": "收尾失败"},
        {"action": "后面那条", "passed": True},
    ]
    assert calls == ["还是跑了"]


def test_a_failed_cleanup_fails_the_scenario_without_blaming_a_criterion(evidence):
    """步骤都成立、收尾没成立:这条用例不过,但证据里看得出失败在流程上,不在判据上。"""
    cleanups = ScenarioCleanup()
    cleanups.on_cleanup(_explode, label="退出工厂模式")

    path = record_scenario(
        evidence,
        name="case_with_failed_cleanup",
        participants=PARTICIPANTS,
        reading_keys={"inv_01": ("有功功率",)},
        steps=PASSING,
        cleanups=cleanups,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "failed"
    assert record["cleanup_failed"] is True
    assert "流程问题" in record["note"] and "不是判据不合格" in record["note"]
    assert [step["passed"] for step in record["steps"]] == [True], "步骤本身仍然记成立"


def test_no_cleanup_leaves_the_fields_empty_and_the_verdict_untouched(evidence):
    """没注册收尾的场景记录不变:多出来的只有两个空字段,不会被读成"收尾失败"。"""
    path = record_scenario(
        evidence,
        name="case_without_cleanup",
        participants=PARTICIPANTS,
        reading_keys={"inv_01": ("有功功率",)},
        steps=PASSING,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["cleanup"] == [] and record["cleanup_failed"] is False


def test_an_interrupted_case_has_no_verdict_but_its_cleanups_still_land(evidence):
    """崩在写出步骤之前:verdict 不许由空步骤算出 passed —— 那是假通过。

    收尾结果照旧落盘:设备有没有停在工厂模式,靠的就是这一条。
    """
    cleanups = ScenarioCleanup()
    cleanups.on_cleanup(lambda: None, label="退出工厂模式")
    cleanups.on_cleanup(_explode, label="抛异常的那条")

    path = record_interrupted_scenario(evidence, name="case_that_died", cleanups=cleanups)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "invalid"
    assert "中断" in record["note"]
    assert record["steps"] == [] and record["participants"] == []
    assert [item["action"] for item in record["cleanup"]] == ["退出工厂模式", "抛异常的那条"]
    assert record["cleanup_failed"] is True


def test_nothing_is_written_without_an_evidence_session(tmp_path):
    """--no-evidence 下行为与没有这一层时一致:不建目录、不写文件。"""
    cleanups = ScenarioCleanup()
    cleanups.on_cleanup(lambda: None, label="退出工厂模式")

    assert record_scenario(
        None, name="x", participants=PARTICIPANTS, reading_keys={}, steps=[], cleanups=cleanups
    ) is None
    assert record_interrupted_scenario(None, name="x", cleanups=cleanups) is None
    assert cleanups.ran is True, "证据关掉也照样跑收尾:设备一样会停在工厂模式"


def test_a_skipped_scenario_leaves_no_record_and_a_crashed_one_does(tmp_path):
    """跳过 vs 崩了:只有后者该留 invalid 场景记录。

    跳过(点名参与方不在清单里 / `--select` 后的子集)没有有效结论,但留一条 invalid
    会把归因顶行从 clean 翻成 invalid —— 一轮全绿的跑动被报成"有测试无效"。
    客户清单只要漏一台设备就会踩。

    用真跑的子 pytest 验收:fixture 判定结局靠 makereport,而不是靠"异常抛进 yield"
    (pytest 不会那样抛,直接驱动生成器只会得到假结论)。
    """
    import os

    from tests_support.nested_pytest import pytest_command

    repo = pathlib.Path(__file__).resolve().parents[1]
    (tmp_path / "devices.yaml").write_text(
        """
criteria_profiles: {}
models:
  g:
    type: grid_simulator
    protocol: vendor_ascii
    capabilities: [Fre]
    point_table:
      Fre: {address: 'TODO', length: 7, encoding: ascii, writable: true}
devices: []
""",
        encoding="utf-8",
    )
    # 借用真实的场景层 fixture(不复制一份,否则验的是复制品)
    (tmp_path / "conftest.py").write_text(
        """
pytest_plugins = ['testplatform.plugin']
from tests_scenario.conftest import (  # noqa: F401 - 导入即注册
    _connect_support_before_tests,
    _restore_support_after_test,
    pytest_runtest_makereport,
    scenario,
)
""",
        encoding="utf-8",
    )
    (tmp_path / "test_scn.py").write_text(
        """
import pytest
from testplatform.cases import case


@case(id='S_T_01', clause='free: X 1.1', tags=['场景'])
def test_skips_before_recording(composition_root, scenario):
    pytest.skip('点名参与方不在这份清单里')


@case(id='S_T_02', clause='free: X 1.2', tags=['场景'])
def test_dies_before_recording(composition_root, scenario):
    raise RuntimeError('用例在写步骤前崩了')
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        pytest_command("-q", "--device-config", "devices.yaml", "test_scn.py"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(repo), str(repo / "src")]),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output  # 崩了那条让退出码非 0,这是对的

    (evidence_dir,) = list((tmp_path / "evidence").iterdir())
    names = sorted(path.name for path in evidence_dir.glob("scenario-*.json"))

    assert not any("test_skips_before_recording" in name for name in names), (
        f"跳过的场景不许留记录(它会污染整轮结论),实际:{names}"
    )
    assert any("test_dies_before_recording" in name for name in names), (
        f"崩了的场景仍要留 invalid 记录(这条不能被修 skip 带走),实际:{names}"
    )
