"""
票 12:端口被抢时不直接失败,而是换一个端口重来(≤3 次)并留痕;三次都不行 = **平台错误**。

"被抢"这件事在测试里没法稳定复现(它正是我们要消灭的那个窗口),所以注入一个**端口分配器**
(`port_reserver`),把它变成可复现的一次事件 —— 端口是外部资源,由使用者分配,这比
monkeypatch 私有函数诚实。分配器返回 `(占住的套接字 | None, 端口)`:None = 这个端口此刻不可用。

四条守卫:
    端口不可用 → 换一个重来并留痕
    三次都不可用 → 平台错误(不是产品不合格),消息点明端口
    产品自己崩(端口空着)→ 照旧报它自己的死因,不被重试掩盖
    产品自报 bind 失败(端口在别人眼里是空的)→ 按端口问题重试
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest
import yaml

from testplatform.execution.task import TASK_RECORD_FILE, TaskRunError, load_task, run_task

REPO = Path(__file__).resolve().parents[1]
FAKE_PRODUCT = REPO / "tests_support" / "fake_product.py"
DEVICES = REPO / "configs" / "devices.yaml"
# 一条不依赖模拟从站端口的用例:重试这件事与"产品能不能读表"无关。
SELECT = "case:M_00_00_01_000002_01"
# 夹具自己的备用端口段(平台缺省段 20000-31999 之外、系统临时端口段 49152+ 之下)。
SPARE_PORT_BASE = 19000

# 第一次启动就自报 bind 失败(用一个标记文件记"已经失败过一次"),第二次正常监听。
# 这样"端口其实是空的、产品却说 bind 失败"这个场景可以稳定复现 —— 这时只有 stderr 那个
# 信号能救,`_port_unavailable` 会说"可用"。
BIND_FAILING_ONCE_PRODUCT = """
import socket
import sys
from pathlib import Path

port = int(sys.argv[2])
marker = Path(__file__).with_name("bind_failed_once")
if not marker.exists():
    marker.write_text("1", encoding="utf-8")
    print(
        "PermissionError: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(8)
while True:
    connection, _ = listener.accept()
    connection.close()
"""


def _spare_port():
    """夹具自己的备用端口:平台缺省段(20000-31999)之外的一小片,探针带 SO_REUSEADDR。

    不 bind(0) 再 close:那样交给产品的是"刚放开的系统临时端口",而产品自己带
    SO_REUSEADDR —— 在 Windows 上它去 bind 刚放开的端口会拿到 10013。那是夹具自己造的假故障,
    会被读成平台缺陷(实测:光板探针 5/60,带 SO_REUSEADDR 0/60)。
    起点按 pid 错开,同一台机器上两个 run 不会总从同一个端口开始。
    """
    base = SPARE_PORT_BASE + os.getpid() % 64
    for offset in range(32):
        candidate = base + offset
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", candidate))
        except OSError:
            probe.close()
            continue
        return probe, candidate
    raise AssertionError(f"夹具备用端口段 {base} 起 32 个都被占了")


def _plain_bind_error(port: int) -> int:
    """普通(不带 SO_REUSEADDR)bind 这个端口的错误码;成功返回 0。"""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        return getattr(exc, "winerror", exc.errno) or -1
    finally:
        probe.close()
    return 0


def _first_unavailable_then_spare(port: int):
    """第一次说这个端口不可用;之后给夹具自己的备用端口。

    不写死"就两次":机器忙时备用端口也可能被占了 —— 那正是被测代码该继续重试的情况,
    测试不该因为"只准备了两个端口"而红。断言只要求"这次不可用被记录下来了"。
    """
    handed: list[int] = []

    def reserver():
        if not handed:
            handed.append(port)
            return None, port
        return _spare_port()

    return reserver


def _task_file(tmp_path: Path, *, extra: tuple[str, ...] = (), launch: list[str] | None = None) -> Path:
    path = tmp_path / "task.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "port-probe",
                # 验的是平台的端口重试,不是设备验收:设备级 0/0 按冒烟放行。
                "kind": "smoke",
                "product": "fake-edgec",
                "launch": launch
                or ["${PYTHON}", str(FAKE_PRODUCT), "--port", "${PORT}", *extra],
                "devices": str(DEVICES),
                "select": SELECT,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _record(evidence: Path) -> dict:
    return json.loads((evidence / TASK_RECORD_FILE).read_text(encoding="utf-8"))


def _events(record: dict, kind: str) -> list[dict]:
    return [event for event in record["process_events"] if event["kind"] == kind]


# --------------------------------------------------- 端口被抢 → 重试 ----

def test_an_unavailable_port_is_retried_and_the_retry_is_in_evidence(tmp_path):
    """分配器说"这个端口用不了":任务不该失败,换一个端口起来,证据里留下这次重试。

    这个"用不了"是**真的**:夹具把端口 bind 住不 close(真实场景:别的进程在用)。
    不是 bind(0) 再 close —— 那样只在放开那一瞬不复存在,而且产品(带 SO_REUSEADDR)
    去 bind 刚放开的端口会拿到 10013:夹具自己制造 10013,把夹具缺陷伪装成平台缺陷。
    """
    evidence = tmp_path / "evidence"
    stolen = 65_000
    holder = socket.socket()
    holder.bind(("127.0.0.1", stolen))
    try:
        # 真占用(与"刚放开的残留"区别开):普通 bind 必须失败。
        # 产品侧带 SO_REUSEADDR,同一个占位者对它回 10013 而不是 10048(Windows 语义),
        # 所以"真占用"只能由这里这种无关方声明的绑定方式来证明。
        refused = _plain_bind_error(stolen)
        assert refused, f"夹具没真的占住 {stolen}"
        if sys.platform == "win32":
            assert refused == 10048, f"普通 bind 应该是 10048(address already in use),拿到 {refused}"

        exit_code = run_task(
            load_task(_task_file(tmp_path)),
            evidence_dir=evidence,
            port_reserver=_first_unavailable_then_spare(stolen),
        )

        assert exit_code == 0, "端口不可用是平台侧的问题,不该让整轮失败"
        record = _record(evidence)
        assert record["error"] is None
        assert record["product_port"] != stolen, "最终用的是换过的端口"
        retries = _events(record, "port_retry")
        assert retries, "重试必须真的发生,并且要留痕(否则这条用例退化成「验能跑」)"
        assert retries[0]["reason"] == "端口他人不可用", retries[0]
        assert retries[0]["port"] == stolen
        assert retries[0]["attempt"] == 2
        assert retries[0]["at"], "重试记录要有时间戳"
    finally:
        holder.close()


def test_three_unavailable_ports_are_a_platform_error_naming_the_ports(tmp_path):
    """三个端口都不可用:结论是平台错误(不是产品不合格),消息点明是哪些端口。"""
    evidence = tmp_path / "evidence"
    stolen = 65_001

    with pytest.raises(TaskRunError) as error:
        run_task(
            load_task(_task_file(tmp_path)),
            evidence_dir=evidence,
            port_reserver=lambda: (None, stolen),
        )

    message = str(error.value)
    assert "平台错误" in message, message
    assert str(stolen) in message, f"消息要点明被占的端口:{message}"
    assert "不是产品不合格" in message, message

    record = _record(evidence)
    assert str(stolen) in (record["error"] or "")
    # 两次重试都留痕(第三次没有再"换"),而且它们不是产品死因
    assert [event["attempt"] for event in _events(record, "port_retry")] == [2, 3]
    assert _events(record, "product_exit") == [], "端口用不了不是产品的死因"


def test_a_crashing_product_is_not_retried_into_a_platform_error(tmp_path):
    """守卫:产品自己崩了(端口是空的)必须照旧报产品的事,不能被"重试"糊成平台错误。"""
    evidence = tmp_path / "evidence"

    with pytest.raises(TaskRunError) as error:
        run_task(load_task(_task_file(tmp_path, extra=("--crash",))), evidence_dir=evidence)

    assert "退出码 3" in str(error.value), str(error.value)
    assert "平台错误" not in str(error.value)
    record = _record(evidence)
    assert _events(record, "port_retry") == [], "产品崩不是端口问题,不该重试"


def test_a_product_reporting_a_bind_failure_is_retried_even_if_the_port_looks_free(tmp_path):
    """实测过的一种真假红:端口对自己人能 bind、对产品回 10013。

    这时"端口现在不可用"是假的,只能信产品自己报的 bind 失败。这一条把那个信号钉住:
    产品第一次自报 bind 失败(而端口其实是空的),换一个端口就能起来 ——
    只认"端口不可用"的实现会在这里红。

    不注入 port_reserver:这条叫 "looks free",端口就真的得是空的(走平台自己的端口段)。
    注入一个 bind(0) 再 close 的分配器,产品(带 SO_REUSEADDR)会拿到 10013 ——
    夹具自己制造 10013,把夹具缺陷伪装成平台缺陷。
    """
    product = tmp_path / "bind_fail_once.py"
    product.write_text(BIND_FAILING_ONCE_PRODUCT, encoding="utf-8")
    task = _task_file(tmp_path, launch=["${PYTHON}", str(product), "--port", "${PORT}"])

    exit_code = run_task(load_task(task), evidence_dir=tmp_path / "ev")

    assert exit_code == 0, "产品自报 bind 失败只该换端口重来,不该把整轮判死"
    record = _record(tmp_path / "ev")
    retries = _events(record, "port_retry")
    assert retries, "重试必须真的发生,并且要留痕(否则这条用例退化成「验能跑」)"
    assert retries[0]["reason"] == "产品 stderr 自报 bind 失败", retries[0]
    # 收尾会记一条 platform_killed(平台自己停的产品,不算死因);被放弃的那次尝试不记。
    assert [event["how"] for event in _events(record, "product_exit")] == ["platform_killed"]
