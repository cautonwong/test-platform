"""任务执行器的失败收尾:重启失败不能把产品 / pytest 留成孤儿。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from testplatform.execution.process_control import RESTART_REQUEST_FILE
from testplatform.execution.task import runtime


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.pid = 123

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.returncode = -15


def test_a_failed_restart_settles_the_new_product_and_clears_the_slot(tmp_path, monkeypatch):
    old, new = FakeProcess(), FakeProcess()
    products = [old]
    record = {"launch": ["product"], "product_port": 1234}
    settled = []
    monkeypatch.setattr(
        runtime,
        "_settle_product",
        lambda record, product, **kwargs: settled.append(product),
    )
    monkeypatch.setattr(runtime, "_start_product", lambda *args, **kwargs: new)

    def fail_ready(*args, **kwargs):
        raise runtime.TaskRunError("new product did not listen")

    monkeypatch.setattr(runtime, "_wait_ready", fail_ready)
    (tmp_path / RESTART_REQUEST_FILE).write_text("{}", encoding="utf-8")

    with pytest.raises(runtime.TaskRunError, match="did not listen"):
        runtime._restart_product(products, record, tmp_path, 1.0, set(), tmp_path)

    assert settled == [old, new]
    assert products[0] is None


def test_a_restart_exception_terminates_the_pytest_child(tmp_path, monkeypatch):
    class FakePytest(FakeProcess):
        pass

    pytest_child = FakePytest()
    products = [FakeProcess()]
    task = SimpleNamespace(
        platform=SimpleNamespace(root=tmp_path, pytest_ini=tmp_path / "pytest.ini"),
        devices=tmp_path / "devices.yaml",
        select="tag:test",
    )
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: pytest_child)
    monkeypatch.setattr(runtime, "_child_env", lambda platform: {})
    monkeypatch.setattr(runtime, "_plugin_arguments", lambda: [])
    monkeypatch.setattr(
        runtime,
        "_restart_product",
        lambda *args, **kwargs: (_ for _ in ()).throw(runtime.TaskRunError("restart failed")),
    )
    (tmp_path / RESTART_REQUEST_FILE).write_text("{}", encoding="utf-8")

    with pytest.raises(runtime.TaskRunError, match="restart failed"):
        runtime._run_pytest(
            task,
            tmp_path,
            products=products,
            record={},
            ready_timeout=1.0,
            task_timeout=None,
            recorded=set(),
        )

    assert pytest_child.terminated
