"""
通过真实 socket 连独立 mock 从站,把"接真实电表前最后一段"跑通。

整链用例(不加任何命令行参数就跑):自己起 mock 子进程、拿系统分配的端口现造一份清单,
于是「清单解析 → 装配 → 点表求值 → 判据 → 证据落盘」五步在同一条用例里一步不断,
平台侧走 real_transports.RealTcpTransport —— 证明的是真实链路上的收发,不是进程内模拟器。
端口曾经只写在出厂清单里,这条用例因此只能人工起 mock 才跑得起来。清单改成现造的以后
默认跑就是真跑,mock 起不来算失败而不是 skip —— 一端口的差别不再变成一条永久 skip 的用例。

出厂清单 `configs/devices_mock.yaml` 单独一条用例:它的端口写死(给人手工跑),
所以那条不连 mock,只证明这份清单自己没写坏 —— 能装配、型号/点表/判据引用自洽、
连接字段给得出端口。

下半部分(不依赖任何 fixture):自己起 mock 子进程验证故障注入 —— 每条用例用
`--port 0` 让系统分配端口,因此可以和"另一个人在跑的那个 mock"同时存在。
故障注入是 mock 相对进程内模拟器唯一不可替代的价值:
timeout 证明平台会超时报错而不是静默读到旧值,bad-crc 证明帧校验真的在生效,
slow 证明延迟是真的(真的等,真的过 socket);最后一条用"退出码 0"证明它是自己
shutdown() 停下的,而不是被 terminate 硬杀。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.config.loader import load_device_config
from testplatform.config.manifest import parse_manifest
from testplatform.protocol import modbus_rtu
from testplatform.protocol.crc import crc16_modbus
from testplatform.reporting.evidence import EvidenceSession
from testplatform.transport.real_transports import RealTcpTransport
from testplatform.transport.simulators import SimulatedModbusSlave

from tests_support.manifest_factory import manifest
from tests_support.mock_meter_process import CHILD_ENV, MOCK_SCRIPT, mock_process as _mock

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTERS = REPO_ROOT / "configs" / "mock_registers.yaml"
FACTORY_MANIFEST = REPO_ROOT / "configs" / "devices_mock.yaml"

# 和 configs/mock_registers.yaml 是同一份寄存器内容的两个视角:那边是从站内存,
# 这边是"点表把哪几个寄存器解成多少"。0x0001E240 × 0.01 = 1234.56 kWh,2300 × 0.1 = 230.0 V。
ENERGY = 1234.56
VOLTAGE = 230.0


# ===================== 端到端:真实 socket 上的一次完整读数 =====================

@case(id="M_00_00_01_000002_10", clause="DLT645-4.5.2", tags=["一致性", "通信"])
@pytest.mark.contract
def test_mock_meter_round_trip_over_a_real_socket(tmp_path):
    """整链五步:清单解析 → 装配 → 点表求值 → 判据结论 → 证据落盘,一步不断。

    清单是现造的,端口用 mock 实际分到的那个,所以这条用例不要求任何人先起 mock、
    也不要求任何命令行参数。用例里不出现寄存器地址,也不出现 CRC:地址在点表里、
    帧在 protocol/ 里 —— 和跑在进程内模拟器上的契约用例是同一份代码,只有链路不同。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            # 1 清单解析 + 2 装配:access/mode 决定真实 socket 传输,型号决定点表与判据集。
            assert root.mode_of("meter_mock") == "real"
            assert root.model_of("meter_mock").protocol == "modbus_rtu"
            assert root.reading_keys_of("meter_mock") == ("有功电量", "电压")
            transport = root.transports["meter_mock"]
            assert isinstance(transport, RealTcpTransport), (
                f"这条用例要证明的是真实 socket 链路,拿到的传输却是 {type(transport).__name__}"
            )

            meter = root.get("meter_mock")
            meter.connect()
            # 3 点表求值 + 4 判据结论
            reading = meter.read_all()
            assert reading.readings["有功电量"] == pytest.approx(ENERGY)
            assert reading.readings["电压"] == pytest.approx(VOLTAGE)
            assert reading.passed is True
        finally:
            root.teardown()

        # 5 证据落盘:帧是真实往返的 RTU 帧,读数那条带 mode,能自证跑在真实链路上。
        # 两个点地址相邻,这次读数只发一帧 —— 合并前这里是两帧、四条记录。
        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        assert [frame["direction"] for frame in frames] == ["send", "receive"]
        slave, function, address, count = modbus_rtu.parse_read_request(
            bytes.fromhex(frames[0]["hex"])
        )
        assert (function, address, count) == (modbus_rtu.READ_HOLDING_REGISTERS, 0, 3)
        assert modbus_rtu.parse_read_response(
            bytes.fromhex(frames[1]["hex"]), slave
        ) == bytes.fromhex("0001E240" "08FC")

        readings = [record for record in records if record["kind"] == "reading"]
        assert len(readings) == 1, f"一次完整读数只该落一条 reading,落了 {len(readings)} 条"
        assert readings[0]["mode"] == "real", "证据得自证这条结论跑在真实链路上"
        assert readings[0]["criteria"]["profile"] == "meter_220v"
        assert readings[0]["readings"]["有功电量"] == pytest.approx(ENERGY)
        assert readings[0]["evaluation"] == {"passed": True, "breaches": []}


# ===================== 出厂清单:不连 mock 也要证明它自己没写坏 =====================

def test_factory_mock_manifest_assembles_without_a_running_mock():
    """`configs/devices_mock.yaml` 的端口写死(给人手工跑),所以这里不连 mock。

    断言的是这份清单本身:每台设备都装配得出来、meter_01 走真实 socket + Modbus RTU、
    判据集引用的物理量都是点表里的读数键(否则那条判据永远不会被判到)、
    连接字段给得出端口。整链证明是上一条用例的事。
    """
    root = CompositionRoot.build(str(FACTORY_MANIFEST))
    manifest = parse_manifest(load_device_config(FACTORY_MANIFEST))
    assert sorted(root.drivers) == sorted(spec["id"] for spec in manifest.devices)

    model = root.model_of("meter_01")
    assert (model.protocol, root.mode_of("meter_01")) == ("modbus_rtu", "real")
    criteria = root.get("meter_01").device.criteria
    assert criteria.name == "meter_220v"
    assert {criterion.quantity for criterion in criteria.criteria} <= set(model.reading_keys)

    meter = next(spec for spec in manifest.devices if spec["id"] == "meter_01")
    assert isinstance(meter["connection"]["port"], int) and meter["connection"]["port"] > 0


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ===================== 写路径:0x06 / 0x10 走真实 socket =====================


def test_mock_write_single_register_over_a_real_socket(tmp_path):
    """0x06:值进 mock 的寄存器表,应答是请求帧原样回显,写后立刻读拿到新值。

    电压 length 2 → 一个寄存器 → 功能码 0x06,由点表决定,不由用例选。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            assert meter.write("电压", 231.5) == pytest.approx(231.5), "write 返回的必须是回读值"
            # 再发一帧读请求把全部点读回来:值真的落在从站内存里,不是只在应答里回显过。
            assert meter.read_all().readings["电压"] == pytest.approx(231.5)
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        # 写请求 / 写应答 / 回读请求 / 回读应答 / 这一轮读的全部点 / 读应答
        assert [frame["direction"] for frame in frames] == ["send", "receive"] * 3
        assert bytes.fromhex(frames[0]["hex"]).hex(" ") == "01 06 00 02 09 0b 6f 9d"
        assert frames[1]["hex"] == frames[0]["hex"], "0x06 的应答是请求帧原样回显"
        slave, function, address, count = modbus_rtu.parse_read_request(
            bytes.fromhex(frames[2]["hex"])
        )
        assert (function, address, count) == (modbus_rtu.READ_HOLDING_REGISTERS, 2, 1)
        assert modbus_rtu.parse_read_response(
            bytes.fromhex(frames[3]["hex"]), slave
        ) == bytes.fromhex("090b")
        # 寄存器层面再确认一次:0/1 号(有功电量)没被写坏,2 号换成了 0x090B。
        assert modbus_rtu.parse_read_response(
            bytes.fromhex(frames[5]["hex"]), slave
        ) == bytes.fromhex("0001e240090b")

        writes = [record for record in records if record["kind"] == "write"]
        assert len(writes) == 1, f"一次写只该落一条 write,落了 {len(writes)} 条"
        assert {key: writes[0][key] for key in ("key", "value", "verified", "read_back")} == {
            "key": "电压",
            "value": 231.5,
            "verified": True,
            "read_back": 231.5,
        }


def test_mock_write_multiple_registers_over_a_real_socket(tmp_path):
    """0x10:两个寄存器一次写进去,应答只回显起始地址 + 寄存器数(没有数据段)。

    有功电量 length 4 → 两个寄存器 → 功能码 0x10。500.0 kWh 就是 0x0000C350(倍率 0.01)。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            assert meter.write("有功电量", 500.0) == pytest.approx(500.0)
            assert meter.read("有功电量") == pytest.approx(500.0)
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        assert bytes.fromhex(frames[0]["hex"]).hex(" ") == "01 10 00 00 00 02 04 00 00 c3 50 a3 63"
        assert bytes.fromhex(frames[1]["hex"]).hex(" ") == "01 10 00 00 00 02 41 c8"
        # 回读那一帧读的是刚写的两个寄存器,拿到的是新值。
        slave, function, address, count = modbus_rtu.parse_read_request(
            bytes.fromhex(frames[2]["hex"])
        )
        assert (function, address, count) == (modbus_rtu.READ_HOLDING_REGISTERS, 0, 2)
        assert modbus_rtu.parse_read_response(
            bytes.fromhex(frames[3]["hex"]), slave
        ) == bytes.fromhex("0000c350")
        writes = [record for record in records if record["kind"] == "write"]
        assert [write["key"] for write in writes] == ["有功电量"]
        assert writes[0]["verified"] is True


def test_mock_answers_a_write_to_a_missing_register_with_illegal_data_address():
    """写一个寄存器表里没有的地址 → 异常应答 02(非法数据地址),不是断掉连接。

    从站收下了帧、明确说“这个地址我没有”:客户端拿到的是 5 字节的异常应答,
    而不是一个空等换来的超时 —— 两者归因不同。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        single = _ask(port, modbus_rtu.build_write_single_request(1, 99, 1), 5)
        # 0x10 里有一个地址不存在,整帧都不写(半截写下去比整帧拒掉难查得多)。
        multiple = _ask(port, _write_multiple_frame(1, 98, bytes(4)), 5)

    assert single.hex(" ") == "01 86 02 c3 a1"
    assert multiple.hex(" ") == "01 90 02 cd c1"
    assert single == modbus_rtu.build_exception_response(
        1, modbus_rtu.ILLEGAL_DATA_ADDRESS, modbus_rtu.WRITE_SINGLE_REGISTER
    )


def test_mock_answers_an_illegal_register_count_with_illegal_data_value():
    """0x10 的寄存器数不合法 → 异常应答 03(非法数据值)。

    个数非法与地址不存在是两句话:个数是“这次请求的形状不对”(拆小重发能过),
    地址是“这个设备没有这个寄存器”。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        empty = _ask(port, _write_multiple_frame(1, 0, b""), 5)
        too_many = _ask(
            port,
            _write_multiple_frame(
                1, 0, bytes(2 * modbus_rtu.WRITE_MULTIPLE_MAX_REGISTERS + 2)
            ),
            5,
        )

    assert empty.hex(" ") == "01 90 03 0c 01", "0 个寄存器的写要被拒,不是无声地写了个寂寞"
    assert too_many == modbus_rtu.build_exception_response(
        1, modbus_rtu.ILLEGAL_DATA_VALUE, modbus_rtu.WRITE_MULTIPLE_REGISTERS
    )


def test_mock_write_reject_fault_raises_a_slave_exception_not_a_timeout(tmp_path):
    """`--fail write-reject`:从站收下请求、明确回一句不干(异常码 03)。

    这和 --fail timeout 必须分得开:一个是设备在回答(换参数可以重试),一个是链路
    根本没回话(重试只会更慢)。平台侧因此抛 SlaveExceptionResponse,不是 TimeoutError。
    """
    with _mock("--registers", str(REGISTERS), "--fail", "write-reject") as (port, _p, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            with pytest.raises(modbus_rtu.SlaveExceptionResponse) as excinfo:
                meter.write("电压", 231.5)

            assert not isinstance(excinfo.value, TimeoutError)
            assert excinfo.value.code == modbus_rtu.ILLEGAL_DATA_VALUE
            assert meter.device.history == (), "写被拒不该被记成一条读数"
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        assert [frame["direction"] for frame in frames] == ["send", "receive"]
        assert "error" not in frames[1], "应答回来了,不是没回"
        assert bytes.fromhex(frames[1]["hex"]) == modbus_rtu.build_exception_response(
            1, modbus_rtu.ILLEGAL_DATA_VALUE, modbus_rtu.WRITE_SINGLE_REGISTER
        )
        assert [record for record in records if record["kind"] == "write"] == []


def test_mock_timeout_fault_still_times_out_on_the_write_path(tmp_path):
    """`--fail timeout` 在写路径上一样:请求收下、一个字不回,平台必须超时。

    写比读更不能被静默放过:一个“看着成功”的写会让后续所有结论都建在没写下去的值上。
    """
    with _mock("--registers", str(REGISTERS), "--fail", "timeout") as (port, _p, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                meter.write("电压", 231.5)
            elapsed = time.monotonic() - started

            assert elapsed < 5.0, f"2.0 秒传输超时,却等了 {elapsed:.1f} 秒"
            assert meter.device.history == (), "超时的那次写不该被记成一条读数"
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        assert bytes.fromhex(frames[0]["hex"]).hex(" ") == "01 06 00 02 09 0b 6f 9d"
        assert frames[1]["direction"] == "receive"
        assert frames[1]["error"] == "timeout"
        assert [record for record in records if record["kind"] == "write"] == []


# 差分的向量表:请求帧一律写成 hex 字面量,不用 `build_*_request` 生成 ——
# 两边共用同一个编码器时,编码上的差异永远测不出来(等于拿同一个函数验证自己)。
# 每项 = (做了什么, 请求帧, 应答帧应有字节数);顺序即状态顺序,写进去的值由后面的读读回来。
# 0x10 的个数上限(0x7B)与 0 走同一条判断,不另列一条 248 字节的向量。
DIFFERENTIAL_VECTORS = (
    ("0x03 读保持寄存器 0..2", "01 03 00 00 00 03 05 cb", 11),
    ("0x04 读输入寄存器 0..2", "01 04 00 00 00 03 b0 0b", 11),
    ("0x06 写寄存器 2 = 0x090B", "01 06 00 02 09 0b 6f 9d", 8),
    ("0x03 回读寄存器 2", "01 03 00 02 00 01 25 ca", 7),
    ("0x10 写寄存器 0..1 = 0x0000C350", "01 10 00 00 00 02 04 00 00 c3 50 a3 63", 8),
    ("0x03 回读寄存器 0..1", "01 03 00 00 00 02 c4 0b", 9),
    ("0x06 写不存在的寄存器 99", "01 06 00 63 00 01 b8 14", 5),
    ("0x10 里有一个寄存器不在表里", "01 10 00 62 00 02 04 00 00 00 00 74 5e", 5),
    ("0x10 寄存器数为 0", "01 10 00 00 00 00 00 09 50", 5),
)
# 上面第一条的 CRC 末字节 0x05 改成 0x06:帧长、地址、功能码、寄存器数都合法,只有校验错。
BAD_CRC_REQUEST = "01 03 00 00 00 03 05 06"


def test_mock_and_the_in_process_simulator_answer_the_same_bytes():
    """同一批请求帧先后递给两个从站,应答必须逐字节相同 —— 一个规范,两份实现受同一组向量检验。

    两边是两套实现(`simulators.py` / `tools/mock_modbus_meter.py`),各自断言“符合自己的期望”
    时,两份实现可以同时通过、同时互相矛盾(规模清单少声明一次寄存器就是这样)。所以这里不各自
    断言,而是比应答字节:0x03 读、0x04 读、0x06 写 + 回读、0x10 写 + 回读、写不存在的地址(异常码
    02)、寄存器数非法(异常码 03)、坏 CRC 都在表里。比“解析结果相同”不行,那会掩盖 CRC 与字节序差异。

    只起一个 mock 子进程,整批向量在同一个进程里跑完;两个从站的寄存器起点是同一份
    `configs/mock_registers.yaml`,否则写进去的值不同,比出来的是状态而不是行为。
    """
    registers = yaml.safe_load(REGISTERS.read_text(encoding="utf-8"))
    simulator = SimulatedModbusSlave(
        {int(address): int(value) for address, value in registers.items()}
    )
    with _mock("--registers", str(REGISTERS)) as (port, _process, _banner):
        for what, request_hex, length in DIFFERENTIAL_VECTORS:
            request = bytes.fromhex(request_hex)
            simulator.send(request)
            in_process = simulator.receive()
            over_socket = _ask(port, request, length)
            assert in_process == over_socket, (
                f"{what}: 两个从站的应答不一致 —— 进程内 {in_process.hex(' ')},"
                f"独立 mock {over_socket.hex(' ')},请求 {request_hex}"
            )

        # 坏 CRC:两边都不许给出应答帧。进程内从站当场抛错,独立从站断开这条连接。
        bad_crc = bytes.fromhex(BAD_CRC_REQUEST)
        simulator.send(bad_crc)
        with pytest.raises(ValueError, match="CRC 校验失败"):
            simulator.receive()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(bad_crc)
            assert connection.recv(64) == b"", "独立从站必须拒收坏 CRC 的请求,而不是照答一帧"


def _ask(port: int, request: bytes, length: int) -> bytes:
    """一条连接问一句就走:直接收发裸帧,不经过驱动层(这里验的就是从站的应答字节)。"""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        response = b""
        while len(response) < length:
            chunk = connection.recv(length - len(response))
            assert chunk, f"从站提前关了连接,只收到 {response.hex()}"
            response += chunk
    return response


def _write_multiple_frame(slave: int, address: int, registers: bytes) -> bytes:
    """手工拼 0x10 请求帧:protocol 层的 build 会挡掉非法个数,而这里要验的正是非法个数。"""
    body = (
        bytes([slave, modbus_rtu.WRITE_MULTIPLE_REGISTERS])
        + address.to_bytes(2, "big")
        + (len(registers) // 2).to_bytes(2, "big")
        + bytes([len(registers)])
        + registers
    )
    crc = crc16_modbus(body)
    return body + bytes([crc & 0xFF, crc >> 8])


# ===================== 故障注入:自己起 mock,证明失败是可预期的 =====================

def test_mock_timeout_fails_loudly_instead_of_returning_a_stale_value(tmp_path):
    """`--fail timeout`:mock 收下请求,一个字不回。

    平台必须在传输层超时并以 TimeoutError 收场,而不是读到一个陈旧的合法值 ——
    所以既断言抛的是超时,也用证据文件证明这一轮"发了一个请求、没收到应答"(证据里那条
    receive 带 error: timeout,与"回了但被拒"分得开),没有落盘读数:失败是可预期的,不是静默的。
    """
    with _mock("--registers", str(REGISTERS), "--fail", "timeout") as (port, _process, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                meter.read("电压")
            elapsed = time.monotonic() - started

            assert elapsed < 5.0, f"2.0 秒传输超时,却等了 {elapsed:.1f} 秒"
            assert meter.device.history == (), "超时的那次读不该被记成一条读数"
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [r for r in records if r["kind"] == "frame"]
        # 没收到应答也要落一条:否则证据里只剩一个 send,分不清"没回"与"回了但被拒"。
        assert [frame["direction"] for frame in frames] == ["send", "receive"]
        assert frames[1]["error"] == "timeout"
        assert frames[1]["timeout"] == pytest.approx(2.0)
        assert 1.9 <= frames[1]["waited"] < 5.0, f"超时 2.0 秒,记录里等了 {frames[1]['waited']} 秒"
        assert [r for r in records if r["kind"] == "reading"] == []


def test_mock_bad_crc_fault_is_rejected_by_the_frame_check(tmp_path):
    """`--fail bad-crc`:帧长、地址、功能码、数据都合法,只有 CRC 是错的。
    平台必须拒收 —— 带着错帧往下解码只会得到一个"看起来正常"的假读数。

    这种失败与超时在证据里必须分得开:应答确实回来了(有 hex、没有 error),
    而且记下来的正是那帧被校验拒掉的应答。
    """
    with _mock("--registers", str(REGISTERS), "--fail", "bad-crc") as (port, _process, _banner):
        session = EvidenceSession(tmp_path / "evidence")
        root = CompositionRoot.build(_write_manifest(tmp_path, port), evidence=session)
        try:
            meter = root.get("meter_mock")
            meter.connect()

            with pytest.raises(ValueError) as excinfo:
                meter.read("电压")

            assert "CRC 校验失败" in str(excinfo.value)
            assert meter.device.history == ()
        finally:
            root.teardown()

        records = _records(session.path_of("meter_mock"))
        frames = [record for record in records if record["kind"] == "frame"]
        assert [frame["direction"] for frame in frames] == ["send", "receive"]
        assert "error" not in frames[1], "应答回来了,不是没回"
        # 记下来的正是那帧被拒的应答:拿它重新解析,还是同一句 CRC 报错。
        with pytest.raises(ValueError, match="CRC 校验失败"):
            modbus_rtu.parse_read_response(bytes.fromhex(frames[1]["hex"]), 1)


def test_mock_slow_fault_delays_the_answer_but_the_read_still_lands(tmp_path):
    """`--fail slow --delay`:延迟是真的(真的等,真的过 socket),不是模拟器的瞬间返回。"""
    with _mock(
        "--registers", str(REGISTERS), "--fail", "slow", "--delay", "0.3"
    ) as (port, _process, _banner):
        root = CompositionRoot.build(_write_manifest(tmp_path, port))
        try:
            meter = root.get("meter_mock")
            meter.connect()

            started = time.monotonic()
            voltage = meter.read("电压")
            elapsed = time.monotonic() - started

            assert voltage == pytest.approx(VOLTAGE)
            # 两个点地址相邻 → 一次请求,只等一次 0.3 秒延迟(合并前是两个请求、约 0.6 秒)。
            # 下界不写到 0.3 是因为睡在 mock 那一端、测量在客户端这一端。
            assert 0.25 <= elapsed < 0.55, f"延迟 0.3 秒,却花了 {elapsed:.2f} 秒"
        finally:
            root.teardown()


def test_mock_bus_with_two_requesters_records_the_contention(tmp_path):
    """同一个 mock(= 一条物理总线)上同时来两个请求者,必须被记下来。

    ``--fail slow --delay 0.4`` 把"一帧还没答完"的窗口拉到 0.4 秒:第二条连接在这个窗口里
    连上来(交错会话)、又发了一帧(交错帧),两种抢占都该落盘。
    并行分组实测(tests/parallel_bus_check.py)就靠这份记录判"防住了没有"—— 它是拿
    "跑绿了"推不出来的那一半。
    """
    log = tmp_path / "contention.jsonl"
    request = modbus_rtu.build_read_request(1, 2, 1)
    with _mock(
        "--registers",
        str(REGISTERS),
        "--fail",
        "slow",
        "--delay",
        "0.4",
        "--contention-log",
        str(log),
    ) as (port, _process, _banner):
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        first.sendall(request)  # 这一帧要 0.4 秒才答完
        second = socket.create_connection(("127.0.0.1", port), timeout=5)
        second.sendall(request)  # 第一帧还没答完又来一帧
        try:
            # 两个请求都答了:抢占是两条连接真的同时在用这条总线,不是靠把谁弄失败换来的。
            assert first.recv(64) and second.recv(64)
        finally:
            first.close()
            second.close()

    kinds = [record["kind"] for record in _records(log)]
    assert "session-overlap" in kinds, f"第二条连接没被记下来: {kinds}"
    assert "frame-overlap" in kinds, f"交错的两帧没被记下来: {kinds}"


def test_mock_stops_cleanly_when_asked():
    """进程能启能停:停止信号进来后走 shutdown() —— 关监听、退出码 0,而不是被硬杀。"""
    with _mock("--registers", str(REGISTERS)) as (_port, process, _banner):
        pass
    assert process.returncode == 0, "mock 不是被 shutdown() 干净停下的"


def test_second_mock_on_a_taken_port_fails_instead_of_answering_for_the_first(tmp_path):
    """同一端口上已经有 mock 时,第二个必须非零退出 —— 不允许它也打印"监听成功"。

    这个 bug 会把故障注入用例变成假绿:陈旧的 mock 静默替 --fail timeout 的那个应答,
    平台读到一个合法的陈旧值,于是"设备不回话时平台会失败"这条结论被 mock 自己伪造掉。
    所以这里必须用进程验证(起、判、收尸),而不是只测一个函数返回值。
    """
    with _mock("--registers", str(REGISTERS)) as (port, first, _banner):
        second = subprocess.run(
            [
                sys.executable,
                str(MOCK_SCRIPT),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),  # 故意撞端口
                "--registers",
                str(REGISTERS),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=CHILD_ENV,
            timeout=30,
        )

        assert second.returncode != 0, f"第二个实例居然起来了: {second.stdout!r}"
        assert "监听" not in second.stdout, f"第二个实例打印了监听成功: {second.stdout!r}"
        assert "端口已被占用" in second.stderr, f"报错不清楚原因: {second.stderr!r}"
        assert "--port" in second.stderr and "pid" in second.stderr, "报错没给出路"

        # 第一个不受影响:还在监听,也还能正常应答。
        assert first.poll() is None, "第二个实例把第一个弄死了"
        root = CompositionRoot.build(_write_manifest(tmp_path, port))
        try:
            meter = root.get("meter_mock")
            meter.connect()
            assert meter.read("电压") == pytest.approx(VOLTAGE)
        finally:
            root.teardown()


def _listening_pids(port: int) -> set[int] | None:
    """当前监听这个端口的进程 pid 集合;探测工具都不可用时返回 None。

    为什么不直接和 Popen.pid 比:有些环境里 python.exe 是个 launcher,
    Popen.pid 拿到的是 launcher 的 pid,而 mock 打印的是真正监听端口那个进程的
    os.getpid()。拿 Popen.pid 比在那类机器上必然红,即使产品代码完全正确。
    """
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        pids: set[int] = set()
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
                continue
            if parts[1].rsplit(":", 1)[-1] == str(port):
                pids.add(int(parts[4]))
        return pids

    probes = (
        (["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], "lsof"),
        (["ss", "-lptnH", f"sport = :{port}"], "ss"),
    )
    for command, kind in probes:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        if kind == "lsof":
            return {int(token) for token in completed.stdout.split() if token.isdigit()}
        return {int(match) for match in re.findall(r"pid=(\d+)", completed.stdout)}
    return None


def test_mock_banner_carries_its_pid():
    """启动行里的 pid,必须正是当前占着那个监听端口的进程。

    特意不拿 Popen.pid 比:某些环境里 python.exe 是 launcher,Popen.pid 是 launcher 的,
    而 mock 打印的是真正监听端口那个进程的 os.getpid() —— 拿 Popen.pid 比在那类机器上
    必然红,即使产品代码正确。这里验的是操作上真正需要的事:端口被占时,
    启动行里的 pid 能拿去查"到底是谁占着"。
    """
    with _mock("--registers", str(REGISTERS)) as (port, _process, banner):
        match = re.search(r"pid (\d+)", banner)
        assert match is not None, f"启动行里没有 pid: {banner!r}"
        printed = int(match.group(1))
        listening = _listening_pids(port)
        if listening is None:
            pytest.skip("这台机器上 netstat/lsof/ss 都不可用,无法确认 pid 是否真的占着端口")
        assert printed in listening, (
            f"启动行里的 pid {printed} 不是占着端口 {port} 的进程: {sorted(listening)}"
        )


# ===================== 起停 mock 与临时清单 =====================




def _write_manifest(tmp_path: Path, port: int) -> str:
    """一份只含这台 mock 表的清单:modbus_rtu + access tcp,走真实 socket。

    两个点都声明 writable:读路径用例不碰它,写路径用例靠同一份清单验写→回读。
    """

    def _real_socket(manifest_dict):
        manifest_dict["devices"][0].update(
            access="tcp", mode="real", connection={"host": "127.0.0.1", "port": port}
        )
        for point in manifest_dict["models"]["dtsd1352_modbus"]["point_table"].values():
            point["writable"] = True

    path = tmp_path / "devices_mock.yaml"
    path.write_text(
        yaml.safe_dump(manifest(meters=["meter_mock"], mutate=_real_socket), allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)
