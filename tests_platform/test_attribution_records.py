"""
归因入口的现场记录:真的跑一遍 pytest,再看证据目录里留下了什么。

三件事只有真跑一遍才证明得了:
- run-scope.json 记下了这一轮的范围 —— 被 --select 筛掉的、被 real_hardware 跳过的都在;
- 聚合根不变量被违反时,platform-errors.jsonl 里有一条现场记录(设备 / 异常 / 用例 nodeid),
  它不是任何设备的"不合格";
- 归因入口据此把四类分对:设备不合格、测试无效、平台错误、没有有效结论,一条不并。

子进程里跑的是真插件(testplatform/plugin),只是清单与用例都是这里现造的。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests_support.manifest_factory import manifest
from tests_support.nested_pytest import pytest_command
from testplatform.reporting.attribution import attribute, render

REPO = Path(__file__).resolve().parent.parent

# 频率读 60Hz:判据集是 50Hz 那一套,这一台的读数注定判不合格。
MANIFEST = manifest(
    inverters=1,
    mutate=lambda m: m["devices"][0]["connection"].update(
        simulated_response="3000.0 230.0 60.0"
    )
)


SUITE_CONFTEST = '''
import pytest

from testplatform.cases import case_of

pytest_plugins = ["testplatform.plugin"]


@pytest.fixture(autouse=True)
def _record_each_case(composition_root, request):
    """与 tests/conftest.py 同一条缝:每条用例结束时按 @case 身份落证据。"""
    yield
    composition_root.record_evidence(identity=case_of(request.node))
'''

PYTEST_INI = """[pytest]
markers =
    real_hardware: 只有真实硬件才有意义
"""

SUITE_CASES = '''
import pytest

from testplatform.cases import case
from testplatform.domain.exceptions import InvariantViolation


@case(id="M_00_00_01_000009_01", clause="GBT37408-5.2.1", tags=["sel"])
def test_the_dut_reads_within_the_criteria(composition_root):
    inverter = composition_root.get("inv_01")
    inverter.connect()
    assert inverter.read_all().readings["有功功率"] == 3000.0


@case(id="M_00_00_01_000009_02", clause="GBT37408-5.2.2", tags=["sel"])
def test_the_platform_violates_its_own_invariant(composition_root):
    composition_root.get("inv_01").connect()
    raise InvariantViolation("SOC 越出 [0, 100]: 120")


@case(id="M_00_00_01_000009_03", clause="GBT37408-5.2.3", tags=["sel"])
def test_the_dut_breaks_a_criterion(composition_root):
    inverter = composition_root.get("inv_01")
    inverter.connect()
    assert inverter.read_all().readings["频率"] == 60.0


@case(id="M_00_00_01_000009_04", clause="GBT37408-5.2.4", tags=["sel"])
@pytest.mark.real_hardware
def test_only_meaningful_on_real_hardware(composition_root):
    """--force-simulator 下收集期就跳过:这一条不进"跑到了"。"""


@case(id="M_00_00_01_000009_05", clause="GBT37408-5.2.5", tags=["other"])
def test_filtered_out_by_select(composition_root):
    """--select tag:sel 把这台筛掉:归因里必须是独立一类,不许读成通过。"""
'''


def _run_round(tmp_path):
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    # 标记在这里注册一次:真仓库用 pyproject 注册,这里只是一份迷你套件。
    (tmp_path / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(MANIFEST, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_round.py").write_text(SUITE_CASES, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command("-q", "-rs", "--force-simulator", "--select", "tag:sel",
                       "--device-config", "devices.yaml", "test_round.py"),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    (directory,) = (tmp_path / "evidence").iterdir()
    return result.returncode, output, directory


@pytest.fixture(scope="module")
def round_result(tmp_path_factory):
    """整份文件只跑一遍 pytest:一轮证据目录喂三个断言,别为省事跑三遍。"""
    return _run_round(tmp_path_factory.mktemp("round"))


def test_the_round_scope_records_filtered_and_skipped_cases(round_result):
    """被 --select 筛掉、被 real_hardware 跳过的用例都留在 run-scope.json 里:
    它们是"没跑",不是"通过",归因据此才说得清这一轮到底跑了多少条。"""
    _, output, directory = round_result
    assert "2 passed" in output and "1 failed" in output, output
    assert "1 skipped" in output, output

    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    assert sorted(entry["case"] for entry in scope["collected"]) == [
        "M_00_00_01_000009_01",
        "M_00_00_01_000009_02",
        "M_00_00_01_000009_03",
        "M_00_00_01_000009_04",
    ]
    (deselected,) = scope["deselected"]
    assert deselected["case"] == "M_00_00_01_000009_05"
    assert "--select" in deselected["reason"]
    (skipped,) = scope["skipped"]
    assert skipped["case"] == "M_00_00_01_000009_04"
    assert "real_hardware" in skipped["reason"]


def test_an_invariant_violation_leaves_a_platform_error_record(round_result):
    """不变量被违反是平台自己的 bug:现场就落在 platform-errors.jsonl 里,
    带设备、异常类型、消息与用例 nodeid —— 事后不必靠"证据看起来矛盾"去推断。"""
    _, _, directory = round_result

    lines = (directory / "platform-errors.jsonl").read_text(encoding="utf-8").splitlines()
    (record,) = [json.loads(line) for line in lines]
    assert record["error"] == "InvariantViolation"
    assert record["message"] == "SOC 越出 [0, 100]: 120"
    assert record["case"]["id"] == "M_00_00_01_000009_02"
    assert record["nodeid"].endswith("test_the_platform_violates_its_own_invariant")


def test_the_four_buckets_line_up_and_every_entry_points_back_to_evidence(round_result):
    """四类各归各位:不合格的读数进"设备不合格",平台 bug 进"平台错误",
    被筛掉/被跳过的进"没有有效结论" —— 谁也不并进谁,每条都指回证据。"""
    _, _, directory = round_result

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    # 模拟器恒定回 60Hz:两条读了数的用例都越界,各归各的用例身份。
    failure = next(
        entry for entry in attribution.device_failures
        if entry["case"]["id"] == "M_00_00_01_000009_03"
    )
    assert (failure["device"], failure["quantity"]) == ("inv_01", "grid_freq_hz")
    assert failure["value"] == 60.0 and failure["limit"] == 55
    assert failure["case"]["clause"] == "GBT37408-5.2.3"

    (platform_error,) = attribution.platform_errors
    assert platform_error["error"] == "InvariantViolation"

    kinds = {entry["kind"] for entry in attribution.no_conclusion}
    assert {"deselected", "skipped", "not_run"} <= kinds
    assert attribution.invalid_tests == [], "这一轮没有断链,不许凭空造出「测试无效」"
    assert attribution.verdict == "platform-error"

    # 能追回证据:归因里的那一行,就是设备 JSONL 里的那一行。
    line_no = int(failure["ref"].split(":")[1])
    record = json.loads(
        (directory / "inv_01.jsonl").read_text(encoding="utf-8").splitlines()[line_no - 1]
    )
    assert record["case"]["id"] == failure["case"]["id"]
    assert record["evaluation"]["breaches"][0]["quantity"] == failure["quantity"]
    assert record["evaluation"]["breaches"][0]["exceeded_by"] == 5.0

    # 人读的那份把这四节都摆出来,不让读者自己去翻 json。
    for section in ("[设备不合格]", "[测试无效]", "[平台错误]", "[没有有效结论]"):
        assert section in text
    assert "M_00_00_01_000009_05" in text and "被 --select / 适用矩阵筛掉" in text
