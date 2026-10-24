"""
票 15 的三条守卫 —— 第一条 acceptance 任务。

它不是"再来一条能跑的任务",而是**证明这份任务真的在验收**:
    ① 产品起不来 → 未完成 + reason=platform_error(不是合格)
    ② 把用例换成不按设备展开的 → 未完成(证明这条任务真的把设备展开成 1/1)
    ③ 单独一份报告 → `tools/gate.py` 退出码 0(它得能当验收通过,否则 CI 永远红)

三条都走真链路:真任务文件、真产品进程、真 pytest 子进程、真报告;没有 mock 传输。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from testplatform.execution.gate import GATE_OK, judge

from testplatform.execution.task import TASK_RECORD_FILE, load_task, run_task

REPO = Path(__file__).resolve().parents[1]
TASK = REPO / "configs" / "tasks" / "acceptance_sample.yaml"
SUMMARY = "summary.json"
# 用例:`tag:验收` 选中的三条(两条按设备的契约用例 + 一条场景用例)。
# 其中 `_02` 走 read_all,证据里会落一条带判据的 reading —— 票 15 要的"真读数"靠它。
DEVICE_CASE = "M_00_00_01_000002_02"
SCENARIO_CASE = "S_00_00_01_000001_01"


def _run(task_path: Path = TASK, evidence: Path | None = None) -> tuple[int, Path]:
    """跑一份任务,返回 (退出码, 证据目录)。任务跑不起来时异常照旧抛给调用方。

    证据目录按 CI 的形状给:`<reports>/<task>/` —— gate 按 `<目录>/*/summary.json` 找报告。
    """
    evidence = evidence or (REPO / "reports" / "acceptance_sample")
    exit_code = run_task(load_task(task_path), evidence_dir=evidence)
    return exit_code, evidence


def _summary(evidence: Path) -> dict:
    return json.loads((evidence / SUMMARY).read_text(encoding="utf-8"))


def _record(evidence: Path) -> dict:
    return json.loads((evidence / TASK_RECORD_FILE).read_text(encoding="utf-8"))


def _variant(tmp_path: Path, **overrides) -> Path:
    """拿真实任务文件改一处,落进 tmp —— 守卫要验的正是"改了那一处之后会怎样"。"""
    written = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    written.update(overrides)
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(written, allow_unicode=True), encoding="utf-8")
    return path


# ------------------------------------------------- 这份任务是真的验收 ----

def test_the_sample_acceptance_task_qualifies_with_one_device_and_one_scenario(tmp_path):
    """设备级 1/1、场景级 1/1、verdict=合格,而且证据里看得到"真验了"。"""
    root = tmp_path / "reports"
    exit_code, evidence = _run(evidence=root / "acceptance_sample")

    assert exit_code == 0
    summary = _summary(evidence)
    assert summary["kind"] == "acceptance"
    assert summary["verdict"] == "clean", summary["note"]
    assert summary["columns"] == {
        "devices": {"passed": 1, "total": 1},
        "scenarios": {"passed": 1, "total": 1},
    }

    record = _record(evidence)
    # 端口进了实际命令行:这是"产品从平台分配的端口起来"的唯一证据
    assert str(record["product_port"]) in record["launch"]
    # TODO(票 14):合入后补三条实断言 —— `port_source` / `port_range` / `port_attempts`。
    # 14 正在改 execution/task.py(端口段接线),本票只消费字段,自己不许加;等它合入再补。
    assert record["env"]["commit"], "env.commit 不许缺失(没有 .git 时报 unknown 也算有值)"

    # 读数:每设备 JSONL 里真有本轮的读数记录
    readings = [
        json.loads(line)
        for line in (evidence / "meter_01.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["kind"] for entry in readings if entry["kind"] == "reading"], readings
    # 用例:范围里被执行的是那两条,且编号能指认
    scope = json.loads((evidence / "run-scope.json").read_text(encoding="utf-8"))
    executed = " ".join(scope["executed"])
    assert "test_meter_reading_is_recorded_in_history" in executed
    assert "test_inverter_holds_the_grid_frequency_setpoint" in executed


def test_the_task_file_says_it_is_a_sample_not_the_edgec_product():
    """示例产品必须在任务文件里写明 —— 否则报告会被读成"EdgeC 验收通过"。"""
    written = TASK.read_text(encoding="utf-8")

    assert "kind: acceptance" in written
    assert "不是 EdgeC" in written
    assert "fake_product.py" in written


# -------------------------------------------------------- 守卫 ① 产品起不来 ----

def test_a_product_that_cannot_start_is_incomplete_not_qualified(tmp_path):
    """产品起不来 = 平台错误(未完成),不是合格 —— 这一条就是"假绿"的正面堵法。"""
    broken = _variant(
        tmp_path,
        launch=["${PYTHON}", "-c", "import sys; sys.exit(2)", "--port", "${PORT}"],
    )
    evidence = tmp_path / "reports" / "acceptance_sample"

    with pytest.raises(Exception) as error:
        _run(broken, evidence=evidence)
    assert "就绪前退出" in str(error.value) or "没起来" in str(error.value), str(error.value)

    summary = _summary(evidence)
    assert summary["verdict"] == "incomplete"
    assert summary["reason"] == "platform_error"
    assert summary["verdict"] != "clean", "产品没起来却判合格 = 假绿"


# ------------------------------------------- 守卫 ② 不按设备展开 → 未完成 ----

def test_a_round_without_device_parameterized_cases_is_incomplete(tmp_path):
    """只选场景用例 → 设备级 0/0 → 未完成。反过来证明这份任务真的把设备展开成 1/1。"""
    scenario_only = _variant(tmp_path, select=f"case:{SCENARIO_CASE}")
    evidence = tmp_path / "reports" / "acceptance_sample"

    exit_code, evidence = _run(scenario_only, evidence=evidence)

    summary = _summary(evidence)
    assert summary["columns"]["devices"] == {"passed": 0, "total": 0}
    assert summary["verdict"] == "incomplete", summary["note"]
    assert summary["reason"] == "no_device_coverage"
    assert exit_code == 3, "未完成的退出码是 3(05 的契约)"


# ------------------------------------------------- 守卫 ③ 门禁判它合格 ----

def test_the_gate_passes_a_single_acceptance_report(tmp_path):
    """CI 的那一条 gate 命令必须对这份报告 exit 0 —— 否则第 6 档永远红。"""
    root = tmp_path / "reports"
    _run(evidence=root / "acceptance_sample")

    decision = judge([_summary(root / "acceptance_sample")])

    assert decision.exit_code == GATE_OK == 0, decision.reason
    assert "全部合格" in decision.reason
    # 单独跑命令行入口,退出码与函数一致(CI 走的是这条)
    from testplatform.execution.gate import main

    assert main([str(root)]) == 0


def test_a_copy_of_the_reports_directory_is_enough_for_the_gate(tmp_path):
    """CI 里 gate job 拿到的只有 artifact(不是原目录的其它东西):单独一份报告就够判。"""
    root = tmp_path / "reports"
    _run(evidence=root / "acceptance_sample")

    only_reports = tmp_path / "downloaded" / "acceptance_sample"
    only_reports.parent.mkdir()
    shutil.copytree(root / "acceptance_sample", only_reports)

    assert judge([_summary(only_reports)]).exit_code == GATE_OK
