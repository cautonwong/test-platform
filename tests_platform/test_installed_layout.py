"""
装成包以后的形态(票 09):项目目录里只有用例、清单、任务文件,平台资源由包提供。

三种情况各有验收:
1. 项目目录不带任何平台资源也能跑完一个任务 —— 每样资源的来源写进 task.json 的 `layout`,
   否则"装完能用"和"根里四样齐"在证据里长得一模一样;
2. 包里少一样资源 → 加载任务时一次报清(缺哪样、包内路径、怎么补),不是 FileNotFoundError;
3. 项目目录里没有用例 → 报"没有用例",**不回落到包里找**(用例不随包,这是设计)。

真任务的形状沿用 tests_platform/test_task_root.py:假产品 + 模拟从站 + 一条只验通路的探针用例。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from testplatform.config import paths
from testplatform.config.paths import COMMIT_ENV, ROOT_ENV, PlatformRootError
from testplatform.execution.task import TASK_RECORD_FILE, TaskRunError, load_task, run_task

REPO = Path(__file__).resolve().parents[1]

PROBE_PRODUCT = '''\
import argparse
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
arguments = parser.parse_args()

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", arguments.port))
listener.listen(8)
print(f"probe product listening on {arguments.port}", flush=True)
while True:
    connection, _ = listener.accept()
    connection.close()
'''

PROBE_CASE = '''\
import json
import socket


def test_product_answers_on_the_allocated_port(evidence_session):
    """探针:产品真的在监听平台分配到的端口。它不挂 @case —— 临时项目里的用例没有编号。"""
    from testplatform.execution.task import TASK_RECORD_FILE

    record = json.loads(
        (evidence_session.directory / TASK_RECORD_FILE).read_text(encoding="utf-8")
    )
    with socket.create_connection(("127.0.0.1", record["product_port"]), timeout=5):
        pass
'''

DEVICES = """\
criteria_profiles: {}
models:
  probe_meter:
    type: meter
    protocol: vendor_ascii
    capabilities: [电压]
    point_table:
      电压: {address: 1, length: 7, encoding: ascii, unit: V}
devices: []
"""

TASK = """\
id: probe-installed
# 验的是"装成包以后项目目录里只有用例也能跑",不是设备验收:设备级 0/0 按冒烟放行。
kind: smoke
product: probe
launch:
  - ${PYTHON}
  - product.py
  - --port
  - ${PORT}
devices: devices_probe.yaml
select: case:probe_tests
"""


def _project(base: Path, *, cases: bool = True) -> Path:
    """一个**不带**任何平台资源的项目目录:没有 pyproject.toml / src / tools / configs。"""
    project = base / "project"
    project.mkdir(parents=True)
    (project / "product.py").write_text(PROBE_PRODUCT, encoding="utf-8")
    (project / "devices_probe.yaml").write_text(DEVICES, encoding="utf-8")
    (project / "task.yaml").write_text(TASK, encoding="utf-8")
    if cases:
        (project / "probe_tests").mkdir()
        (project / "probe_tests" / "test_probe.py").write_text(PROBE_CASE, encoding="utf-8")
    return project


# --------------------------------------- 项目目录不带资源也能跑完一个任务 ----

def test_a_project_directory_without_platform_resources_runs_a_task(tmp_path, monkeypatch):
    """资源的两种来源在证据里必须分得开:四样来自 package,用例来自 project。"""
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv(ROOT_ENV, raising=False)
    monkeypatch.delenv(COMMIT_ENV, raising=False)

    exit_code = run_task(load_task(project / "task.yaml"), evidence_dir=tmp_path / "evidence")

    assert exit_code == 0
    record = json.loads((tmp_path / "evidence" / TASK_RECORD_FILE).read_text(encoding="utf-8"))
    assert record["layout"] == {
        "pytest_ini": "package",
        "slave_script": "package",
        "registers": "package",
        "clauses": "package",
        "sample_product": "package",
        "cases": "project",
    }, record["layout"]
    # 项目目录没有 .git:commit 是显式的 unknown;platform_version 同口径,不许 None
    assert record["env"]["commit"] == "unknown"
    assert record["env"]["platform_version"], record["env"]


def test_the_project_directory_wins_when_it_has_the_resource(tmp_path, monkeypatch):
    """项目目录自己带 pyproject.toml 时用项目的那份 —— 来源变成 project,不是静默混用。"""
    project = _project(tmp_path)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.delenv(ROOT_ENV, raising=False)

    layout = paths.layout()

    assert layout.pytest_ini == project / "pyproject.toml"
    assert layout.sources()["pytest_ini"] == "project"
    assert layout.slave_script == paths.PACKAGE_DATA / "tools" / "mock_modbus_meter.py"
    assert layout.sources()["slave_script"] == "package"


def test_a_resource_in_neither_place_is_unknown_not_package(tmp_path, monkeypatch):
    """两边都没有:来源是 `unknown` —— 装坏了不许冒充 package(取值只有 package/project/unknown)。"""
    monkeypatch.setattr(paths, "PACKAGE_DATA", tmp_path / "no_such_package_data")
    project = _project(tmp_path, cases=False)

    sources = paths.PlatformLayout(project).sources()

    assert sources["slave_script"] == paths.FROM_UNKNOWN == "unknown"
    assert sources["cases"] == paths.FROM_PROJECT, "用例的来源固定是 project:它不随包走"
    assert set(sources) == {
        "slave_script",
        "registers",
        "clauses",
        "pytest_ini",
        "sample_product",
        "cases",
    }


# ------------------------------------------- 包里少一样:加载期一次报清 ----

def test_a_missing_package_resource_is_reported_once_with_the_package_path(tmp_path, monkeypatch):
    """删掉包内任意一样 → 加载任务时就报:缺哪样、包内路径在哪、怎么补。"""
    broken = tmp_path / "package" / "_data"
    shutil.copytree(paths.PACKAGE_DATA, broken)
    (broken / "tools" / "mock_modbus_meter.py").unlink()
    monkeypatch.setattr(paths, "PACKAGE_DATA", broken)
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv(ROOT_ENV, raising=False)

    with pytest.raises(PlatformRootError) as error:
        load_task(project / "task.yaml")

    message = str(error.value)
    assert "tools/mock_modbus_meter.py" in message, message
    assert str(broken) in message, f"没说是包里的哪一处:{message}"
    assert "--root" in message and ROOT_ENV in message, f"没告诉怎么补:{message}"


# ---------------------------------------- 项目目录没有用例:不回落到包里 ----

def test_a_project_directory_without_cases_says_so_instead_of_falling_back(tmp_path, monkeypatch):
    """用例不随包:项目目录里没有就是没有,不许拿包里的用例凑数,也不许只说"测试无效"。"""
    project = _project(tmp_path, cases=False)
    monkeypatch.chdir(project)
    monkeypatch.delenv(ROOT_ENV, raising=False)

    with pytest.raises(TaskRunError) as error:
        run_task(load_task(project / "task.yaml"), evidence_dir=tmp_path / "evidence")

    message = str(error.value)
    assert "没有用例" in message and str(project) in message, message


def _invalid_digest(evidence: Path) -> str:
    """失败时把"为什么判无效"留在断言消息里:结论、无效用例、一条现场记录。

    `invalid` 是"判定被环境吞掉"(平台说不清结论),和 `unknown` 同族 —— 红了却要翻目录才看得懂,
    等于下一次还是查不动。
    """
    def _read(name: str):
        try:
            return json.loads((evidence / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    summary = _read("summary.json") or {}
    attribution = _read("attribution.json") or {}
    invalid = attribution.get("invalid_tests") or []
    digest = [
        f"verdict={summary.get('verdict')} reason={summary.get('reason')} "
        f"columns={summary.get('columns')}",
        f"invalid_tests={json.dumps(invalid, ensure_ascii=False)[:800]}",
    ]
    for name in sorted(item.name for item in evidence.glob("*.jsonl")):
        lines = [ln for ln in (evidence / name).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            digest.append(f"{name}: {lines[-1][:800]}")
            break
    else:
        digest.append("现场记录:本轮没有 .jsonl 证据(没有读数/帧可看)")
    return chr(10).join(digest)


# ------------------------------------- 票 09d:显式根也逐样回落(用例除外)----

def test_an_explicit_root_that_only_ships_the_slave_script_runs_the_task(tmp_path):
    """混排:项目目录只带 `tools/mock_modbus_meter.py`,其余几样来自包 —— 显式根照样跑通,来源如实记。"""
    project = _project(tmp_path)
    (project / "tools").mkdir()
    shutil.copy(REPO / "tools" / "mock_modbus_meter.py", project / "tools")

    evidence = tmp_path / "evidence"
    exit_code = run_task(load_task(project / "task.yaml", root=project), evidence_dir=evidence)

    assert exit_code == 0, _invalid_digest(evidence)
    record = json.loads((tmp_path / "evidence" / TASK_RECORD_FILE).read_text(encoding="utf-8"))
    assert record["layout"] == {
        "slave_script": "project",
        "pytest_ini": "package",
        "registers": "package",
        "clauses": "package",
        "sample_product": "package",
        "cases": "project",
    }, record["layout"]


def test_an_explicit_root_without_cases_does_not_borrow_the_package_ones(tmp_path, monkeypatch):
    """只有 configs、没有 src/tools/pyproject 的目录(CI 的形状):报到"没有用例",不是"缺 pyproject.toml"。

    包里恰好有可收集的用例时也不许拿它凑数 —— 用例不参与回落。
    """
    package = tmp_path / "package" / "_data"
    shutil.copytree(paths.PACKAGE_DATA, package)
    (package / "decoy_tests").mkdir()
    (package / "decoy_tests" / "test_decoy.py").write_text(
        "def test_decoy():\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "PACKAGE_DATA", package)
    project = tmp_path / "proj09"
    (project / "configs").mkdir(parents=True)
    (project / "product.py").write_text(PROBE_PRODUCT, encoding="utf-8")
    (project / "devices_probe.yaml").write_text(DEVICES, encoding="utf-8")
    (project / "task.yaml").write_text(TASK, encoding="utf-8")
    monkeypatch.delenv(ROOT_ENV, raising=False)

    with pytest.raises(TaskRunError) as error:
        run_task(
            load_task(project / "task.yaml", root=project), evidence_dir=tmp_path / "evidence"
        )

    message = str(error.value)
    assert "没有用例" in message and str(project.resolve()) in message, message
    assert "pyproject.toml" not in message, f"缺项不该再是 pyproject.toml(它由包提供):{message}"
