"""
四个脚手架(票 09b):**生成即可跑**。

每个子命令生成一个项目,然后立刻用 `testplatform-run-task` 跑它 —— exit 0 才算过。
"生成了四个文件"不算验收:脚手架的意义是新用户第一条命令就能看到一份报告。

另有两条:
    * 生成的 Python 必须能编译(`compileall`,不跑到就无法"以后改坏")
    * **消费者视角**:临时目录里自己写 driver + 用例(不经过 scaffold、不碰仓库的
      `tests_support/`)→ 跑通。平台自带示例跑通证明不了 PYTHONPATH 真把项目目录接上了。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from testplatform import scaffold

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _run(project: Path, task: Path) -> subprocess.CompletedProcess:
    """按用户的方式跑一个任务:进程入口 + 项目目录(--root)。"""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "testplatform.execution.cli",
            str(task),
            "--root",
            str(project),
            "--evidence-dir",
            str(project / "reports"),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(SRC), str(project)]),
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=180,
    )


def _summary(project: Path) -> dict:
    return json.loads((project / "reports" / "summary.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", scaffold.KINDS)
def test_a_scaffolded_project_runs_immediately(tmp_path, kind):
    """生成 → 立刻跑 → exit 0,且报告说"合格"。四个子命令各一条。"""
    project = scaffold.scaffold(kind, tmp_path / kind)
    task = project / "tasks" / f"{kind}_sample.yaml"

    completed = _run(project, task)

    assert completed.returncode == 0, f"{kind} 生成的词项目跑不通:\n{completed.stdout[-2500:]}\n{completed.stderr[-1500:]}"
    summary = _summary(project)
    assert summary["verdict"] == "clean", summary["note"]
    assert summary["kind"] == "smoke"


@pytest.mark.parametrize("kind", scaffold.KINDS)
def test_scaffolded_python_compiles(tmp_path, kind):
    """生成的东西必须能编译 —— 骨架是给人改的起点,起点就得是合法 Python。"""
    project = scaffold.scaffold(kind, tmp_path / kind)

    completed = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_generated_files_say_they_are_python(tmp_path):
    """`npx` 只是外壳,写的是 Python —— 这句要落在生成文件里,不然新用户会以为有 DSL。"""
    for kind in scaffold.KINDS:
        project = scaffold.scaffold(kind, tmp_path / kind)
        generated = [
            path
            for path in project.rglob("*.py")
            if path.name != "product.py" and "conftest" not in path.name
        ]
        assert generated, kind
        for path in generated:
            first = path.read_text(encoding="utf-8").splitlines()[0]
            assert "Python" in first, f"{kind}: {path.name} 头部没写它是 Python: {first!r}"


# ------------------------------------------------- 消费者视角(不经过 scaffold) ----

CONSUMER_DRIVER = '''
"""用户自己写的驱动:注册 (消费者类型, vendor_ascii) → 工厂。"""
from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import register_device
from testplatform.drivers.vendor_ascii import vendor_ascii_reader
from testplatform.transport.interfaces import ByteStreamTransport


@register_device("consumer_meter", "vendor_ascii")
def build_consumer_driver(device_id, spec, model: Model, transport: ByteStreamTransport,
                          criteria: CriteriaProfile) -> PointTableDriver:
    return PointTableDriver(
        Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria),
        model,
        transport,
        vendor_ascii_reader(transport, b"READ\\n"),
    )
'''

CONSUMER_CASE = '''
from testplatform.cases import case


@case(id="T_00_00_91_000001_01", clause="{clause_id}", tags=["消费者"])
def test_consumer_device_reads(composition_root):
    device = composition_root.get("consumer_01")
    device.connect()
    reading = device.read_all()
    assert reading.readings
    assert device.device.latest_reading.passed is True
'''

CONSUMER_DEVICES = """
criteria_profiles:
  consumer_220v:
    - {quantity: 电压, gt: 0}
models:
  consumer_model:
    type: consumer_meter
    protocol: vendor_ascii
    criteria: consumer_220v
    capabilities: [电压]
    point_table:
      电压: {address: 0, length: 7, encoding: ascii, unit: V}
devices:
  - id: consumer_01
    model: consumer_model
    access: tcp
    mode: simulator
    name: "消费者项目里的模拟设备"
    connection:
      simulated_response: "230.0"
"""

CONSUMER_TASK = """
id: consumer_sample
kind: smoke
product: consumer-product
launch:
  - ${PYTHON}
  - product.py
  - --port
  - ${PORT}
devices: configs/devices.yaml
select: case:T_00_00_91_000001_01
"""


def test_a_consumer_project_with_its_own_driver_runs(tmp_path):
    """临时目录里(只有 configs/ / tests/ / tasks/ + 自己写的 driver)跑通一条任务。

    这条证明的是 `paths.sys_path()` 把**项目目录**接上了子进程:平台自带示例跑通不算。
    消费者代码不依赖仓库里的 `tests_support/`(那东西不随包分发)。
    """
    project = tmp_path / "consumer"
    (project / "configs").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tasks").mkdir()
    (project / "consumer_driver.py").write_text(CONSUMER_DRIVER, encoding="utf-8")
    (project / "conftest.py").write_text("import consumer_driver  # noqa: F401\n", encoding="utf-8")
    (project / "tests" / "test_consumer.py").write_text(
        CONSUMER_CASE.replace("{clause_id}", "C-1"), encoding="utf-8"
    )
    (project / "configs" / "devices.yaml").write_text(CONSUMER_DEVICES, encoding="utf-8")
    (project / "configs" / "clauses.yaml").write_text(
        "clauses:\n  C-1: {standard: 消费者, number: '', summary: 消费者项目自己的条款}\n",
        encoding="utf-8",
    )
    (project / "tasks" / "consumer_sample.yaml").write_text(CONSUMER_TASK, encoding="utf-8")
    (project / "product.py").write_text(scaffold.PRODUCT, encoding="utf-8")
    (project / "pyproject.toml").write_text(scaffold.PYPROJECT, encoding="utf-8")

    completed = _run(project, project / "tasks" / "consumer_sample.yaml")

    assert completed.returncode == 0, completed.stdout[-2500:] + completed.stderr[-1500:]
    assert _summary(project)["verdict"] == "clean"
