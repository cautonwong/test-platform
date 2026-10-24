"""起停 tools/mock_modbus_meter.py 子进程的公共代码。

用例和并行实测脚本都要"起一个 mock、从启动行里读它实际分到的端口、干净停掉",所以放在这里,
两边都用同一个启停路径:停的时候关掉 stdin,让 --stop-on-stdin-close 走 shutdown()
(退出码 0,监听套接字由它关掉),并一定 wait 到进程真的没了 —— 否则下一次跑会撞端口。

文件名不以 test_ 开头,不被收集。
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_platform_paths = None  # 延迟到用的时候再 import:测试进程自己已经在 sys.path 里了
MOCK_SCRIPT = REPO_ROOT / "tools" / "mock_modbus_meter.py"

# 子进程与父进程说同一种编码:否则 Windows 上子进程按控制台编码写、父进程按 locale 解码,
# 一行中文启动输出就能把用例炸成 UnicodeDecodeError。
def _child_env() -> dict:
    """从站脚本要 import `testplatform.protocol`:把平台自己的代码层交给子进程。

    用 `paths.sys_path()` 组装(不在测试里另写一套)—— 票 11c 之后从站脚本不再靠
    "自身在源码树的层级"猜路径,源码布局的这份 PYTHONPATH 由调用方给。
    """
    from testplatform.config import paths as platform_paths

    return {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join(platform_paths.sys_path()),
    }


CHILD_ENV = _child_env()


@contextlib.contextmanager
def mock_process(*arguments: str):
    """起一个 mock 子进程。`--port 0` 让系统分配端口,这行启动输出里带着实际端口。

    yield (实际端口, 子进程, 启动那行输出)。
    """
    process = subprocess.Popen(
        [
            sys.executable,
            str(MOCK_SCRIPT),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--stop-on-stdin-close",
            *arguments,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=CHILD_ENV,
    )
    try:
        banner = process.stdout.readline()
        match = re.search(r"监听 (\S+):(\d+)", banner)
        if match is None:
            process.kill()
            raise AssertionError(f"mock 没起来: {banner!r} {process.stderr.read()!r}")
        yield int(match.group(2)), process, banner
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
