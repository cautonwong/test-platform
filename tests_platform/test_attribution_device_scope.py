"""
设备范围:run-scope.json 记下每条用例落在哪些设备上,归因据此把「没有有效结论」分成两档 ——
不在本轮范围(只给一行计数)与在范围内却没读数(逐台列出)。

证据只有两件事知道得清:哪些设备在范围内、这份证据里有没有读数。`executed` 再把最后一层分开:
用例跑到了却没读数(选中的用例本来就不读数)与根本没跑到(跳过 / 筛掉)不写同一句。

只有真跑一遍才证明得了的几件事:收集期真把 devices 写进了 run-scope.json;一台在范围内、
却一条读数都没留下的设备仍然逐台列着(没被当成"范围外"埋掉);只跑连接用例时也不会
被写成漏测。

子进程里跑的是真插件(testplatform/plugin),清单与用例都是这里现造的。
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

CASE_ID = "M_00_00_01_000010_01"

MANIFEST = manifest(inverters=2, meters=1)


SUITE_CONFTEST = '''
import pytest

from testplatform.cases import case_of

pytest_plugins = ["testplatform.plugin"]

INVERTERS = ["inv_01", "inv_02"]


def pytest_generate_tests(metafunc):
    if "inverter" in metafunc.fixturenames:
        metafunc.parametrize("inverter", INVERTERS, indirect=True)


@pytest.fixture()
def inverter(composition_root, request):
    """间接参数化:request.param 就是设备 id,与插件记设备范围用的是同一份。"""
    return composition_root.get(request.param)


@pytest.fixture()
def inverter_id(inverter):
    """跟着泛型 fixture 走:同一个用例里两者永远指向同一台设备。"""
    return inverter.device.id


@pytest.fixture(autouse=True)
def _record_each_case(composition_root, request):
    yield
    composition_root.record_evidence(identity=case_of(request.node))
'''

SUITE_CASES = f'''
from testplatform.cases import case


@case(id="{CASE_ID}", clause="GBT37408-5.2.1", tags=["一致性"])
def test_the_dut_reads(inverter, inverter_id):
    """inv_02 在范围内,却中途崩了:它一条读数都不会留下。"""
    if inverter_id == "inv_02":
        raise RuntimeError("用例中途崩了:这台设备在范围内,却没有读数")
    inverter.connect()
    assert inverter.read("有功功率") == 3000.0
'''

CONNECT_ONLY_CASES = f'''
from testplatform.cases import case


@case(id="{CASE_ID}", clause="GBT37408-5.2.1", tags=["接入"])
def test_the_dut_connects(inverter):
    """接入用例按设计不读数:连上就是这条用例的全部内容。"""
    inverter.connect()
'''


def _run_round(tmp_path: Path, cases_source: str, *args: str) -> tuple[str, Path]:
    """把一份清单 + 一组用例在子进程里真跑一轮,交回(输出, 证据目录)。"""
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(MANIFEST, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_scope.py").write_text(cases_source, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command("-q", "--device-config", "devices.yaml", "test_scope.py", *args),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    (directory,) = (tmp_path / "evidence").iterdir()
    return (result.stdout + result.stderr).decode("utf-8", "replace"), directory


@pytest.fixture(scope="module")
def round_result(tmp_path_factory):
    """整份文件只跑一遍 pytest:一轮证据目录喂多条断言,别为省事跑两遍。"""
    return _run_round(tmp_path_factory.mktemp("scope"), SUITE_CASES)


@pytest.fixture(scope="module")
def connect_only_result(tmp_path_factory):
    """只跑「连接」用例的一轮:用例按设计不读数,设备证据文件全是空的。"""
    return _run_round(tmp_path_factory.mktemp("connect_only"), CONNECT_ONLY_CASES, "-k", "connects")


def test_the_round_scope_records_which_devices_each_case_lands_on(round_result):
    """按设备参数化的用例在 run-scope.json 里带 devices:归因分不分得清"范围外"就靠它。"""
    output, directory = round_result
    assert "1 passed" in output and "1 failed" in output, output

    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    assert sorted(entry["devices"] for entry in scope["collected"]) == [["inv_01"], ["inv_02"]]


def test_an_in_scope_device_without_readings_is_listed_a_meter_nobody_touched_is_not(round_result):
    """两档必须分开:inv_02 在范围内却没有读数(逐台列),
    meter_01 一条用例都没落在它身上(不在本轮范围,只给一行计数)。"""
    _, directory = round_result

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    (empty,) = [entry for entry in attribution.no_conclusion if entry["kind"] == "no_readings"]
    assert empty["device"] == "inv_02" and empty["ref"] == "inv_02.jsonl"
    assert empty["case_nodeids"] == ["test_scope.py::test_the_dut_reads[inv_02]"]
    assert "设备 inv_02" in text, text

    assert attribution.out_of_scope == ["meter_01"]
    assert "另有 1 台设备不在本轮范围内" in text
    assert "设备 meter_01  证据" not in text, "范围外的设备不许逐条列"


def test_a_connect_only_round_lists_the_devices_but_claims_no_miss(connect_only_result):
    """只跑接入用例:两台设备仍逐台列出(真该测没测时靠这一档),
    但话说到底 —— 用例进了 executed,所以是「用例本来就不读数」,不写成漏测。"""
    output, directory = connect_only_result
    assert "2 passed" in output, output

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    listed = [entry for entry in attribution.no_conclusion if entry["kind"] == "no_readings"]
    assert [entry["device"] for entry in listed] == ["inv_01", "inv_02"]
    for entry in listed:
        nodeid = f"test_scope.py::test_the_dut_connects[{entry['device']}]"
        assert "本来就不读数" in entry["reason"]
        assert entry["case_nodeids"] == [nodeid] and entry["executed_nodeids"] == [nodeid]
        assert f"设备 {entry['device']}" in text
    assert "漏测" not in text and "该测却没测到" not in text, text
    assert attribution.verdict == "clean", "没有读数不是失败"

    assert attribution.out_of_scope == ["meter_01"], "没被点到的设备仍只给一行计数"
    assert "另有 1 台设备不在本轮范围内" in text
