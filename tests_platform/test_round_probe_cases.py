"""
死因分类的探针用例:只在任务执行器下跑,别的场合跳过。

它们本身不断言产品,只做两件事:让出时间(好让平台看见产品死了)、要求平台重启产品。
选它们的是 `tests_platform/test_task_runner.py` 里那几条真进程用例 —— 平台的分类逻辑不靠这些用例,
但它们是唯一能在真进程上造出"跑一半产品没了""用例要求冷启动"的东西。

触发产品崩溃走 `--crash-on-file`:任务文件把路径写进命令行,这条用例把文件写出来。
比"睡 N 秒然后崩"可靠 —— 收集用例要多久不受这里控制。
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from testplatform.cases import case
from testplatform.execution.process_control import restart_product
from testplatform.execution.task import TASK_RECORD_FILE

# 触发崩溃后的等待:平台每 0.1 秒看一次产品死没死,这段时间要够它看见并叫停本轮。
PROBE_SLEEP = 1.2


@case(id="T_00_00_03_000000_01", clause="free: 平台:死因分类", tags=["任务"])
def test_slow_case_leaves_the_platform_time_to_see_the_product_die(evidence_session):
    record = _task_record(evidence_session)
    trigger = _crash_trigger(record)
    if trigger is not None:
        trigger.write_text("crash\n", encoding="utf-8")
    time.sleep(PROBE_SLEEP)


@case(id="T_00_00_03_000000_02", clause="free: 平台:死因分类", tags=["任务"])
def test_case_restarts_the_product_for_a_cold_boot(evidence_session):
    record = _task_record(evidence_session)
    ack = restart_product(reason="冷启动")
    assert ack["port"] == record["product_port"]
    with socket.create_connection(("127.0.0.1", record["product_port"]), timeout=5) as connection:
        served = json.loads(connection.makefile("rb").readline())
    assert served["product"] == "fake-edgec", "重启后的产品没在服务:冷启动等于没做"


@case(id="T_00_00_03_000000_03", clause="free: 平台:死因分类", tags=["任务"])
def test_quick_case_that_only_needs_the_product_alive(evidence_session):
    _task_record(evidence_session)


def _crash_trigger(record: dict) -> Path | None:
    """任务文件把 --crash-on-file 写进命令行:这条用例照它写出触发文件。"""
    launch = record.get("launch") or []
    if "--crash-on-file" not in launch:
        return None
    return Path(launch[launch.index("--crash-on-file") + 1])


def _task_record(evidence_session) -> dict:
    path = evidence_session.directory / TASK_RECORD_FILE
    if not path.exists():
        pytest.skip(
            "这条用例只在任务执行器下跑(证据目录里没有 task.json;python tools/run_task.py <任务文件>)"
        )
    return json.loads(path.read_text(encoding="utf-8"))
