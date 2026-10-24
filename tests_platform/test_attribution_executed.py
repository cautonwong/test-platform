"""
「这一轮跑了 N 条」必须是真正执行过的条数,不是「收集 − 跳过」。

只有真跑一遍才证明得了的三件事:
- `--maxfail=1` 在第一条失败后停下,剩下的用例没执行,规模行就不许把它们算进去;
- 正常跑完的一轮,`run-scope.json` 的 executed 条数 = pytest 报告里跑出结论的条数,跳过的另算;
- 同一份机器可读输出里不能出现两处矛盾的「跑了多少条」(`ran.executed` 与终端规模行同源)。

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
from testplatform.reporting.attribution import attribute, render, write

REPO = Path(__file__).resolve().parent.parent

CASE_IDS = [f"M_00_00_01_00002{i}_01" for i in range(5)]

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

# 五条用例:第一条就失败(--maxfail=1 之后三条根本没执行),最后一条收集期就跳过。
SUITE_CASES = f'''
import pytest

from testplatform.cases import case


@case(id="{CASE_IDS[0]}", clause="GBT37408-5.2.1", tags=["执行"])
def test_one_fails_first():
    assert False, "第一条就失败:后面三条没被执行"


@case(id="{CASE_IDS[1]}", clause="GBT37408-5.2.2", tags=["执行"])
def test_two():
    pass


@case(id="{CASE_IDS[2]}", clause="GBT37408-5.2.3", tags=["执行"])
def test_three():
    pass


@case(id="{CASE_IDS[3]}", clause="GBT37408-5.2.4", tags=["执行"])
def test_four():
    pass


@case(id="{CASE_IDS[4]}", clause="GBT37408-5.2.5", tags=["执行"])
@pytest.mark.skip(reason="收集期就跳过:它在 skipped 里,不许再算成执行过")
def test_five_is_skipped():
    pass
'''


def _run_round(tmp_path: Path, *args: str):
    """把一份清单 + 五条用例在子进程里真跑一轮,交回(输出, 证据目录)。"""
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(MANIFEST, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_executed.py").write_text(SUITE_CASES, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command("-q", "--device-config", "devices.yaml", "test_executed.py", *args),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    (directory,) = (tmp_path / "evidence").iterdir()
    return (result.stdout + result.stderr).decode("utf-8", "replace"), directory


@pytest.fixture(scope="module")
def interrupted_round(tmp_path_factory):
    """`--maxfail=1`:第一条失败后停下,后三条被收集却没执行。"""
    return _run_round(tmp_path_factory.mktemp("interrupted"), "--maxfail=1")


@pytest.fixture(scope="module")
def completed_round(tmp_path_factory):
    """跑完一整轮:收集的每一条要么执行了、要么被跳过。"""
    return _run_round(tmp_path_factory.mktemp("completed"))


def test_a_round_that_stops_early_counts_only_the_cases_it_executed(interrupted_round):
    """中断的一轮:收集 5 条、执行 1 条。规模行说执行了 1 条,不是 4 条(收集减跳过)。

    这一轮必须按文件里的定义顺序跑,失败那条排第一:`--maxfail=1` 才真的在第一条就停。
    顺序一乱(子进程里 pytest-randomly 会被打开,pytest 不继承本仓库的 ini),
    失败的用例排到后面,executed 就变成 2~4 条 —— 所以 tests_support/nested_pytest.py 显式带
    `-p no:randomly`,下面也只断言「执行过的那一条是哪一条」,不断言它是 executed[0]。
    """
    output, directory = interrupted_round
    assert "1 failed" in output, output

    scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    assert len(scope["collected"]) == 5
    assert [entry["case"] for entry in scope["skipped"]] == [CASE_IDS[4]]
    executed_names = {entry.rsplit("::", 1)[-1] for entry in scope["executed"]}
    assert executed_names == {"test_one_fails_first"}, (
        f"只有失败那条被执行过;executed 里出现了 {sorted(executed_names)} —— "
        "多半是执行顺序被随机化了,--maxfail=1 没在第一条就停"
    )

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.ran["executed"] == 1
    assert "执行了 1 条" in text and "还没跑到 3 条" in text
    assert "执行了 4 条" not in text, "收集减跳过会把这个数说成 4 条"


def test_a_completed_round_counts_every_case_it_executed(completed_round):
    """跑完的一轮:执行 4 条、跳过 1 条,与 pytest 报告的 1 failed + 3 passed + 1 skipped 对得上。"""
    output, directory = completed_round
    assert "1 failed" in output and "3 passed" in output and "1 skipped" in output, output

    attribution = attribute(directory)
    text = "\n".join(render(attribution))

    assert attribution.ran["executed"] == 4
    assert "执行了 4 条" in text and "还没跑到 0 条" in text


def test_the_machine_readable_scale_agrees_with_the_printed_one(completed_round):
    """机器可读那侧只有一处「跑了多少条」:ran.executed,与终端规模行同一个数。"""
    _, directory = completed_round

    record = json.loads(write(directory, attribute(directory)).read_text(encoding="utf-8"))

    assert record["ran"]["executed"] == 4
    assert record["scope"]["executed"] == 4
    assert record["scope"]["not_reached"] == 0
    assert record["ran"]["cases"] == 0, "这一轮用例都不读数:证据里没有读数"
