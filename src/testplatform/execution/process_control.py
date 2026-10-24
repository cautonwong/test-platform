"""
任务跑用例这一段,平台与 pytest 进程之间的控制面。两边不共享内存,所以协议就是证据目录里的文件。

    stop.request      平台叫停本轮:{"reason": "product_crashed" | "task_timeout" | "task_cancelled"}
                      pytest 在每一条用例开跑前读它。读到就把这条与后面的一条报告都不打
                      —— 它们留在 run-scope 的 collected 里、不进 executed / skipped,
                      平台据此把它们记成「未执行 N 条」,而不是"通过"或"跳过"。
    restart.request   用例要求冷启动产品:{"reason": "..."}(见 restart_product)
    restart.ack       平台写完重启回执:{"at": ..., "pid": ..., "port": ...}

目录由平台经环境变量 TESTPLATFORM_CONTROL_DIR 告诉 pytest 进程;任务超时经
TESTPLATFORM_TASK_TIMEOUT 传进来(秒,从第一条用例开跑算起)。没设这两个变量时所有钩子
都不做事:平台自己的套件与这套控制面无关。

任务超时的检查点是用例边界(每条用例开跑前),正在跑的那条由 pytest-timeout 兜底 ——
在用例中间硬杀 pytest 会把 run-scope 一起弄丢,而"未执行几条"只有它说得清。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

CONTROL_DIR_ENV = "TESTPLATFORM_CONTROL_DIR"
TASK_TIMEOUT_ENV = "TESTPLATFORM_TASK_TIMEOUT"
STOP_REQUEST_FILE = "stop.request"
RESTART_REQUEST_FILE = "restart.request"
RESTART_ACK_FILE = "restart.ack"

# 平台侧的原因字面量:与证据里 unexecuted.reason 是同一套拼法,04 按它分类。
PRODUCT_CRASHED = "product_crashed"
TASK_TIMEOUT = "task_timeout"
TASK_CANCELLED = "task_cancelled"

_STARTED_KEY = pytest.StashKey[float]()


def control_directory() -> Path | None:
    """这一轮的控制面目录(证据目录);不在任务执行器下时为 None。"""
    written = os.environ.get(CONTROL_DIR_ENV)
    return Path(written) if written else None


def request_stop(directory: str | Path, reason: str) -> None:
    """平台叫停本轮。写文件,不杀进程:让 pytest 停在用例边界,把已跑过的结论留着。"""
    (Path(directory) / STOP_REQUEST_FILE).write_text(
        json.dumps({"reason": reason}, ensure_ascii=False), encoding="utf-8"
    )


def stop_reason(directory: str | Path) -> str | None:
    """本轮被叫停的原因;没被叫停(或还没有这个文件)时为 None。

    目录是显式参数,不读环境变量:平台那一侧(任务执行器)不在这个变量的环境里,
    要读的是自己写下的那份请求。
    """
    try:
        return json.loads((Path(directory) / STOP_REQUEST_FILE).read_text(encoding="utf-8")).get(
            "reason"
        )
    except (OSError, ValueError):
        return None


def restart_product(*, reason: str = "用例要求冷启动", timeout: float = 60.0) -> dict:
    """要求平台把产品重启一遍(冷启动),返回回执 {at, pid, port}。

    产品进程归任务执行器所有,用例不自己起进程:写一份请求,等平台把新进程等就绪了回执。
    重启点由平台写进证据(process_events 的 product_restart),用例这边拿到的只有"已经重启好了"。
    """
    directory = control_directory()
    if directory is None:
        raise RuntimeError(
            "当前不在任务执行器下(没有 TESTPLATFORM_CONTROL_DIR):"
            "产品进程归任务执行器所有,用例不能自己重启它"
        )
    ack_path = directory / RESTART_ACK_FILE
    ack_path.unlink(missing_ok=True)
    (directory / RESTART_REQUEST_FILE).write_text(
        json.dumps({"reason": reason}, ensure_ascii=False), encoding="utf-8"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ack_path.is_file():
            ack = json.loads(ack_path.read_text(encoding="utf-8"))
            ack_path.unlink(missing_ok=True)
            return ack
        time.sleep(0.05)
    raise TimeoutError(f"产品重启 {timeout:g}s 内没有完成(平台没回执)")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """每一条用例开跑前:本轮被叫停了吗?超时了吗?

    被叫停时返回 True(这个钩子 firstresult,True = 已处理):这一条既不跑也不产报告,
    于是它在 run-scope 里只剩 collected 那一条 —— 平台据此数出「未执行 N 条」。
    """
    directory = control_directory()
    limit = _task_timeout()
    if limit is not None and directory is not None:
        started = item.config.stash.get(_STARTED_KEY, None)
        if started is None:
            item.config.stash[_STARTED_KEY] = time.monotonic()
        elif time.monotonic() - started > limit:
            request_stop(directory, TASK_TIMEOUT)
    reason = None if directory is None else stop_reason(directory)
    if reason is None:
        return None
    item.session.shouldstop = f"平台已终止本轮({reason}),剩余用例未执行"
    return True


def _task_timeout() -> float | None:
    written = os.environ.get(TASK_TIMEOUT_ENV)
    return float(written) if written else None
