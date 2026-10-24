"""
含人工步骤的条款,端到端:清单能写 → 自动步骤先跑 → 跑到人工步骤才 skip(退出码 0)→
证据里既有自动步骤的写入与读数、也有人工占位 → 归因说得出"已执行几步、还剩几步"。

真跑一轮子进程 pytest,而不是只测函数返回值:这张票要防的是"含人工步骤的用例被判通过"
那种静默,而 skip 只体现在真跑一轮的报告与证据里。子进程跑的是仓库自己那份
tests/test_setting_cases.py —— 复制一份测试函数就等于测副本,副本漂了没人知道。

两条守卫各钉住一边:
    自动步骤失败(在人工步骤之前)→ 条款 failed,不是 skipped:真故障不许被"等人确认"盖住
    自动步骤全过 + 有人工步骤 → skipped,但证据里看得到那些步骤真的跑过
"""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests_support.nested_pytest import pytest_command
from testplatform.reporting.attribution import attribute, render

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "tests" / "test_setting_cases.py"
NODEID = "test_setting_roundtrip[M_00_00_13_000101_01]"
CASE_ID = "M_00_00_13_000101_01"

# 一条条款两步:一个能自动写的点 + 一次人工切换上位机通信模式。
# 上游实测 17% 的步骤长这样(切换模式 / 目视看液晶 / 拔插卡),平台只能表达后者为 manual。
MANIFEST = {
    "models": {
        "手册电表": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "capabilities": ["有功计量模式"],
            "point_table": {
                "有功计量模式": {
                    "address": 3,
                    "length": 2,
                    "encoding": "uint",
                    "writable": True,
                }
            },
        }
    },
    "setting_cases": {
        "手册电表": {
            CASE_ID: {
                "clause": "DLT645-4.5.2",
                "steps": [
                    {"write": "有功计量模式", "value": "1601", "expect": "1601"},
                    {
                        "manual": "切换上位机通信模式为 HDLC",
                        "expect": "由操作员确认已切换",
                    },
                ],
            }
        }
    },
    "devices": [
        {
            "id": "meter_manual",
            "model": "手册电表",
            "access": "serial",
            "mode": "simulator",
            "bus_address": 1,
            "connection": {"port": "/dev/ttyUSB0", "simulated_registers": {"3": 1601}},
        }
    ],
}


def _run_round(tmp_path, manifest):
    """真跑一轮:交回(退出码, 输出, 证据目录)。"""
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command(
            "-v",
            "-rs",
            "--device-config",
            "devices.yaml",
            str(SUITE),
            "-k",
            "test_setting_roundtrip",
        ),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    (directory,) = (tmp_path / "evidence").iterdir()
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace"), directory


@pytest.fixture(scope="module")
def manual_round(tmp_path_factory):
    """自动那一步写得进去、读回相等:跑到人工步骤才停。"""
    return _run_round(tmp_path_factory.mktemp("manual_round"), MANIFEST)


@pytest.fixture(scope="module")
def failing_round(tmp_path_factory):
    """同一个模子,只把自动步骤的期望值改错:人工步骤之前就已经不成立。"""
    manifest = copy.deepcopy(MANIFEST)
    steps = manifest["setting_cases"]["手册电表"][CASE_ID]["steps"]
    steps[0]["expect"] = "1600"  # 写 1601、读回 1601,期望 1600 → 这一步不成立
    return _run_round(tmp_path_factory.mktemp("failing_round"), manifest)


def _records(directory: Path, device_id: str) -> list[dict]:
    path = directory / f"{device_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_clause_with_manual_steps_is_skipped_with_a_readable_reason(manual_round):
    """待人工确认 = 第三种状态:报告里 SKIPPED、退出码 0、理由说得出“要人做什么”与“跑到哪一步”。"""
    exit_code, output, directory = manual_round

    assert exit_code == 0, output
    assert "1 skipped" in output and NODEID in output, output
    assert "含 1 个待人工确认步骤" in output and "切换上位机通信模式为 HDLC" in output
    # 停在人工步骤之前,不是一步没跑:已经跑过的自动步骤写进理由。
    assert "前面的 1 个自动步骤已执行且通过" in output

    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    (skipped,) = scope["skipped"]
    assert skipped["nodeid"].endswith("test_setting_roundtrip[M_00_00_13_000101_01]")
    # 运行期的 reason 前面带 pytest 自己的 “Skipped: ” 前缀。
    assert "含 1 个待人工确认步骤" in skipped["reason"]
    # 与“测试无效”(台架断链 / 复位失败)必须措辞分开:下一步该找的是操作员,不是查线。
    assert "测试无效" not in skipped["reason"]


def test_the_automatic_steps_run_before_the_manual_step_stops_the_clause(manual_round):
    """守卫:自动步骤全过 + 有人工步骤 → skipped,但证据里看得到那些步骤真的跑过。

    整条 skip 的代价就藏在这里:条款里能抓设备故障的那几步(写参数、读回、判相等)跟着一起没了。
    """
    _, _, directory = manual_round
    records = _records(directory, "meter_manual")

    (write,) = [record for record in records if record["kind"] == "write"]
    assert write["key"] == "有功计量模式" and write["value"] == 1601.0
    assert write["verified"] is True and write["read_back"] == 1601.0

    (reading,) = [record for record in records if record["kind"] == "reading"]
    assert reading["readings"]["有功计量模式"] == 1601.0
    assert reading["case"]["id"] == CASE_ID

    (manual,) = [record for record in records if record["kind"] == "manual_step"]
    assert manual["device"] == "meter_manual"
    assert manual["action"] == "切换上位机通信模式为 HDLC"
    assert manual["expect"] == "由操作员确认已切换"
    assert manual["confirmed"] is None
    assert manual["executed_steps"] == 1, "停在人工步骤之前,自动那一步已经跑过去了"
    assert manual["case"]["id"] == CASE_ID


def test_attribution_says_how_many_steps_ran_and_how_many_are_pending(manual_round):
    """归因:不是设备不合格、不是测试无效、不是证据损坏,而是「没有有效结论」——
    并且说得出“已执行 N 步全部通过、还有 M 步待人工确认”。"""
    _, _, directory = manual_round

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.device_failures == []
    assert attribution.invalid_tests == []
    assert attribution.evidence_corrupt == [] and attribution.unrecognized == []
    (manual,) = [entry for entry in attribution.no_conclusion if entry["kind"] == "manual_step"]
    assert manual["device"] == "meter_manual" and manual["confirmed"] is None
    assert manual["executed_steps"] == 1 and manual["pending_steps"] == 1
    assert "[没有有效结论]" in text and "待人工确认 1 条" in text, text
    assert "已执行 1 个自动步骤且全部通过,还有 1 步待人工确认" in text, text
    # 这一轮确实一条用例都没跑到(跳过不算执行):待人工确认不是通过,归因不许说这一轮全绿。
    assert attribution.verdict == "incomplete", text


def test_a_failed_automatic_step_fails_the_clause_instead_of_skipping_it(failing_round):
    """守卫:人工步骤之前就有自动步骤不成立 → 这条条款 failed,不是 skipped。

    这是硬要求:真故障不能被“等人确认”盖住 —— 先跑自动步骤的全部意义就在这里,
    否则 17% 的人工步骤会变成一个可以藏住任何设备故障的大盖子。
    """
    exit_code, output, directory = failing_round

    assert exit_code != 0, output
    assert "1 failed" in output and NODEID in output, output
    assert "待人工确认" not in output, "自动步骤已经失败,不许再把这条条款挂到等人确认上"

    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    assert scope["skipped"] == [], "失败就是失败,不能同时占一条跳过"
    assert [entry["nodeid"] for entry in scope["failed"]] == [
        f"tests/test_setting_cases.py::{NODEID}"
    ]
    assert scope["executed"] == [f"tests/test_setting_cases.py::{NODEID}"]

    records = _records(directory, "meter_manual")
    (failed,) = [record for record in records if record["kind"] == "step_failed"]
    assert failed["key"] == "有功计量模式"
    assert failed["value"] == 1601.0 and failed["expected"] == "1600"
    assert failed["case"]["id"] == CASE_ID
    assert [record for record in records if record["kind"] == "manual_step"] == [], (
        "没跑到人工步骤,就不该留下它的占位"
    )

    attribution = attribute(directory)
    assert attribution.verdict == "failed", render(attribution)
    (failure,) = attribution.device_failures
    assert failure["kind"] == "step_failed" and failure["device"] == "meter_manual"
    assert failure["quantity"] == "有功计量模式" and failure["expected"] == "1600"
    # 已经有结论的失败不再列进“未落证据”:同一条用例不许一边说不合格、一边说没证据。
    assert attribution.unevidenced_failures == []
    text = "\n".join(render(attribution))
    assert "[设备不合格] 1 条" in text and "有功计量模式 期望 '1600',实测 1601.0" in text, text
