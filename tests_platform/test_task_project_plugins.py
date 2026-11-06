"""任务加载前必须导入项目根目录的 conftest.py。

新协议/新驱动在 conftest 里注册;任务加载在父进程解析清单,子 pytest 还没起来。
不先导入 conftest,项目里的新规约会停在"未知规约",task 根本跑不到用例。
"""
from pathlib import Path

import pytest

from testplatform.execution.task import TaskFileError, load_task

DEVICES = """\
criteria_profiles:
  demo_220v:
    - {{quantity: 电压, gt: 0}}
models:
  demo_model:
    type: meter
    protocol: {protocol}
    criteria: demo_220v
    capabilities: [电压]
    point_table:
      电压: {{address: 0, length: 2, encoding: uint, unit: V}}
devices:
  - id: dev_01
    model: demo_model
    access: tcp
    mode: simulator
    connection: {{simulated_response: "231"}}
"""

TASK = """\
id: demo
product: demo-product
launch:
  - ${PYTHON}
  - product.py
  - --port
  - ${PORT}
devices: configs/devices.yaml
select: tag:样板
"""


def _project(tmp_path: Path, protocol: str, conftest: str) -> Path:
    project = tmp_path / "project"
    (project / "configs").mkdir(parents=True)
    (project / "tasks").mkdir()
    (project / "configs" / "devices.yaml").write_text(
        DEVICES.format(protocol=protocol), encoding="utf-8"
    )
    (project / "tasks" / "demo.yaml").write_text(TASK, encoding="utf-8")
    (project / "product.py").write_text("pass\n", encoding="utf-8")
    (project / "conftest.py").write_text(conftest, encoding="utf-8")
    return project


def test_a_protocol_registered_in_the_project_conftest_is_known_at_task_load(tmp_path):
    project = _project(
        tmp_path,
        "demo_task_proto_a",
        "from testplatform.config.protocols import register_protocol\n"
        'register_protocol("demo_task_proto_a", access={"tcp"})\n',
    )

    task = load_task(project / "tasks" / "demo.yaml", root=project)

    assert task.manifest.models["demo_model"].protocol == "demo_task_proto_a"
    assert task.devices == project / "configs" / "devices.yaml"


def test_a_broken_project_conftest_fails_the_load_instead_of_a_half_manifest(tmp_path):
    project = _project(
        tmp_path,
        "demo_task_proto_b",
        "raise RuntimeError('插件加载失败')\n",
    )

    with pytest.raises(TaskFileError, match="conftest.py 导入失败"):
        load_task(project / "tasks" / "demo.yaml", root=project)


def test_a_project_conftest_runs_once_for_multiple_tasks(tmp_path):
    """conftest 里可能用 @register_device;重复执行就是重复注册。"""
    project = _project(
        tmp_path,
        "demo_task_proto_c",
        "from testplatform.config.protocols import register_protocol\n"
        "from testplatform.drivers.registry import register_device\n"
        'register_protocol("demo_task_proto_c", access={"tcp"})\n'
        '@register_device("demo_direct", "demo_task_proto_c")\n'
        "def _build(*args, **kwargs):\n"
        "    raise NotImplementedError\n",
    )
    (project / "tasks" / "second.yaml").write_text(
        TASK.replace("id: demo", "id: demo2"), encoding="utf-8"
    )

    first = load_task(project / "tasks" / "demo.yaml", root=project)
    second = load_task(project / "tasks" / "second.yaml", root=project)

    assert first.id == "demo"
    assert second.id == "demo2"
