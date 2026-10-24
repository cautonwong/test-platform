"""
`@case` 编号的唯一性:两个函数重号 = 收集期报错;一个函数参数化出多个节点 = 合法,但证据不能互相覆盖。

编号是归因与证据的唯一身份,所以它只有一条规则:一个测试函数一个编号。
参数化把同一个函数展开成 N 个节点(按设备、按设定值),N 个节点共用一个编号 ——
这是同一条用例跑了 N 轮,不是 N 条用例。

两件事都只有真跑一遍才证明得了:重号在收集阶段就停下(不是跑到一半才发现),以及
GB/T 37408 的四个频率点各落一份场景记录、谁都不覆盖谁。子进程里跑的是真插件,
清单与用例都是这里现造的。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests_support.nested_pytest import pytest_command

REPO = Path(__file__).resolve().parent.parent
DEVICES = REPO / "configs" / "devices.yaml"

CASE_ID = "S_00_00_01_000001_01"
FREQS = [47.5, 49.5, 50.5, 51.5]

SUITE_CONFTEST = '''
pytest_plugins = ["testplatform.plugin"]
'''

DUPLICATE_CASES = f'''
from testplatform.cases import case


@case(id="{CASE_ID}", clause="GBT37408-5.2.1", tags=["场景"])
def test_the_inverter_holds_the_frequency_setpoint():
    """两条不同的用例写了同一个编号:归因分不清证据属于谁。"""


@case(id="{CASE_ID}", clause="GBT37408-5.2.1", tags=["场景"])
def test_the_inverter_holds_the_voltage_setpoint():
    """同一个编号的第二个函数。"""
'''

PARAMETRIZED_SCENARIO = '''
import json
import os
from pathlib import Path

import pytest
import yaml

from testplatform.cases import case, case_of
from testplatform.composition.root import CompositionRoot
from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.scenario import ScenarioStep, record_scenario

REPO = Path(os.environ["TESTPLATFORM_REPO"])
# 这条用例自己的场景记录放固定目录:4 个参数节点共用一份,谁都不能覆盖谁。
EVIDENCE = Path("evidence/grid-frequency")
GRID, INVERTER = "grid_sim_01", "inv_01"
SETPOINT_V = 230.0


@case(id="''' + CASE_ID + '''", clause="GBT37408-5.2.1", tags=["场景"])
@pytest.mark.parametrize("freq_hz", ''' + repr(FREQS) + ''')
def test_inverter_holds_the_grid_frequency_setpoint(request, freq_hz, tmp_path):
    """GB/T 37408 的频率点是多点:一个节点一个设定值,场景记录必须各落一份。"""
    manifest = yaml.safe_load((REPO / "configs" / "devices.yaml").read_text(encoding="utf-8"))
    for device in manifest["devices"]:
        if device["id"] == GRID:
            device["connection"]["simulated_response"] = f"{SETPOINT_V} {freq_hz}"
        if device["id"] == INVERTER:
            device["connection"]["simulated_response"] = f"3000.0 230.0 {freq_hz}"
    config = tmp_path / "devices.yaml"
    config.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")

    root = CompositionRoot.build(str(config), evidence=EvidenceSession(EVIDENCE))
    try:
        grid = root.get(GRID)
        grid.connect()
        setpoint = grid.apply_grid(SETPOINT_V, freq_hz)

        inverter = root.get(INVERTER)
        inverter.connect()
        measured = inverter.read_all()
        evaluation = inverter.device.latest_reading

        steps = [
            ScenarioStep(
                GRID,
                f"设定电网 {SETPOINT_V}V / {freq_hz}Hz",
                abs(setpoint.freq_hz - freq_hz) < 0.1,
                {"requested_freq_hz": freq_hz, "applied_freq_hz": setpoint.freq_hz},
            ),
            ScenarioStep(
                INVERTER,
                "按点表读全部读数键并由判据集求值",
                evaluation.passed,
                {
                    "readings": dict(measured.readings),
                    "breaches": [breach.describe() for breach in evaluation.breaches],
                },
            ),
        ]
        # name 用 request.node.name(带参数 id),不是 originalname:后者是 4 个节点共用的名字,
        # 4 份记录会往同一个文件名上覆盖,只剩最后一个设定值。
        path = record_scenario(
            root.evidence,
            name=request.node.name,
            identity=case_of(request.node),
            participants={
                GRID: root.role_of(GRID),
                INVERTER: root.role_of(INVERTER),
            },
            reading_keys={INVERTER: tuple(measured.readings)},
            steps=steps,
        )
    finally:
        root.teardown()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["case"]["id"] == "''' + CASE_ID + '''", "参数化不改编号"
'''


def _run_round(tmp_path: Path, suite_source: str, *args: str):
    """把一组用例在子进程里真跑一轮(真插件、真收集),交回(输出, 退出码, 会话目录)。

    清单跟着写入 tmp 并**相对路径**传给 --device-config:绝对路径会被 pytest 当成收集目标
    拿去算 rootdir,套上仓库自己的 pyproject.toml,nodeid 就不再是 test_round.py::... 了。
    """
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(DEVICES.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "test_round.py").write_text(suite_source, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
        "TESTPLATFORM_REPO": str(REPO),
    }
    result = subprocess.run(
        pytest_command("-q", "--device-config", "devices.yaml", "test_round.py", *args),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    return (result.stdout + result.stderr).decode("utf-8", "replace"), result.returncode, tmp_path


def test_two_functions_sharing_one_case_id_fail_at_collection(tmp_path):
    """重号是收集期错误(退出码 4),不是跑到一半才发现的同名证据;消息里给出两个 nodeid。"""
    output, returncode, _ = _run_round(tmp_path, DUPLICATE_CASES, "--no-evidence")

    assert returncode == 4, output
    assert f"@case id {CASE_ID} 挂在两个不同的测试函数上" in output, output
    assert "test_round.py::test_the_inverter_holds_the_frequency_setpoint" in output, output
    assert "test_round.py::test_the_inverter_holds_the_voltage_setpoint" in output, output


@pytest.fixture(scope="module")
def parametrized_round(tmp_path_factory):
    """4 个设定值真跑一轮:一轮证据目录喂多条断言,别为省事跑两遍。"""
    return _run_round(tmp_path_factory.mktemp("parametrized"), PARAMETRIZED_SCENARIO, "-vv")


def test_parametrized_nodes_of_one_function_are_not_a_duplicate(parametrized_round):
    """同一函数按设定值展开成 4 个节点:不报重号,4 个节点全跑,nodeid 各自带参数。"""
    output, returncode, _ = parametrized_round

    assert returncode == 0, output
    assert "4 passed" in output, output
    for freq in FREQS:
        assert (
            f"test_round.py::test_inverter_holds_the_grid_frequency_setpoint[{freq}] PASSED"
            in output
        ), output


def test_each_parametrized_node_leaves_its_own_scenario_record(parametrized_round):
    """4 个设定值 → 4 份互不覆盖的场景记录,每份都能对上自己那个设定值。"""
    _, _, directory = parametrized_round

    records = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "evidence" / "grid-frequency").glob("scenario-*.json"))
    }
    assert len(records) == len(FREQS), f"4 个设定值要 4 份记录,实际 {sorted(records)}"
    for freq in FREQS:
        (name,) = [name for name in records if f"[{freq}]" in name]
        record = records[name]
        assert record["case"]["id"] == CASE_ID, "编号不拼参数"
        assert record["verdict"] == "passed", record
        assert record["steps"][0]["detail"]["requested_freq_hz"] == freq
        assert record["steps"][0]["detail"]["applied_freq_hz"] == pytest.approx(freq)
        assert record["steps"][1]["detail"]["readings"]["频率"] == pytest.approx(freq)
