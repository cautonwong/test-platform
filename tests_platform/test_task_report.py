"""
任务报告(票 04)的验收:四档结论各能构造、两栏通过率与分母写得出、未测两栏合并不了、
场景失败有归属、`clause.summary` 不进人读报告。

假输入直接写证据目录(与 execution/task.py 写出来的同名同形文件),跑报告不跑任务 ——
分类逻辑要在毫秒级用例里能红,不然"改错一处"只能靠真崩溃去撞。真任务只留一条:
接线(跑完真的落下 report.md / summary.json)。

条款一律 `free: 平台:测试报告`:报告是平台自己的用例,没有受控标准条款。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from testplatform.cases import case
from testplatform.clauses import load_clauses
from testplatform.reporting.report import (
    DECLARED_REASONS,
    REPORT_FILE,
    SUMMARY_FILE,
    build,
    render,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_TASK = REPO_ROOT / "configs" / "tasks" / "minimal.yaml"

# 条款清单里的标准条款:人读报告要写成 id(standard number),两个都是清单里的权威字段。
STANDARD_CLAUSE = "DLT645-4.5.2"
STANDARD_LABEL = "DLT645-4.5.2(DL/T 645 4.5.2)"

DEVICE_CASE = "M_00_00_01_000002_09"
OTHER_CASE = "M_00_00_01_000002_10"
DEVICE_NODEID = f"tests/test_meter.py::{DEVICE_CASE}"
OTHER_NODEID = f"tests/test_meter.py::{OTHER_CASE}"

BREACH = {
    "quantity": "有功功率",
    "value": 12.5,
    "limit": 10.0,
    "exceeded_by": 2.5,
    "severity": "reject",
    "text": "有功功率=12.5 越过 10.0,超出 2.5",
}


# ------------------------------------------------------------- 四档结论 ----

@case(id="T_00_00_04_000000_01", clause="free: 平台:测试报告", tags=["报告"])
def test_a_device_that_met_its_criteria_is_qualified(tmp_path):
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    report = build(directory)

    assert report.verdict == "clean", report.note
    assert (report.devices_passed, report.devices_total) == (1, 1)
    assert _text(directory).startswith("结论: 合格 —— ")


@case(id="T_00_00_04_000000_02", clause="free: 平台:测试报告", tags=["报告"])
def test_a_reject_breach_is_failed_and_names_the_numbers(tmp_path):
    """不合格项要能追到设备、条款、实测值、边界、超出多少 —— 缺一个,现场就得回去翻证据。"""
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE, breaches=[BREACH])]},
    )

    report = build(directory)
    text = _text(directory)

    assert report.verdict == "failed", report.note
    assert "meter_01" in text
    assert STANDARD_LABEL in text, text
    assert "实测 12.5" in text and "边界 10.0" in text and "超出 2.5" in text, text


@case(id="T_00_00_04_000000_03", clause="free: 平台:测试报告", tags=["报告"])
def test_a_link_fault_is_an_invalid_test_not_a_failed_device(tmp_path):
    """断链是台架的事:判测试无效,不许算成设备不合格。"""
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={
            "meter_01": [
                _reading(DEVICE_CASE),
                {
                    "kind": "link_lost",
                    "device": "grid_sim_01",
                    "operation": "receive",
                    "failure": "ConnectionError",
                    "message": "对端关闭",
                    "at": "2026-10-23T00:00:01.000+00:00",
                },
            ]
        },
        round_status={"verdict": "needs-review", "note": "本轮存在断链"},
    )

    report = build(directory)

    assert report.verdict == "invalid", report.note
    assert "测试无效" in _text(directory)


@case(id="T_00_00_04_000000_04", clause="free: 平台:测试报告", tags=["报告"])
def test_a_case_that_never_ran_is_incomplete(tmp_path):
    """守卫:申报外未测只要有一条,结论就不是合格(哪怕跑过的那条一切正常)。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[_device_case(DEVICE_CASE), _device_case(OTHER_CASE)],
            executed=[DEVICE_NODEID],
        ),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    report = build(directory)

    assert report.verdict == "incomplete", report.note
    assert [row["kind"] for row in report.outside_untested] == ["not_run"]
    assert report.verdict != "clean"


@case(id="T_00_00_04_000000_05", clause="free: 平台:测试报告", tags=["报告"])
def test_a_product_that_exits_by_itself_is_failed_not_invalid(tmp_path):
    """产品自己崩 = 产品不合格。它同时也会让剩余用例未执行,但优先级先命中不合格。"""
    directory = _round(
        tmp_path,
        task=_task(
            process_events=[
                {"kind": "product_exit", "how": "self_exit", "code": 3, "signal": None,
                 "at": "2026-10-23T00:00:02.000+00:00"},
                {"kind": "unexecuted", "count": 2, "reason": "product_crashed"},
            ]
        ),
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    report = build(directory)

    assert report.verdict == "failed", report.note
    assert "how=self_exit" in _text(directory)


@case(id="T_00_00_04_000000_06", clause="free: 平台:测试报告", tags=["报告"])
def test_a_product_that_never_started_is_incomplete_with_a_platform_reason(tmp_path):
    """起不来 = 平台错误:结论落未完成,而且首行与 summary.json 都要指向平台。"""
    directory = _round(
        tmp_path,
        task=_task(error="产品在 15s 内没有监听端口 15020(没起来或起得比超时更慢)"),
        scope=None,
    )

    write_report(directory)
    report = build(directory)
    summary = _summary(directory)

    assert report.verdict == "incomplete", report.note
    assert report.reason == "platform_error"
    assert summary["reason"] == "platform_error"
    assert _text(directory).splitlines()[0].startswith("结论: 未完成(reason: platform_error)")


@case(id="T_00_00_04_000000_07", clause="free: 平台:测试报告", tags=["报告"])
def test_a_failed_device_outranks_an_outside_untested_case(tmp_path):
    """优先级:同时有不合格与申报外未测时必须判不合格,不是"未完成"。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[_device_case(DEVICE_CASE), _device_case(OTHER_CASE)],
            executed=[DEVICE_NODEID],
        ),
        devices={"meter_01": [_reading(DEVICE_CASE, breaches=[BREACH])]},
    )

    report = build(directory)

    assert report.outside_untested, report.outside_untested
    assert report.verdict == "failed", report.note


# ------------------------------------------------- 两栏通过率与分母 ----

@case(id="T_00_00_04_000000_08", clause="free: 平台:测试报告", tags=["报告"])
def test_the_two_columns_and_the_denominator_are_written_down(tmp_path):
    """设备级按台、场景级按条(一个 S_ 节点一条),分母是 select ∩ 适用矩阵的命中数。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[
                _device_case(DEVICE_CASE),
                _device_case(OTHER_CASE, "meter_02"),
                _scenario_case("S_00_00_01_000001_01"),
                _scenario_case("S_00_00_01_000002_01"),
            ],
            executed=[DEVICE_NODEID, "tests_scenario/test_grid.py::S_00_00_01_000001_01"],
            deselected=[{"nodeid": "tests/test_meter.py::test_other", "case": None,
                         "reason": "不满足 --select case:T_00_00_01_000000"},
                        {"nodeid": "tests/test_meter.py::test_another", "case": None,
                         "reason": "不在该型号的适用矩阵里"}],
        ),
        devices={
            "meter_01": [_reading(DEVICE_CASE)],
            "meter_02": [_reading(OTHER_CASE, breaches=[BREACH])],
        },
        scenarios={
            "grid_ok": _scenario("grid_ok", "S_00_00_01_000001_01", "passed", []),
            "grid_bad": _scenario(
                "grid_bad",
                "S_00_00_01_000002_01",
                "failed",
                [{"device": "meter_01", "action": "读有功功率", "passed": False, "detail": {}}],
            ),
        },
    )

    report = build(directory)
    text = _text(directory)

    assert (report.devices_passed, report.devices_total) == (1, 2)
    assert (report.scenarios_passed, report.scenarios_total) == (1, 2)
    assert "设备级 1/2 台" in text, text
    assert "场景级 1/2 条" in text, text
    assert "命中 4 条" in text, text
    assert "筛掉的 2 条" in text, text


@case(id="T_00_00_04_000000_09", clause="free: 平台:测试报告", tags=["报告"])
def test_every_failed_scenario_says_who_is_to_blame(tmp_path):
    """场景失败三归属各一条:设备 / 台架 / 平台,不合并成一句"场景失败"。"""
    platform_error = (
        json.dumps(
            {
                "kind": "platform_error",
                "device": "meter_01",
                "case": {"id": "S_00_00_01_000003_01", "clause": STANDARD_CLAUSE, "tags": []},
                "nodeid": "tests_scenario/test_grid.py::S_00_00_01_000003_01",
                "error": "InvariantViolation",
                "message": "SOC 超出 0-100",
                "at": "2026-10-23T00:00:03.000+00:00",
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[
                _scenario_case("S_00_00_01_000001_01"),
                _scenario_case("S_00_00_01_000002_01"),
                _scenario_case("S_00_00_01_000003_01"),
            ]
        ),
        scenarios={
            "dut_step": _scenario(
                "dut_step",
                "S_00_00_01_000001_01",
                "failed",
                [{"device": "meter_01", "action": "读有功功率", "passed": False, "detail": {}}],
            ),
            "bench_step": _scenario(
                "bench_step",
                "S_00_00_01_000002_01",
                "invalid",
                [{"device": "grid_sim_01", "action": "下发三相", "passed": False, "detail": {}}],
            ),
            "platform_step": _scenario(
                "platform_step",
                "S_00_00_01_000003_01",
                "failed",
                [{"device": "meter_01", "action": "读电压", "passed": False, "detail": {}}],
            ),
        },
        platform_errors=platform_error,
    )

    lines = _text(directory).splitlines()

    assert any("[场景归属] 设备" in line and "dut_step" in line for line in lines), lines
    assert any("[场景归属] 台架" in line and "bench_step" in line for line in lines), lines
    assert any("[场景归属] 平台" in line and "platform_step" in line for line in lines), lines


# ------------------------------------------------------------- 设备级 0/0 ----

@pytest.mark.parametrize("kind", ["acceptance", None])
@case(id="T_00_00_04_000000_16", clause="free: 平台:测试报告", tags=["报告"])
def test_a_round_that_verified_no_device_is_not_qualified(tmp_path, kind):
    """设备级 0/0 = 这一轮一台设备都没验:验收任务不能算合格(场景栏满的也不行)。

    kind 缺省就是 acceptance —— 去掉字段的同一份任务必须照样红,否则"不写"等于给自己开眼。
    """
    scenario_case = "S_00_00_01_000001_01"
    directory = _round(
        tmp_path,
        task=_task(**({} if kind is None else {"kind": kind})),
        scope=_scope(
            collected=[_scenario_case(scenario_case)],
            executed=[f"tests_scenario/test_grid.py::{scenario_case}"],
        ),
        scenarios={"grid_ok": _scenario("grid_ok", scenario_case, "passed", [])},
    )

    write_report(directory)
    report = build(directory)
    first_line = _text(directory).splitlines()[0]

    assert (report.devices_passed, report.devices_total) == (0, 0)
    assert (report.scenarios_passed, report.scenarios_total) == (1, 1)
    assert report.verdict == "incomplete", report.note
    assert report.reason == "no_device_coverage"
    assert first_line.startswith("结论: 未完成(reason: no_device_coverage)"), first_line
    summary = _summary(directory)
    assert (summary["kind"], summary["reason"]) == ("acceptance", "no_device_coverage")


@case(id="T_00_00_04_000000_17", clause="free: 平台:测试报告", tags=["报告"])
def test_a_smoke_round_that_verified_no_device_is_qualified_but_says_which_kind(tmp_path):
    """冒烟任务验的是通路,不是设备:允许合格,但首行必须写明这一轮没验设备。

    只写“合格”等于说这台设备验过了 —— 首行是唯一保证被读到的一行。
    """
    scenario_case = "S_00_00_01_000001_01"
    directory = _round(
        tmp_path,
        task=_task(kind="smoke"),
        scope=_scope(
            collected=[_scenario_case(scenario_case)],
            executed=[f"tests_scenario/test_grid.py::{scenario_case}"],
        ),
        scenarios={"grid_ok": _scenario("grid_ok", scenario_case, "passed", [])},
    )

    write_report(directory)
    report = build(directory)
    first_line = _text(directory).splitlines()[0]

    assert (report.devices_passed, report.devices_total) == (0, 0)
    assert report.verdict == "clean", report.note
    assert report.reason is None
    assert "合格(冒烟:本轮未验设备)" in first_line, first_line
    summary = _summary(directory)
    assert (summary["kind"], summary["reason"]) == ("smoke", None)


# ------------------------------------------------------- 未测两栏 ----

@pytest.mark.parametrize(
    "reason, expected",
    [
        ("real_hardware 用例在 --force-simulator 下自动跳过", "平台不支持"),
        ("Skipped: inv_01(型号 x)未声明能力 ['无功电量'],跳过", "设备缺能力"),
        (
            "含 1 个待人工确认步骤(前面的 2 个自动步骤已执行且通过):"
            "不算通过也不算失败,由操作员确认后回填:合闸",
            "人工步骤",
        ),
        ("已知缺陷跳过:厂商固件 1.2.3 写后不生效", "已知缺陷跳过"),
    ],
)
@case(id="T_00_00_04_000000_10", clause="free: 平台:测试报告", tags=["报告"])
def test_a_declared_skip_lands_in_one_of_four_reasons(tmp_path, reason, expected):
    """申报内(灰)的理由是闭集:用例申报的跳过只认这四类,不自由发挥。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[_device_case(DEVICE_CASE)],
            executed=[DEVICE_NODEID],
            skipped=[{"nodeid": "tests/test_meter.py::test_skipped", "case": OTHER_CASE,
                      "reason": reason}],
        ),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    report = build(directory)

    assert [row["reason"] for row in report.declared_untested] == [expected]
    assert report.outside_untested == []
    assert report.verdict == "clean", report.note


@case(id="T_00_00_04_000000_11", clause="free: 平台:测试报告", tags=["报告"])
def test_a_skip_with_a_reason_outside_the_four_is_outside_untested(tmp_path):
    """认不出理由的跳过是申报外(红):一句"因为所以"不能把未测涂成灰的。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[_device_case(DEVICE_CASE)],
            executed=[DEVICE_NODEID],
            skipped=[{"nodeid": "tests/test_meter.py::test_skipped", "case": OTHER_CASE,
                      "reason": "懒得测"}],
        ),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    report = build(directory)

    assert report.declared_untested == []
    assert [row["kind"] for row in report.outside_untested] == ["skip"]
    assert report.verdict == "incomplete", report.note
    assert set(DECLARED_REASONS) == {"平台不支持", "设备缺能力", "人工步骤", "已知缺陷跳过"}


# ------------------------------------------- 设备没有读数:分得清的才不拉低 ----

@case(id="T_00_00_04_000000_18", clause="free: 平台:测试报告", tags=["报告"])
def test_a_device_whose_cases_never_read_is_not_an_evidence_gap(tmp_path):
    """选中的用例本来就不读数(跑到了、过了):不拿它把结论拉成未完成。

    设备仍在 devices_without_readings 里露名 —— 不拉低不等于不提。
    """
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": []},
    )

    report = build(directory)

    assert report.verdict == "clean", report.note
    assert report.gaps == [], report.gaps
    assert report.devices_without_readings == ["meter_01"], "设备仍在名单里露名"
    assert _text(directory).splitlines()[0].startswith("结论: 合格"), _text(directory)


@case(id="T_00_00_04_000000_19", clause="free: 平台:测试报告", tags=["报告"])
def test_a_case_that_failed_before_writing_evidence_is_still_a_gap(tmp_path):
    """守卫:用例执行了却失败在写证据之前 —— 那台设备的无读数仍是缺口。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[_device_case(DEVICE_CASE)],
            executed=[DEVICE_NODEID],
            failed=[{"nodeid": DEVICE_NODEID, "case": DEVICE_CASE, "reason": "assert 1 == 0"}],
        ),
        devices={"meter_01": []},
    )

    report = build(directory)

    assert [row["kind"] for row in report.gaps] == ["no_readings"], report.gaps
    assert report.verdict == "incomplete", report.note


@case(id="T_00_00_04_000000_20", clause="free: 平台:测试报告", tags=["报告"])
def test_a_device_case_that_never_ran_keeps_the_gap(tmp_path):
    """守卫:这台设备还有用例没进 executed(没跑到 / 被中断)→ 分不出,照旧拉低。"""
    directory = _round(
        tmp_path,
        scope=_scope(
            collected=[
                _device_case(DEVICE_CASE),
                {"nodeid": "tests/test_meter.py::test_bench_plumbing", "case": None,
                 "devices": ["meter_01"]},
            ],
            executed=[DEVICE_NODEID],
        ),
        devices={"meter_01": []},
    )

    report = build(directory)

    assert [row["kind"] for row in report.gaps] == ["no_readings"], report.gaps
    assert report.verdict == "incomplete", report.note


@case(id="T_00_00_04_000000_12", clause="free: 平台:测试报告", tags=["报告"])
def test_product_exit_and_unexecuted_are_two_separate_statements(tmp_path):
    """03 的两条记录不许合成一句话:产品退出一条,未执行 N 条另一条。"""
    directory = _round(
        tmp_path,
        task=_task(
            process_events=[
                {"kind": "product_exit", "how": "platform_killed", "code": None, "signal": 15,
                 "at": "2026-10-23T00:00:04.000+00:00"},
                {"kind": "unexecuted", "count": 2, "reason": "task_timeout"},
            ]
        ),
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    lines = _text(directory).splitlines()
    exit_line = next(line for line in lines if "产品退出(how=" in line)
    unexecuted_line = next(line for line in lines if line.strip().startswith("未执行 "))

    assert "how=platform_killed" in exit_line and "signal=15" in exit_line
    assert "count" not in exit_line and "未执行" not in exit_line
    assert "how=" not in unexecuted_line
    assert "reason=task_timeout" in unexecuted_line


# ------------------------------------------------------- 机器可读 ----

@case(id="T_00_00_04_000000_13", clause="free: 平台:测试报告", tags=["报告"])
def test_summary_carries_runtime_and_env_like_the_task_record(tmp_path):
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE)]},
    )

    write_report(directory)
    summary = _summary(directory)

    assert set(summary["runtime"]) == {
        "startup_seconds",
        "first_reading_seconds",
        "total_seconds",
    }, summary["runtime"]
    assert set(summary["env"]) == {
        "commit",
        "platform_version",
        "product_sha256",
        "simulator_version",
    }, summary["env"]
    assert summary["columns"]["devices"] == {"passed": 1, "total": 1}
    assert summary["denominator"] == {"collected": 1, "deselected": 0}


@case(id="T_00_00_04_000000_14", clause="free: 平台:测试报告", tags=["报告"])
def test_the_human_report_never_renders_the_clause_summary(tmp_path):
    """人读报告只渲染清单里的 standard + number;summary 是概括,进报告就会被当原文读。"""
    registry = load_clauses()
    directory = _round(
        tmp_path,
        scope=_scope(collected=[_device_case(DEVICE_CASE)], executed=[DEVICE_NODEID]),
        devices={"meter_01": [_reading(DEVICE_CASE, breaches=[BREACH])]},
    )

    text = _text(directory)

    assert STANDARD_LABEL in text
    for clause_id in registry.ids():
        assert registry.get(clause_id).summary not in text, clause_id


# ------------------------------------------------------------ 接线 ----

@case(id="T_00_00_04_000000_15", clause="free: 平台:测试报告", tags=["报告"])
def test_a_real_task_run_leaves_the_report_and_the_summary(tmp_path):
    """执行器跑完真的调报告:一条命令之后,任务目录里两份东西都在。"""
    evidence = tmp_path / "evidence"
    completed = subprocess.run(
        [sys.executable, "tools/run_task.py", str(SHIPPED_TASK), "--evidence-dir", str(evidence)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )

    assert completed.returncode == 0, (
        f"任务没跑通,退出码 {completed.returncode}\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )
    assert (evidence / REPORT_FILE).is_file(), sorted(p.name for p in evidence.iterdir())
    summary = json.loads((evidence / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["task"] == "minimal"
    assert summary["denominator"]["collected"] == 1
    assert summary["verdict"] == "clean", summary["note"]
    # pytest 自己的退出码留在 summary 里给诊断用,但它不参与判据(结论一律由证据判)
    assert summary["pytest_exit_code"] == 0, summary["pytest_exit_code"]
    assert summary["runtime"]["total_seconds"] > 0
    assert summary["env"]["commit"]
    first_line = (evidence / REPORT_FILE).read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("结论: 合格(冒烟:本轮未验设备)"), first_line  # minimal.yaml 是 kind: smoke


# ------------------------------------------------------------- 工具 ----

def _round(
    tmp_path: Path,
    *,
    task: dict | None = None,
    scope: dict | None = None,
    devices: dict | None = None,
    scenarios: dict | None = None,
    platform_errors: str | None = None,
    round_status: dict | None = None,
) -> Path:
    """一个任务目录:与 execution/task.py + 证据层写出来的文件同名同形。"""
    directory = tmp_path / "round"
    directory.mkdir()
    _write(directory, "task.json", task or _task())
    if scope is not None:
        _write(directory, "run-scope.json", scope)
    for device, records in (devices or {}).items():
        (directory / f"{device}.jsonl").write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
    for name, data in (scenarios or {}).items():
        _write(directory, f"scenario-{name}.json", data)
    if platform_errors is not None:
        (directory / "platform-errors.jsonl").write_text(platform_errors, encoding="utf-8")
    _write(directory, "round-status.json", round_status or {"verdict": "clean", "note": "本轮没有断链"})
    return directory


def _task(**overrides) -> dict:
    record = {
        "task": "probe",
        "product": "fake-edgec",
        "select": "case:T_00_00_01_000000",
        "runtime": {
            "startup_seconds": 1.5,
            "first_reading_seconds": 2.25,
            "total_seconds": 7.5,
        },
        "env": {
            "commit": "abc1234",
            "platform_version": "0.1.0",
            "product_sha256": "0" * 64,
            "simulator_version": "1" * 64,
        },
        "process_events": [],
        "error": None,
    }
    record.update(overrides)
    return record


def _scope(*, collected=(), executed=(), skipped=(), deselected=(), failed=(), errored=()) -> dict:
    return {
        "collected": list(collected),
        "deselected": list(deselected),
        "skipped": list(skipped),
        "executed": list(executed),
        "failed": list(failed),
        "errored": list(errored),
    }


def _device_case(case_id: str, device: str = "meter_01") -> dict:
    nodeid = f"tests/test_meter.py::{case_id}"
    return {"nodeid": nodeid, "case": case_id, "devices": [device]}


def _scenario_case(case_id: str) -> dict:
    return {"nodeid": f"tests_scenario/test_grid.py::{case_id}", "case": case_id}


def _reading(case_id: str, *, breaches=(), device: str = "meter_01") -> dict:
    return {
        "kind": "reading",
        "device": device,
        "at": "2026-10-23T00:00:00.000+00:00",
        "case": {"id": case_id, "clause": STANDARD_CLAUSE, "tags": []},
        "criteria": {"profile": "meter_220v", "overridden": [], "criteria": []},
        "readings": {"有功功率": 12.5},
        "evaluation": {"passed": not breaches, "breaches": list(breaches)},
        "events": [],
    }


def _scenario(name: str, case_id: str, verdict: str, steps: list) -> dict:
    return {
        "scenario": name,
        "case": {"id": case_id, "clause": STANDARD_CLAUSE, "tags": []},
        "verdict": verdict,
        "participants": [{"id": "meter_01", "role": "dut"}, {"id": "grid_sim_01", "role": "support"}],
        "steps": steps,
        "cleanup_failed": False,
    }


def _text(directory: Path) -> str:
    return "\n".join(render(build(directory), load_clauses()))


def _summary(directory: Path) -> dict:
    return json.loads((directory / SUMMARY_FILE).read_text(encoding="utf-8"))


def _write(directory: Path, name: str, data: dict) -> None:
    (directory / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
