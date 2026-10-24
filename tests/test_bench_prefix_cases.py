"""
条款的台架前置(bench:):先下发、再写、再回读,失败时两种结论分得开。

一条带 `bench:` 的条款,现场有两种"不成立",结论归属相反(见 `tests/test_setting_cases.py::_apply_bench`):

    前置没发下去(台架连不上 / 下发被拒 / 量不被接受)  → 测试无效(不是设备不合格)
    前置发下去了,表没反应 / 读数不成立               → 设备不合格

证据里靠两样东西分开:台架下发那一刻的帧与 `bench_setup` 记录(在被测设备那一份里,
排在随后的 reading 之前),以及前置失败时落的 `link_lost`(operation: apply_three_phase)。
所以这里真跑一轮 pytest:skip 与 failed 是测试函数的结论,证据是给归因读的那一侧,
两者都只有跑过才算数。
"""
import json
import os
import subprocess
from pathlib import Path

import yaml

from tests_support.nested_pytest import pytest_command
from testplatform.reporting.attribution import attribute

REPO = Path(__file__).resolve().parent.parent
FACTORY_MANIFEST = REPO / "configs" / "devices.yaml"
CASES = REPO / "tests" / "test_setting_cases.py"
GRID = "grid_sim_01"
METER = "meter_01"
CASE_ID = "M_00_00_13_0002FF_61"


def _manifest() -> dict:
    """真清单的一份副本:型号、点表、判据集、设备全照旧,只留这一条带 bench: 的条款。"""
    manifest = yaml.safe_load(FACTORY_MANIFEST.read_text(encoding="utf-8"))
    manifest["setting_cases"] = {
        "dtsd1352_modbus": {CASE_ID: manifest["setting_cases"]["dtsd1352_modbus"][CASE_ID]}
    }
    return manifest


def _grid(manifest) -> dict:
    return next(spec for spec in manifest["devices"] if spec["id"] == GRID)


def _write(tmp_path: Path, manifest) -> str:
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return str(path)


def _run(tmp_path: Path, config: str) -> tuple[int, str]:
    """在临时目录里跑这一条条款:证据落在临时目录,不写进仓库的 evidence/。"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command(
            "-q", "-rs", f"{CASES}::test_setting_roundtrip[{CASE_ID}]",
            "--device-config", config,
        ),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=180,
    )
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")


def _records(tmp_path: Path, device_id: str) -> list:
    (path,) = sorted((tmp_path / "evidence").glob(f"*/{device_id}.jsonl"))
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _evidence_dir(tmp_path: Path) -> Path:
    (session,) = sorted((tmp_path / "evidence").iterdir())
    return session


def test_the_bench_prefix_goes_down_before_the_write_step(tmp_path):
    """执行顺序:连接 → bench: 前置(经周边设备驱动下发)→ steps → 回读判定。

    证据里先有 bench_setup 再有 reading,台架那份有真发出去的 SET_GRID3 帧:
    现场才分得清"前置没下发"与"表没反应"。
    """
    returncode, output = _run(tmp_path, _write(tmp_path, _manifest()))

    assert returncode == 0, output
    assert "1 passed" in output, output

    meter = _records(tmp_path, METER)
    kinds = [record["kind"] for record in meter]
    assert kinds.index("bench_setup") < kinds.index("reading"), kinds
    setup = meter[kinds.index("bench_setup")]
    assert setup["case"]["id"] == CASE_ID and setup["bench"] == GRID
    assert setup["setpoints"]["Ua"] == 220.0 and setup["applied"]["Fre"] == 50.0

    grid_sends = [
        bytes.fromhex(record["hex"]).decode()
        for record in _records(tmp_path, GRID)
        if record["kind"] == "frame" and record["direction"] == "send"
    ]
    assert grid_sends[0].startswith("SET_GRID3 "), grid_sends
    assert "Ua=220.0" in grid_sends[0] and "Fre=50.0" in grid_sends[0]

    assert attribute(_evidence_dir(tmp_path)).verdict == "clean"


def test_a_bench_prefix_that_cannot_be_dispatched_is_a_test_invalid(tmp_path):
    """前置没发下去 → 测试无效:skip 带固定前缀、退出码不变,证据里落一条 link_lost。

    表还没被验过,这时判"设备不合格"就是把台架的问题算到设备头上。
    """
    manifest = _manifest()
    # 台架只回那句固定的单相应答:SET_GRID3 的应答里没有 量名=值 = 这次前置没被接下。
    _grid(manifest)["connection"]["simulated_grid"] = False

    returncode, output = _run(tmp_path, _write(tmp_path, manifest))

    assert returncode == 0, f"测试无效不改退出码:{output}"
    assert "1 skipped" in output, output
    assert "测试无效:" in output and GRID in output, output
    assert "台架应答里没有 量名=值" in output, output

    status = json.loads(
        (_evidence_dir(tmp_path) / "round-status.json").read_text(encoding="utf-8")
    )
    assert status["verdict"] == "needs-review"
    assert "台架前置下发失败" in status["note"] and "断链" not in status["note"], status

    record = attribute(_evidence_dir(tmp_path))
    assert record.verdict == "invalid"
    assert [
        (entry["kind"], entry["operation"]) for entry in record.invalid_tests
    ] == [("link_lost", "apply_three_phase")]
    assert record.device_failures == [], "前置没发下去不是设备不合格"


def test_a_bench_prefix_that_went_down_but_the_meter_disagrees_is_a_device_failure(tmp_path):
    """前置发下去了、读数却不成立 → 设备不合格:证据里 bench_setup 在前、step_failed 在后。

    这条路径与上一条的区别只看证据:都红了,但结论归属相反。
    """
    manifest = _manifest()
    manifest["setting_cases"]["dtsd1352_modbus"][CASE_ID]["steps"] = [
        {"write": "有功计量模式", "value": "1601", "expect": "1600"}
    ]

    returncode, output = _run(tmp_path, _write(tmp_path, manifest))

    assert returncode != 0, output
    assert "1 failed" in output, output

    kinds = [record["kind"] for record in _records(tmp_path, METER)]
    assert kinds.index("bench_setup") < kinds.index("step_failed"), kinds

    record = attribute(_evidence_dir(tmp_path))
    assert record.verdict == "failed"
    assert [entry["kind"] for entry in record.device_failures] == ["step_failed"]
    assert record.invalid_tests == [], "前置发出去了,这一轮有有效结论"
