"""
票 01b 的正验收:一个**不在源码树里**的目录当平台根,任务照样跑通。

这里造的是最小可跑根(不是整仓拷贝):`pyproject.toml` + `src/` + 模拟从站脚本 +
寄存器表/条款清单 + 一份薄设备清单 + 一条探针用例。跑通的判据是任务退出码 0、
证据落在根下面、`env.commit` 在没有 `.git` 的情况下是 `unknown`(不是 None)。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from testplatform.config.paths import COMMIT_ENV, ROOT_ENV
from testplatform.execution.task import TASK_RECORD_FILE, load_task, run_task

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

from testplatform.execution.task import TASK_RECORD_FILE


def test_product_answers_on_the_allocated_port(evidence_session):
    """这条用例只证明"执行器在别的根上也找得到自己那几样东西":产品真的在监听分配到的端口。

    它不挂 @case:这是临时根里的探针,不是平台用例 —— 没有编号就不该占一条条款,
    选中它按节点 id 前缀(--select case:probe_tests)。
    """
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
id: probe-root
# 验的是平台根可指定,不是设备验收:设备级 0/0 按冒烟放行。
kind: smoke
product: probe
launch:
  - ${PYTHON}
  - product.py
  - --port
  - ${PORT}
devices: configs/devices_probe.yaml
select: case:probe_tests
"""


def _minimal_root(base: Path) -> Path:
    """一个不在源码树里的平台根:只放执行器真正需要的那几样。"""
    root = base / "platform"
    (root / "src").mkdir(parents=True)
    shutil.copytree(
        REPO / "src" / "testplatform",
        root / "src" / "testplatform",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'pythonpath = ["src"]\n'
        'testpaths = ["probe_tests"]\n'
        'addopts = "-p no:randomly"\n'
        "timeout = 60\n"
        'timeout_method = "thread"\n'
        "markers = []\n",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    shutil.copy(REPO / "tools" / "mock_modbus_meter.py", root / "tools")
    (root / "configs").mkdir()
    shutil.copy(REPO / "configs" / "mock_registers.yaml", root / "configs")
    shutil.copy(REPO / "configs" / "clauses.yaml", root / "configs")
    (root / "configs" / "devices_probe.yaml").write_text(DEVICES, encoding="utf-8")
    (root / "probe_tests").mkdir()
    (root / "probe_tests" / "test_probe.py").write_text(PROBE_CASE, encoding="utf-8")
    (root / "product.py").write_text(PROBE_PRODUCT, encoding="utf-8")
    (root / "task.yaml").write_text(TASK, encoding="utf-8")
    return root


# --------------------------------------------- 临时根:任务真能跑通 ----

def test_a_task_runs_with_an_explicit_root_outside_the_source_tree(tmp_path):
    """显式 --root 指向临时根:退出码 0,证据落在那个根下,commit 是 unknown(那里没有 .git)。"""
    root = _minimal_root(tmp_path)

    exit_code = run_task(load_task(root / "task.yaml", root=root), evidence_dir=root / "evidence")

    assert exit_code == 0
    record = json.loads((root / "evidence" / TASK_RECORD_FILE).read_text(encoding="utf-8"))
    assert record["pytest_exit_code"] == 0
    assert record["env"]["commit"] == "unknown", (
        "没有 .git 的根里 commit 必须是显式的 unknown,不是 None / 空串"
    )
    # 证据里点名了用的是哪个根:事后要能指认这一轮跑在什么布局上
    assert Path(record["devices"]).is_relative_to(root.resolve())


def test_the_root_environment_variable_also_works_and_commit_can_be_supplied(
    tmp_path, monkeypatch
):
    """TESTPLATFORM_ROOT 指根(不用参数);TESTPLATFORM_COMMIT 给出显式 commit —— 两个环境变量都在子进程里生效。"""
    root = _minimal_root(tmp_path)
    monkeypatch.setenv(ROOT_ENV, str(root))
    monkeypatch.setenv(COMMIT_ENV, "deadbee")

    exit_code = run_task(load_task(root / "task.yaml"), evidence_dir=root / "evidence")

    assert exit_code == 0
    record = json.loads((root / "evidence" / TASK_RECORD_FILE).read_text(encoding="utf-8"))
    assert record["env"]["commit"] == "deadbee"

