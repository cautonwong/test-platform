"""
元守卫:pytest 退出码非 0 ⇒ 归因结论不得是 clean。

这张票(25)的缺陷是负责消灭"假绿"的工具自己产生了假绿:一轮 pytest 红了,归因说 clean。
它在写证据之前就崩的用例身上复活过一次 —— 那种用例一条读数都没有,只读证据的归因看不见它。
所以这条守卫必须真跑一轮 pytest(带一条必然失败的用例),不能只测函数返回值:
只测返回值的话,下一次归因再漏一类失败,这里照样绿。

断言两条,缺一不可:
- 子进程退出码非 0(这一轮确实红了);
- 同一个证据目录的归因结论不是 clean,而且失败那条出现在「未落证据的失败」里。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests_support.manifest_factory import manifest
from tests_support.nested_pytest import pytest_command
from testplatform.reporting.attribution import attribute, render, write

REPO = Path(__file__).resolve().parent.parent

MANIFEST = manifest(inverters=1)


SUITE_CONFTEST = '''
import pytest

from testplatform.cases import case_of

pytest_plugins = ["testplatform.plugin"]


@pytest.fixture(autouse=True)
def _record_each_case(composition_root, request):
    yield
    composition_root.record_evidence(identity=case_of(request.node))
'''

# 失败那条在写证据之前就挂:一条读数都没有,证据里只剩"它不存在"。
SUITE_CASES = '''
import pytest

from testplatform.cases import case


@case(id="M_00_00_01_000010_01", clause="GBT37408-5.2.1", tags=["守卫"])
def test_passes_and_leaves_evidence(composition_root):
    inverter = composition_root.get("inv_01")
    inverter.connect()
    assert inverter.read("有功功率") == 3000.0


@case(id="M_00_00_01_000010_02", clause="GBT37408-5.2.2", tags=["守卫"])
def test_fails_before_writing_any_evidence():
    raise AssertionError("这条在写证据之前就失败:证据里没有结论,归因不许说 clean")


@pytest.fixture
def broken_fixture():
    raise RuntimeError("setup 阶段就崩:这一条在 pytest 里是 ERROR,不是 FAILED")


@case(id="M_00_00_01_000010_03", clause="GBT37408-5.2.3", tags=["守卫"])
def test_errors_in_setup(broken_fixture):
    pass
'''


@pytest.fixture(scope="module")
def red_round(tmp_path_factory):
    """真跑一轮会红的 pytest:交回(退出码, 输出, 证据目录)。"""
    tmp_path = tmp_path_factory.mktemp("red_round")
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(MANIFEST, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_red.py").write_text(SUITE_CASES, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command("-q", "--device-config", "devices.yaml", "test_red.py"),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    (directory,) = (tmp_path / "evidence").iterdir()
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace"), directory


def test_a_red_round_is_never_attributed_as_clean(red_round):
    """退出码非 0 ⇒ 归因不得是 clean。这条断言就是这张票的守卫本身。"""
    exit_code, output, directory = red_round
    assert exit_code != 0, f"这一轮本该死给归因看:\n{output}"

    attribution = attribute(directory)
    assert attribution.verdict != "clean", (
        "pytest 退出码非 0,归因却说 clean —— 失败在写证据之前就发生了,"
        f"而归因漏掉了它。归因输出:\n" + "\n".join(render(attribution))
    )


def test_both_kinds_of_non_passing_cases_reach_the_unevidenced_failures_column(red_round):
    """两条非通过项要指名道姓地出现在「未落证据的失败」里,并按 pytest 的 short summary 分两类:
    call 里失败 = failed(FAILED),setup 里崩掉 = errored(ERROR)。"""
    _, output, directory = red_round
    assert "1 failed" in output and "1 error" in output, output

    attribution = attribute(directory)
    text = "\n".join(render(attribution))
    by_node = {
        item["nodeid"].rsplit("::", 1)[-1]: item for item in attribution.unevidenced_failures
    }

    failed = by_node["test_fails_before_writing_any_evidence"]
    assert failed["kind"] == "failed"
    assert failed["case"] == "M_00_00_01_000010_02", "run-scope 里 case 就是编号"
    assert "这条在写证据之前就失败" in failed["reason"]

    errored = by_node["test_errors_in_setup"]
    assert errored["kind"] == "errored"
    assert "setup 阶段就崩" in errored["reason"]
    assert "[未落证据的失败] 2 条" in text

    # run-scope 里这两栏与 pytest 的 short summary 一致:一条 FAILED、一条 ERROR,passed 那条不在里面。
    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    assert [e["nodeid"].rsplit("::", 1)[-1] for e in scope["failed"]] == [
        "test_fails_before_writing_any_evidence"
    ]
    assert [e["nodeid"].rsplit("::", 1)[-1] for e in scope["errored"]] == ["test_errors_in_setup"]


def test_the_machine_readable_attribution_keeps_the_same_verdict(red_round):
    """终端说不是 clean,attribution.json 也必须是同一个结论(退出码只看这一份)。"""
    _, _, directory = red_round

    record = json.loads(write(directory, attribute(directory)).read_text(encoding="utf-8"))

    assert record["verdict"] == "failed-without-evidence"
    assert {item["kind"] for item in record["unevidenced_failures"]} == {"failed", "errored"}
