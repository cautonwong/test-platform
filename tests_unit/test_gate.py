"""
CI 门禁(票 05):四档结论 + 一堆报告 → 一个退出码 + 一行理由。

每条规则一条用例:把 `RULES` 里任何一行删掉/调换,对应用例就红 —— 这就是"门禁本身"的守卫。
另有三条边界:空输入不许绿、混档不许互相掩盖、smoke 任务不参与门禁但**必须被点名**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from testplatform.execution.gate import (
    GATE_FAILED,
    GATE_NO_ACCEPTANCE,
    GATE_INCOMPLETE,
    GATE_INVALID,
    GATE_NO_TASKS,
    GATE_OK,
    GATE_PLATFORM_ERROR,
    RULES,
    GateInputError,
    judge,
    load_summaries,
    main,
)

CLEAN = {"task": "t_clean", "kind": "acceptance", "verdict": "clean"}
FAILED = {"task": "t_failed", "kind": "acceptance", "verdict": "failed"}
INVALID = {"task": "t_invalid", "kind": "acceptance", "verdict": "invalid"}
INCOMPLETE = {"task": "t_incomplete", "kind": "acceptance", "verdict": "incomplete"}
PLATFORM = {
    "task": "t_platform",
    "kind": "acceptance",
    "verdict": "incomplete",
    "reason": "platform_error",
}


def _write_reports(root: Path, *summaries: dict) -> Path:
    for summary in summaries:
        directory = root / summary["task"]
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8"
        )
    return root


# ------------------------------------------------------------ 四条判决 ----

def test_every_acceptance_task_qualified_is_green():
    decision = judge([CLEAN, {**CLEAN, "task": "t_clean_2"}])

    assert decision.exit_code == GATE_OK
    assert "全部合格" in decision.reason
    assert "2 个验收任务" in decision.reason


def test_any_failed_task_is_red_and_named():
    decision = judge([CLEAN, FAILED])

    assert decision.exit_code == GATE_FAILED == 1
    assert "不合格 1 个(t_failed)" in decision.reason


def test_invalid_says_platform_problem_not_product():
    decision = judge([CLEAN, INVALID])

    assert decision.exit_code == GATE_INVALID == 2
    assert "测试无效 1 个(t_invalid)" in decision.reason
    assert "平台问题,不是产品" in decision.reason


def test_incomplete_is_its_own_red():
    decision = judge([CLEAN, INCOMPLETE])

    assert decision.exit_code == GATE_INCOMPLETE == 3
    assert "未完成 1 个(t_incomplete)" in decision.reason


def test_a_platform_error_is_four_while_the_report_stays_incomplete():
    """门禁把 reason=platform_error 单独映射成 4,但**不动报告的四档**(它仍是未完成)。"""
    decision = judge([PLATFORM])

    assert decision.exit_code == GATE_PLATFORM_ERROR == 4
    assert "平台错误 1 个(t_platform)" in decision.reason
    assert "不是产品不合格" in decision.reason
    assert PLATFORM["verdict"] == "incomplete", "门禁只读报告,不改报告的结论"


# -------------------------------------------------------- 混档与空输入 ----

def test_mixed_failed_and_invalid_lists_both_and_fails_on_the_worst():
    """混档:退出码取最严重的(不合格),理由里两类都列出来,谁也不掩盖谁。"""
    decision = judge([FAILED, INVALID])

    assert decision.exit_code == GATE_FAILED == 1
    assert "不合格 1 个(t_failed)" in decision.reason
    assert "测试无效 1 个(t_invalid)" in decision.reason


def test_no_reports_at_all_is_red_because_nothing_was_run():
    decision = judge([])

    assert decision.exit_code == GATE_NO_TASKS == 5
    assert "没有任何任务被跑过" in decision.reason
    assert "没跑不等于通过" in decision.reason


def test_only_smoke_tasks_is_red_with_its_own_code():
    """全标 smoke = 没有一条验收在跑。它必须红,而且要和"0 份报告(5)"分开报。"""
    decision = judge(
        [
            {"task": "t_smoke_ok", "kind": "smoke", "verdict": "clean"},
            {"task": "t_smoke_bad", "kind": "smoke", "verdict": "failed"},
        ]
    )

    assert decision.exit_code == GATE_NO_ACCEPTANCE == 6
    assert "没有一条验收任务在跑" in decision.reason
    assert "冒烟" in decision.reason
    # 冒烟任务仍然被点名(第 6 档不掩盖它们)
    assert decision.details[0].startswith("另有 2 个冒烟任务")
    assert "t_smoke_bad" in decision.details[1]


def test_no_reports_and_no_acceptance_are_different_codes():
    """5 = 一份报告都没有;6 = 有报告但没有验收。两者都不绿,但理由必须分得开。"""
    assert judge([]).exit_code == 5
    assert judge([{"task": "s", "kind": "smoke", "verdict": "clean"}]).exit_code == 6


def test_an_unrecognised_verdict_is_a_platform_error_not_a_pass():
    """报告里出现不认识的 verdict:报告层出了问题 —— 按平台错误红,不许当成合格。"""
    decision = judge([{"task": "t_weird", "kind": "acceptance", "verdict": "maybe"}])

    assert decision.exit_code == GATE_PLATFORM_ERROR
    assert "t_weird" in decision.reason


# --------------------------------------------------------------- smoke ----

def test_smoke_tasks_do_not_gate_but_are_listed():
    """冒烟任务不参与验收:它红了也不改退出码,但必须列出来(否则等于悄悄放行)。"""
    decision = judge([CLEAN, {"task": "t_smoke", "kind": "smoke", "verdict": "failed"}])

    assert decision.exit_code == GATE_OK == 0
    assert decision.details[0] == "另有 1 个冒烟任务(不参与验收): t_smoke"


def test_a_failed_smoke_task_is_named_as_a_failure():
    """"把红任务改成 smoke 就绿"是唯一能绕过验收的后门:失败必须在输出里以"失败"出现。"""
    decision = judge(
        [
            CLEAN,
            {"task": "t_smoke", "kind": "smoke", "verdict": "failed"},
            {"task": "t_smoke_invalid", "kind": "smoke", "verdict": "invalid"},
        ]
    )

    assert decision.exit_code == GATE_OK
    assert decision.details[0] == "另有 2 个冒烟任务(不参与验收): t_smoke、t_smoke_invalid"
    assert decision.details[1] == "其中 2 个冒烟任务是失败的: t_smoke(不合格)、t_smoke_invalid(测试无效)"


def test_clean_smoke_tasks_do_not_claim_a_failure():
    decision = judge([CLEAN, {"task": "t_smoke", "kind": "smoke", "verdict": "clean"}])

    assert decision.exit_code == GATE_OK
    assert len(decision.details) == 1, decision.details
    assert "失败" not in decision.details[0]


# ------------------------------------------------------- 读文件与入口 ----

def test_load_summaries_reads_a_directory_of_task_reports(tmp_path):
    reports = _write_reports(tmp_path / "reports", CLEAN, FAILED)

    loaded = load_summaries([reports])

    assert [summary["task"] for summary in loaded] == ["t_clean", "t_failed"]


def test_load_summaries_reads_individual_files(tmp_path):
    reports = _write_reports(tmp_path / "reports", CLEAN)

    loaded = load_summaries([reports / "t_clean" / "summary.json"])

    assert loaded == [CLEAN]


def test_a_broken_report_is_a_platform_error_not_a_missing_task(tmp_path):
    """坏掉的报告不能被当成"没这条":那会把跑失败的一轮悄悄变成合格。"""
    broken = tmp_path / "reports" / "t_broken"
    broken.mkdir(parents=True)
    (broken / "summary.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(GateInputError) as error:
        load_summaries([tmp_path / "reports"])
    assert "t_broken" in str(error.value)

    assert main([str(tmp_path / "reports")]) == GATE_PLATFORM_ERROR


def test_the_command_prints_the_reason_and_returns_the_code(tmp_path, capsys):
    reports = _write_reports(tmp_path / "reports", CLEAN, INVALID)

    code = main([str(reports)])

    assert code == GATE_INVALID == 2
    output = capsys.readouterr().out
    assert "测试无效 1 个(t_invalid)" in output
    assert "平台问题,不是产品" in output


def test_the_command_on_an_empty_directory_says_nothing_was_run(tmp_path, capsys):
    empty = tmp_path / "reports"
    empty.mkdir()

    assert main([str(empty)]) == GATE_NO_TASKS

    output = capsys.readouterr().out
    assert "没有任何任务被跑过" in output


def test_the_help_lists_every_exit_code_one_per_line(capsys):
    """半年后没人记得 5 是什么 —— 所以退出码表必须在 --help 里,一行一个。"""
    with pytest.raises(SystemExit):
        main(["--help"])

    help_text = capsys.readouterr().out
    expected = [
        "0  合格",
        "1  不合格",
        "2  测试无效",
        "3  未完成",
        "4  平台错误",
        "5  没跑过任务",
        "6  没有验收任务",
    ]
    for line in expected:
        assert f"  {line}" in help_text, f"--help 里缺退出码 {line!r}"


def test_the_rule_table_covers_exactly_the_four_verdict_classes():
    """`RULES` 就是门禁本身:顺序 = 优先级,code = 退出码。少了/多了一档都必须被看见。"""
    assert [name for name, _code, _title in RULES] == [
        "failed",
        "invalid",
        "incomplete",
        "platform_error",
    ]
    assert [code for _name, code, _title in RULES] == [1, 2, 3, 4]
