"""
起一个一次性 mosquitto broker，拿到它**实际监听的端口**，用完干净停掉。

## 为什么要有这个

北向 E2E 的被测对象不是"一台能被连的设备"，而是「设备往云端推」这条通路。
这条通路的两端是：**broker**（平台侧基础设施）与**设备**（DUT）。
broker 起不来时，结论是**测试无效**，不是设备不合格 —— 和 ADR-0009 里
"上游没就绪"是同一类归因。

所以 broker 的启停必须集中一处，且必须满足三条：

1. **端口不写死**：CI 并行跑多个任务时固定端口会撞。
2. **一定 wait 到进程真的没了**：否则下一次跑撞端口，失败现象是"设备连不上"。
3. **缺 mosquitto 时明确跳过**，不是静默失败也不是判设备不合格。

文件名不以 `test_` 开头，不被 pytest 收集。
"""
from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import time
from pathlib import Path

__all__ = ["BrokerUnavailable", "MosquittoBroker", "mosquitto_available"]


class BrokerUnavailable(RuntimeError):
    """本机没有可用的 mosquitto。**这不是设备不合格**，是台架缺件。"""


def mosquitto_available() -> bool:
    return shutil.which("mosquitto") is not None


def _free_port() -> int:
    """向内核要一个空闲端口。

    拿到的端口在"关闭 socket"到"mosquitto 绑定"之间理论上可能被别人抢走，
    所以 `start()` 里带重试——这不是理论洁癖，并行跑时真的会撞。
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int, timeout: float = 0.2) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return False
        return True


class MosquittoBroker:
    """一个前台 mosquitto 子进程。

    用法::

        with MosquittoBroker.start() as broker:
            transport = RealMqttTransport("127.0.0.1", broker.port)
    """

    def __init__(self, process: subprocess.Popen, port: int) -> None:
        self._process = process
        self.port = port

    # ---- 启停 ----

    @classmethod
    def start(cls, *, connect_timeout: float = 5.0, attempts: int = 5) -> "MosquittoBroker":
        if not mosquitto_available():
            raise BrokerUnavailable(
                "找不到 mosquitto：北向 E2E 需要本地 broker。"
                "装它（apt install mosquitto）或跳过北向用例——"
                "**不要**把它读成设备不合格。"
            )

        last_error = ""
        for _ in range(attempts):
            port = _free_port()
            process = subprocess.Popen(
                ["mosquitto", "-p", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if cls._wait_ready(process, port, connect_timeout):
                return cls(process, port)
            # 起不来（多半是端口被抢）：收掉再换一个
            last_error = cls._drain_stderr(process)
            cls._terminate(process)

        raise BrokerUnavailable(f"mosquitto 连续 {attempts} 次起不来：{last_error or '未知原因'}")

    @staticmethod
    def _wait_ready(process: subprocess.Popen, port: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False  # 进程已退出：端口被占之类
            if _port_open(port):
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> str:
        if process.poll() is None or process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", "replace").strip()[:500]
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def stop(self) -> None:
        self._terminate(self._process)
        if self._process.stderr is not None:
            self._process.stderr.close()

    # ---- 上下文 ----

    def __enter__(self) -> "MosquittoBroker":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def log_path(self) -> Path | None:
        """mosquitto 输出没有落盘（只有 stderr 管道），保留接口给将来的排障。"""
        return None
