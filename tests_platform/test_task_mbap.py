"""
MBAP(Modbus TCP)端到端:产品、平台、模拟从站三方说的是同一种帧。

三层各一条,都断言外部可观察的东西:
    1. 任务执行器下跑一整轮:产品经 MBAP 读模拟从站 → 平台读同一台 → 读数一致
    2. `meter.read_all()` 经 MBAP 驱动的完整路径(清单 → 装配 → 点表 → 判据),不依赖任务
    3. 帧格式错配(RTU 客户端连 MBAP 从站):报错可辨认,不静默解出值

再加两条锁:帧格式开关不传时还是 RTU(既有 `minimal.yaml` 一字不变)、任务文件自己说清帧格式
(跑它不需要设任何环境变量 —— 一份配置的语义不靠进程环境)。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.protocol import modbus_tcp
from testplatform.execution.task import TASK_RECORD_FILE
from tests_support.manifest_factory import manifest, write
from tests_support.mock_meter_process import mock_process

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTERS = REPO_ROOT / "configs" / "mock_registers.yaml"
MBAP_TASK = REPO_ROOT / "configs" / "tasks" / "mbap.yaml"
MINIMAL_TASK = REPO_ROOT / "configs" / "tasks" / "minimal.yaml"

# 和 configs/mock_registers.yaml 同一份寄存器内容的两个视角:0x0001E240 × 0.01 = 1234.56 kWh,
# 2300 × 0.1 = 230.0 V。
ENERGY = 1234.56
VOLTAGE = 230.0
MBAP = "modbus_tcp"
RTU = "modbus_rtu"


# ===================== 端到端:产品与平台读同一台 MBAP 从站 =====================

@case(id="T_00_00_02_000000_01", clause="free: 平台:MBAP 端到端", tags=["任务"])
def test_platform_and_product_read_the_same_meter_over_modbus_tcp(tmp_path, evidence_session):
    """任务执行器把端口写进证据目录的 task.json,平台照它去连同一台从站 —— 两边读数必须一致。"""
    record = _task_record(evidence_session)
    ports = record["slave_ports"]
    if not ports:
        pytest.skip("这一轮任务没有拉起模拟从站(launch 里没有 ${SLAVE_PORT:<设备id>})")
    root = _build(tmp_path, ports, record["slave_addresses"], MBAP, evidence_session)
    try:
        measured = {}
        for device_id in ports:
            # 帧格式是这个型号的规约说了算:装配出来的链路必须真的是 MBAP,不是"读通了就算"。
            assert root.model_of(device_id).protocol == MBAP
            meter = root.get(device_id)
            meter.connect()
            reading = meter.read_all()
            assert reading.passed is True, f"{device_id} 的读数没通过判据: {reading.evaluation}"
            measured[device_id] = reading.readings
    finally:
        root.teardown()

    product = _ask_product(int(record["product_port"]))
    for device_id, seen in product["meters"].items():
        assert seen["port"] == ports[device_id], (
            f"产品读的不是任务分配给 {device_id} 的那条端口:"
            f"产品 {seen['port']} vs 任务 {ports[device_id]}"
        )
        assert seen["energy_kwh"] == pytest.approx(measured[device_id]["有功电量"]), (
            f"产品经 MBAP 读到 {seen['energy_kwh']} kWh,"
            f"平台读同一台从站是 {measured[device_id]['有功电量']} kWh"
        )
        assert seen["voltage_v"] == pytest.approx(measured[device_id]["电压"])


# ========================= meter.read_all() 经 MBAP 全路径 =========================

@case(id="T_00_00_02_000000_02", clause="free: 平台:MBAP 端到端", tags=["一致性", "读数"])
def test_meter_read_all_over_modbus_tcp_reads_every_point(tmp_path):
    """02c 欠的那条:MBAP 驱动的完整路径此前没有用例走过(只有帧编解码的单测)。

    走的还是那个 `(meter, modbus_tcp)` 工厂 + `_MbapLink` + 通用点表驱动:清单里只把型号的规约
    换成 modbus_tcp、模拟从站产出 MBAP 帧,读法其余部分一处不改。
    """
    root = CompositionRoot.build(write(tmp_path, _simulated_mbap_manifest()))
    try:
        assert root.model_of("meter_01").protocol == MBAP
        meter = root.get("meter_01")
        meter.connect()
        reading = meter.read_all()
    finally:
        root.teardown()

    assert reading.passed is True, f"读数没通过判据: {reading.evaluation}"
    assert reading.readings["有功电量"] == pytest.approx(ENERGY)
    assert reading.readings["电压"] == pytest.approx(VOLTAGE)


# ============================ 帧格式错配:报错,不是假读数 ============================

@case(id="T_00_00_02_000000_03", clause="free: 平台:MBAP 端到端", tags=["任务"])
def test_rtu_client_against_an_mbap_slave_fails_loudly(tmp_path):
    """RTU 帧喂给 MBAP 从站:平台报"对端断开",不是解出一个看着正常的读数。

    从站那头先开的口(功能码与长度字段凑不出一帧 MBAP 请求),现场看日志就知道是清单里规约写错了 ——
    而不是去猜"这台表怎么不理人"。
    """
    with mock_process("--registers", str(REGISTERS), "--frame", MBAP) as (port, process, _banner):
        root = CompositionRoot.build(write(tmp_path, _real_manifest({"meter_01": port}, RTU)))
        meter = root.get("meter_01")
        meter.connect()
        try:
            with pytest.raises(ConnectionError) as caught:
                meter.read_all()
        finally:
            root.teardown()
    assert "关闭连接" in str(caught.value), f"报错得说清是对端断开: {caught.value}"
    assert "不是 MBAP 帧" in process.stderr.read(), "从站没点名帧格式错配"


# ====================== 帧格式开关:不缺省乱走,也不靠环境变量 ======================

@case(id="T_00_00_02_000000_04", clause="free: 平台:MBAP 端到端", tags=["任务"])
def test_shipped_minimal_task_still_speaks_rtu_by_default(tmp_path):
    """不传 --frame 就还是 RTU:既有任务原样跑绿,从站 argv 里也是 modbus_rtu。

    这条锁的是"加开关没有改掉既有行为":如果默认值漂成 MBAP,假产品读不到东西、
    就绪等待先红,任务退出码不是 0。
    """
    record = _run_shipped_task(MINIMAL_TASK, tmp_path / "evidence")
    assert _slave_frame(record) == RTU


@case(id="T_00_00_02_000000_05", clause="free: 平台:MBAP 端到端", tags=["任务"])
def test_shipped_mbap_task_runs_without_any_environment_variable(tmp_path):
    """MBAP 任务单看就能跑:帧格式由清单里型号的 protocol 推导,任务文件和环境变量都不掺和。"""
    record = _run_shipped_task(MBAP_TASK, tmp_path / "evidence")
    assert _slave_frame(record) == MBAP
    assert record["launch"][record["launch"].index("--frame") + 1] == MBAP


# ================================ MBAP 从站的写路径 ================================

@case(id="T_00_00_02_000000_06", clause="free: 平台:MBAP 端到端", tags=["任务"])
def test_mbap_slave_answers_writes_and_the_value_sticks():
    """0x06 / 0x10 两种写:应答必须是这一帧的回声,写进去的值随后能读回。

    从站行为只有 RTU 那一份,MBAP 帧是翻译过来的 —— 翻译最容易在写路径上出错(写应答是回显,
    不是数据),所以两个写功能码各走一遍。
    """
    with mock_process("--registers", str(REGISTERS), "--frame", MBAP) as (port, _process, _banner):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            single = modbus_tcp.build_write_single_request(1, 1, 2, 2310)
            assert _reply(connection, single) == single, "写单个寄存器的应答是请求帧原样回显"
            multiple = modbus_tcp.build_write_multiple_request(2, 1, 0, bytes.fromhex("0002e240"))
            assert _reply(connection, multiple) == modbus_tcp.build_write_multiple_response(2, 1, 0, 2)
            payload = modbus_tcp.parse_read_response(
                _reply(connection, modbus_tcp.build_read_request(3, 1, 0, 3)),
                1,
                expected_transaction=3,
            )
    # 写进去的就在同一张寄存器表里:0x0002E240 × 0.01 = 1889.92 kWh,2310 × 0.1 = 231.0 V。
    assert int.from_bytes(payload[0:4], "big") * 0.01 == pytest.approx(1889.92)
    assert int.from_bytes(payload[4:6], "big") * 0.1 == pytest.approx(231.0)


# ---------------------------------------------------------------- 工具 ----

def _reply(connection: socket.socket, request: bytes) -> bytes:
    """发一帧 MBAP 请求,收满一帧 MBAP 应答(长度字段说整帧多长)。"""
    connection.sendall(request)
    received = b""
    while (needed := modbus_tcp.response_length(received)) is None or len(received) < needed:
        chunk = connection.recv(256)
        assert chunk, f"从站没回应答: {request.hex()}"
        received += chunk
    return received

def _task_record(evidence) -> dict:
    path = evidence.directory / TASK_RECORD_FILE
    if not path.exists():
        pytest.skip(
            "这条用例只在任务执行器下跑(证据目录里没有 task.json;"
            "python tools/run_task.py configs/tasks/mbap.yaml)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _real_manifest(ports: dict[str, int], protocol: str, addresses=None) -> dict:
    """现造一份清单:每台表按给的端口走真实 socket,规约由 protocol 给(帧格式跟着它走)。"""
    addresses = addresses or {}

    def _real(built):
        built["models"]["dtsd1352_modbus"]["protocol"] = protocol
        for spec in built["devices"]:
            spec.update(
                access="tcp",
                mode="real",
                bus_address=addresses.get(spec["id"], 1),
                connection={"host": "127.0.0.1", "port": ports[spec["id"]]},
            )

    return manifest(meters=list(ports), mutate=_real)


def _build(tmp_path, ports: dict[str, int], addresses: dict[str, int], protocol: str, evidence=None):
    return CompositionRoot.build(
        write(tmp_path, _real_manifest(ports, protocol, addresses)), evidence=evidence
    )


def _simulated_mbap_manifest() -> dict:
    """进程内模拟从站产出 MBAP 帧:型号规约选帧格式,和清单里 connection.simulated_frames 同一套取值。"""

    def _mbap(built):
        built["models"]["dtsd1352_modbus"]["protocol"] = MBAP
        built["devices"][0].update(
            access="tcp",
            connection={
                "simulated_registers": {0: 0x0001, 1: 0xE240, 2: 2300},
                "simulated_frames": MBAP,
            },
        )

    return manifest(meters=["meter_01"], mutate=_mbap)


def _run_shipped_task(task_path: Path, evidence: Path) -> dict:
    """走 `tools/run_task.py` 这个入口(不是直接调 run_task):验收的是"一条命令"。"""
    completed = subprocess.run(
        [sys.executable, "tools/run_task.py", str(task_path), "--evidence-dir", str(evidence)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"{task_path.name} 没跑通,退出码 {completed.returncode}\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )
    return json.loads((evidence / TASK_RECORD_FILE).read_text(encoding="utf-8"))


def _slave_frame(record: dict) -> str:
    """任务实际给模拟从站的那条 --frame:证据里记着的是真跑过的命令行。"""
    argv = record["slaves"][0]["argv"]
    return argv[argv.index("--frame") + 1]


def _ask_product(port: int) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        line = connection.makefile("rb").readline()
    if not line:
        raise AssertionError(f"产品在 {port} 上没回任何东西")
    return json.loads(line)
