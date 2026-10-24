"""
独立的 Modbus 从站进程,用 TCP 承载 RTU 帧(缺省)或 MBAP 帧(--frame modbus_tcp)。

为什么不是 transport/simulators.py 里的 SimulatedModbusSlave:那个是进程内的假实现,
直接返回字节,跳过了 socket 收发、帧边界、超时、重连 —— 也就是"接真实电表前最后一段"
里唯一还没被证明的部分。这个进程真的监听端口、真的按字节收发,平台侧走
real_transports.RealTcpTransport,除了不接真表,链路上没有别的省略。

功能码按请求帧里的功能码分派:03/04 读寄存器、06 写单个寄存器、10 写多个寄存器。
写是把值真的写进 --registers 那张表(所以写后立刻读能读到新值),应答按 Modbus 规定回显。
地址不在表里回异常码 02(非法数据地址),个数/值不合法回 03(非法数据值) ——
从站收下了帧并明确拒绝,而不是断掉连接让客户端去等一个超时。

帧的拼装、解析与 CRC 校验全部复用 protocol/modbus_rtu.py,这里不另写一套;
收发只用标准库 socket / threading。寄存器表从 --registers 读(yaml 用平台已有的
pyyaml,json 用标准库),从站地址从 --slave 读。

    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml
    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml --fail timeout
    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml --fail bad-crc
    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml --fail slow --delay 2.0
    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml --fail write-reject

--fail 才是 mock 相对进程内模拟器不可替代的部分:只有它能造出"对方收下请求但不回答"
这种状态,用来证明平台会超时报错,而不是静默读到一个陈旧的合法值。

--frame 选这个从站说哪种帧:modbus_rtu(缺省,串口服务器那种"RTU 帧走 TCP")或 modbus_tcp
(带 7 字节 MBAP 头)。**缺省 RTU 是既有行为的锁**:不传这个参数,所有老用例看到的字节一字不变。
从站行为(寄存器表、异常码、故障注入)只写一份:MBAP 帧先翻成 RTU 帧交给同一段逻辑,应答再翻回去。
收到与自己帧格式不符的帧时当场报错并断开,不去猜——按 RTU 帧解一个 MBAP 流会解出"看着正常"的假读数。

    python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml --frame modbus_tcp

一个 mock 进程 = 一条物理总线(一个监听端口)。同一条总线上同时来了两个请求者
(两条连接同时开着,或者一帧还没答完又来一帧)在真实台架上就是串话、抢总线,
这里把它记下来:`--contention-log` 给一个文件时每条记录落一行 JSON,
不给就打到 stderr。并行分组是否真的防住了抢总线,靠这份记录判,不靠"跑绿了"推断。

--slave 可以给多次:一条总线上挂着几台从站(不同从站地址)时,
寄存器表这些从站共用一份 —— 抢总线这件事不关心各表的值是多少。

    python tools/mock_modbus_meter.py --port 15020 --slave 1 --slave 2 \\
        --registers configs/mock_registers.yaml --fail slow --delay 0.4 --contention-log /tmp/bus.jsonl

停止:Ctrl+C,或者启动时加 --stop-on-stdin-close 后关掉 stdin(给脚本启停用,
两个路径都走 shutdown(),监听套接字由它关掉,退出码 0)。

同一端口只允许一个 mock:第二个实例启动时就以退出码 1 失败并打印原因,
而不是两个进程同时监听、陈旧的 mock 静默替新的应答。注意这条和上面的抢占记录不是一回事:
那是"两个 mock 抢一个端口",这里记的是"一个 mock(= 一条总线)上来了两个请求者"。

TCP 上跑的是哪一种帧由 --frame 定:缺省 RTU 帧(串口服务器的常见形态),--frame modbus_tcp 才是
Modbus TCP(帧里有 MBAP 头,靠长度字段切帧而不是 CRC)。两种帧的 PDU 逐字节相同,差别只在帧头。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

# 平台代码从哪来:装了包就在 site-packages(直接可 import);源码布局里由调用方给
# PYTHONPATH(任务执行器走 `paths.sys_path`)。这里**不猜目录层级** —— 猜错会静默 import 到
# 另一个 worktree 的代码(票 11c)。实在 import 不到就一次说清怎么补。
try:
    import testplatform  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - 只有手工裸跑这个脚本才会走到
    sys.stderr.write(
        "找不到 testplatform 包:装了平台就直接跑;源码布局请设 PYTHONPATH=<仓库>/src"
    )
    raise

from testplatform.protocol import modbus_rtu, modbus_tcp  # noqa: E402

# 03/04 读、06 写单个的请求帧固定 8 字节:从站侧靠这个长度切帧。
REQUEST_LENGTH = 8
# 任何请求帧的前 7 个字节:从站地址 1 | 功能码 1 | 起始寄存器 2 | 寄存器数 2 | (10 的字节数) 1。
# 10 写多个的总长从这里定:后面跟 寄存器数据 + CRC。
HEADER_LENGTH = 7

FAULTS = ("none", "timeout", "bad-crc", "slow", "write-reject")
# 与清单里的 connection.simulated_frames 同一套取值:帧格式在这两处是同一个概念。
FRAMES = ("modbus_rtu", "modbus_tcp")
# MBAP 长度字段 = 单元号 1 + PDU;PDU 上限 253 字节(协议规定),最少 1 个功能码。
MBAP_MIN_LENGTH, MBAP_MAX_LENGTH = 2, 254
# 请求 PDU 固定 5 字节的那几个功能码(读寄存器 2 字节地址 + 2 字节个数,写单个 地址 + 值)。
FIXED_PDU_REQUESTS = (
    modbus_rtu.READ_HOLDING_REGISTERS,
    modbus_rtu.READ_INPUT_REGISTERS,
    modbus_rtu.WRITE_SINGLE_REGISTER,
)


class ContentionLog:
    """一条总线上的抢占记录:同一个 mock 进程上同时来了两个请求者。

    给了路径就每条记录一行 JSON 落盘(并行实测脚本按行解析),没给就打到 stderr。
    记录格式:{"kind": "session-overlap" | "frame-overlap", "at": 时间戳,
    "connection": 后来的连接线号, "other": 先到的那条}。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.count = 0

    def record(self, kind: str, connection: int, other: int) -> None:
        entry = {
            "kind": kind,
            "at": round(time.time(), 6),
            "connection": connection,
            "other": other,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.count += 1
            if self._path is None:
                print(f"mock 抢占 {line}", file=sys.stderr, flush=True)
            else:
                with self._path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")


class MockModbusMeter:
    """一个从站的应答逻辑:请求帧进,应答帧出。故障注入也在这里。"""

    def __init__(
        self,
        slaves: Iterable[int],
        registers: Dict[int, int],
        fault: str = "none",
        delay: float = 2.0,
    ) -> None:
        self._slaves = frozenset(slaves)
        self._registers = registers
        self._fault = fault
        self._delay = delay

    def handle(self, request: bytes) -> Optional[bytes]:
        """一个请求帧 → 一个应答帧;None 表示这个故障模式故意不回答。"""
        if request[0] not in self._slaves:
            return None  # 不是发给我的帧:真实从站不应答别人的请求
        if self._fault == "slow":
            time.sleep(self._delay)
        if self._fault == "timeout":
            return None
        response = self._answer(request)
        if self._fault == "bad-crc":
            # 只动 CRC 最后一个字节:帧长、地址、功能码、数据都合法,只有校验错。
            return response[:-1] + bytes([response[-1] ^ 0xFF])
        return response

    def _answer(self, request: bytes) -> bytes:
        """按功能码分派。写路径的应答是回显,值有没有生效由客户端回读判,不在这里判。"""
        function = request[1]
        if function == modbus_rtu.WRITE_SINGLE_REGISTER:
            return self._write_single(request)
        if function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
            return self._write_multiple(request)
        frame_slave, function, address, count = modbus_rtu.parse_read_request(request)
        payload = b"".join(self._word(address + offset) for offset in range(count))
        return modbus_rtu.build_read_response(frame_slave, payload, function)

    def _write_single(self, request: bytes) -> bytes:
        """06 写单个寄存器:值进寄存器表,应答是请求帧原样回显(Modbus 规定)。

        写和读看同一张寄存器表,所以写后立刻读能读到新值;地址在表里存不存在也是同一个判定。
        """
        slave, function, address, value = modbus_rtu.parse_write_single_request(request)
        if self._fault == "write-reject":
            return self._exception(slave, modbus_rtu.ILLEGAL_DATA_VALUE, function)
        if address not in self._registers:
            return self._exception(slave, modbus_rtu.ILLEGAL_DATA_ADDRESS, function)
        self._registers[address] = value
        return request

    def _write_multiple(self, request: bytes) -> bytes:
        """10 写多个寄存器:地址与个数都合法才写,应答回显起始地址 + 寄存器数(没有数据段)。"""
        slave, function, address, count, payload = modbus_rtu.parse_write_multiple_request(request)
        if self._fault == "write-reject":
            return self._exception(slave, modbus_rtu.ILLEGAL_DATA_VALUE, function)
        if not 1 <= count <= modbus_rtu.WRITE_MULTIPLE_MAX_REGISTERS:
            # 个数不合法是"值不合法"(拆小重发能过),不是"地址不存在"。
            return self._exception(slave, modbus_rtu.ILLEGAL_DATA_VALUE, function)
        if any(address + offset not in self._registers for offset in range(count)):
            # 一个寄存器都不写:半截写下去比整帧拒掉难查得多。
            return self._exception(slave, modbus_rtu.ILLEGAL_DATA_ADDRESS, function)
        for offset in range(count):
            self._registers[address + offset] = int.from_bytes(
                payload[offset * 2 : offset * 2 + 2], "big"
            )
        return modbus_rtu.build_write_multiple_response(slave, address, count)

    def _exception(self, slave: int, code: int, function: int) -> bytes:
        """从站拒了这一写:回异常应答,而不是断连接 —— 拒绝和链路没回话是两件事。"""
        return modbus_rtu.build_exception_response(slave, code, function)

    def _word(self, address: int) -> bytes:
        if address not in self._registers:
            # 不拿 0 顶替:一个"看着正常"的假读数比报错难查得多,直接断掉这条连接。
            raise ValueError(
                f"寄存器表里没有地址 {address},已声明: {sorted(self._registers)}"
            )
        return self._registers[address].to_bytes(2, "big")


class MockServer:
    """监听、接受连接、每条连接一个线程收发 —— 一条连接卡住不影响别的连接。"""

    def __init__(
        self,
        meter: MockModbusMeter,
        host: str,
        port: int,
        contention: Optional[ContentionLog],
        frames: str,
    ) -> None:
        self._meter = meter
        self._frames = frames
        self._address = (host, port)
        self._contention = contention or ContentionLog()
        self._listener: Optional[socket.socket] = None
        self._stop = threading.Event()
        # socket → 线号。这条总线上一共有几条连接开着,就是"几个请求者在抢"。
        self._connections: Dict[socket.socket, int] = {}
        # 线号:拿 id(socket) 记进抢占记录的话,回收后会被复用。
        self._next_number = 0
        self._in_flight: Optional[int] = None  # 正在应答的那条连接的线号
        self._lock = threading.Lock()

    def start(self) -> Tuple[str, int]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 端口独占:两个活着的 mock 同时监听同一端口时,第二个会静默替第一个应答,
        # 于是 --fail timeout 的用例读到一个陈旧的合法值、假绿。
        # Windows 的 SO_REUSEADDR 恰恰允许这种重复绑定,只有 SO_EXCLUSIVEADDRUSE 是独占;
        # POSIX 的 SO_REUSEADDR 只放过 TIME_WAIT,第二个活监听照样 bind 失败。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(self._address)
        except OSError:
            listener.close()
            raise
        listener.listen(8)
        listener.settimeout(0.2)  # 让 accept 定期回来看停止标志,退出不必再等一个新连接
        self._listener = listener
        host, port = listener.getsockname()[:2]
        return host, port

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return  # 监听套接字已经被 shutdown 关掉了
            with self._lock:
                self._next_number += 1
                number = self._next_number
                opened = sorted(self._connections.values())
                self._connections[connection] = number
            if opened:
                # 这条总线上已经有连接开着,又来一条:两个请求者同时在这条总线上。
                self._contention.record("session-overlap", number, opened[0])
            threading.Thread(
                target=self._serve_connection, args=(connection, number), daemon=True
            ).start()

    def _serve_connection(self, connection: socket.socket, number: int) -> None:
        try:
            while not self._stop.is_set():
                try:
                    request = _receive_request(connection, self._frames)
                except ValueError as exc:
                    # 连帧都切不出来(MBAP 从站收到 RTU 帧):说清是哪一种帧再断开。
                    print(f"mock: 请求帧认不出来,断开这条连接: {exc}", file=sys.stderr, flush=True)
                    return
                if request is None:
                    return
                self._request_started(number)
                try:
                    response = self._answer(request)
                except ValueError as exc:
                    print(
                        f"mock: 请求帧处理失败,断开这条连接 {request.hex()}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                finally:
                    self._request_finished(number)
                if response is not None:
                    connection.sendall(response)
        except OSError:
            pass  # 客户端断开、或者关停时被掐断,都不是错
        finally:
            with self._lock:
                self._connections.pop(connection, None)
            connection.close()

    def _answer(self, request: bytes) -> Optional[bytes]:
        """线上那一帧 → 应答帧。从站行为只有一份:MBAP 帧翻成 RTU 帧交给它,应答再翻回去。

        RTU 逐字节原样转交:既有 RTU 这条路上,加 MBAP 前后发出去的字节完全一样。
        应答为 None(故障注入 / 不是发给我的)在两种帧下都是"不回答"。
        """
        if self._frames == "modbus_rtu":
            return self._meter.handle(request)
        transaction, rtu_request = _to_rtu(request)
        response = self._meter.handle(rtu_request)
        return None if response is None else _to_mbap(transaction, response)

    def _request_started(self, number: int) -> None:
        """一帧开始应答:上一帧还没答完又来一帧,就是这条总线上的两个请求者撞在一起了。"""
        with self._lock:
            holder, self._in_flight = self._in_flight, number
        if holder is not None and holder != number:
            self._contention.record("frame-overlap", number, holder)

    def _request_finished(self, number: int) -> None:
        with self._lock:
            if self._in_flight == number:
                self._in_flight = None

    def shutdown(self) -> None:
        """关监听、断所有连接:进程退出后端口上什么都不留。"""
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._lock:
            connections, self._connections = list(self._connections), {}
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


def _receive_request(connection: socket.socket, frames: str) -> Optional[bytes]:
    """socket 是字节流:读满一帧才算一个请求,半个帧不算。帧长怎么算由帧格式定。

    RTU:03/04/06 固定 8 字节。10 写多个的长度随寄存器数变,所以先读 7 个字节的帧头:
    里面的字节数说了数据段有多长。MBAP 的长度字段说整帧多长(见 _receive_mbap_request)。
    """
    if frames == "modbus_tcp":
        return _receive_mbap_request(connection)
    header = _read_exactly(connection, HEADER_LENGTH)
    if header is None:
        return None
    if header[1] == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
        tail = _read_exactly(connection, header[6] + 2)  # 寄存器数据 + CRC
    else:
        tail = _read_exactly(connection, REQUEST_LENGTH - HEADER_LENGTH)
    if tail is None:
        return None
    return header + tail


def _receive_mbap_request(connection: socket.socket) -> Optional[bytes]:
    """MBAP 帧:先读 7 字节头,长度字段(单元号 + PDU)说后面还有多少字节。

    协议号不是 0 就当场报错:RTU 帧喂给 MBAP 从站时,从站地址会落在事务号上、起始寄存器落在
    协议号上,照着一个假长度往下读只会把这条连接吊到超时 —— 现场看到的是一句超时,不是一句
    "帧格式不对"。
    """
    header = _read_exactly(connection, modbus_tcp.MBAP_HEADER_BYTES)
    if header is None:
        return None
    protocol = int.from_bytes(header[2:4], "big")
    if protocol != modbus_tcp.PROTOCOL_ID:
        raise ValueError(
            f"不是 MBAP 帧: 协议号必须是 {modbus_tcp.PROTOCOL_ID},帧头里是 {protocol:#06x}"
            f"(这个从站按 --frame modbus_tcp 应答): {header.hex()}"
        )
    declared = int.from_bytes(header[4:6], "big")
    if not MBAP_MIN_LENGTH <= declared <= MBAP_MAX_LENGTH:
        raise ValueError(
            f"MBAP 长度字段是 {declared},不可能(单元号 1 + PDU 1–253): {header.hex()}"
        )
    function = _read_exactly(connection, 1)
    if function is None:
        return None
    # 长度字段与功能码必须自洽。不查这一条的话,地址 0 上的 RTU 帧也能冒充成
    # "协议号 0 + 长度 3"的 MBAP 帧,照它往下读就是把这个连接吊到超时。
    if not _mbap_length_fits(function[0], declared):
        raise ValueError(
            f"不是 MBAP 帧: 功能码 {function[0]:#04x} 与长度字段 {declared} 凑不出一帧合法请求"
            f"(这个从站按 --frame modbus_tcp 应答): {(header + function).hex()}"
        )
    tail = _read_exactly(connection, declared - 2)
    if tail is None:
        return None
    return header + function + tail


def _mbap_length_fits(function: int, declared: int) -> bool:
    """长度字段(单元号 + PDU)与功能码自洽吗:03/04/06 的请求 PDU 固定 5 字节,0x10 是 6 + 2N。"""
    if function in FIXED_PDU_REQUESTS:
        return declared == 6
    if function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
        return declared >= 9 and (declared - 7) % 2 == 0
    return False


def _to_rtu(frame: bytes) -> Tuple[int, bytes]:
    """MBAP 帧 → (事务号, 等价的 RTU 帧):从站逻辑只认 RTU 那一份,两种帧不会各长一套规则。"""
    if len(frame) <= modbus_tcp.MBAP_HEADER_BYTES:
        raise ValueError(f"MBAP 帧里没有功能码: {frame.hex()}")
    function = frame[modbus_tcp.MBAP_HEADER_BYTES]
    if function == modbus_rtu.WRITE_SINGLE_REGISTER:
        transaction, unit, _, address, value = modbus_tcp.parse_write_single_request(frame)
        return transaction, modbus_rtu.build_write_single_request(unit, address, value)
    if function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
        transaction, unit, _, address, _, payload = modbus_tcp.parse_write_multiple_request(frame)
        return transaction, modbus_rtu.build_write_multiple_request(unit, address, payload)
    transaction, unit, function, address, count = modbus_tcp.parse_read_request(frame)
    return transaction, modbus_rtu.build_read_request(unit, address, count, function)


def _to_mbap(transaction: int, response: bytes) -> bytes:
    """RTU 应答帧 → MBAP 应答帧:去掉尾部 CRC、补上 7 字节头,事务号原样还回去。"""
    body = response[:-2]
    unit, function = body[0], body[1]
    if function & 0x80:
        return modbus_tcp.build_exception_response(transaction, unit, body[2], function & ~0x80)
    if function in (modbus_rtu.WRITE_SINGLE_REGISTER, modbus_rtu.WRITE_MULTIPLE_REGISTERS):
        # 0x06 / 0x10 的应答都是 6 字节回显(地址 + 值 / 个数):MBAP 下就是同一个 PDU 加个头,
        # 功能码照抄 —— 用 0x10 的构造器拼 0x06 的回显会把功能码写错。
        return modbus_tcp.build_write_multiple_response(
            transaction,
            unit,
            int.from_bytes(body[2:4], "big"),
            int.from_bytes(body[4:6], "big"),
            function=function,
        )
    return modbus_tcp.build_read_response(transaction, unit, body[3:], function)


def _read_exactly(connection: socket.socket, count: int) -> Optional[bytes]:
    """读满 count 字节;对端先关了连接就返回 None(半个帧不当一个请求)。"""
    buffer = b""
    while len(buffer) < count:
        chunk = connection.recv(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer


def _load_registers(path: Path) -> Dict[int, int]:
    """寄存器表:寄存器地址 → 16 位无符号值。"""
    if not path.exists():
        raise SystemExit(f"寄存器表不存在: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        import yaml  # 平台已有依赖(pyyaml),不是为 mock 新加的

        raw = yaml.safe_load(text)
    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"寄存器表必须写成非空映射(寄存器地址 → 值): {path}")
    registers = {}
    for address, value in raw.items():
        if not (isinstance(value, int) and 0 <= value <= 0xFFFF):
            raise SystemExit(f"寄存器 {address} 的值必须是 0–65535 的整数,拿到 {value!r}")
        registers[int(address)] = value
    return registers


def _stop_on_stdin_close(server: MockServer) -> None:
    """stdin 读到 EOF 就当停止信号:比 terminate 干净,退出码仍是 0。"""
    sys.stdin.read()
    server.shutdown()


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立 Modbus 从站(TCP 承载 RTU 帧或 MBAP 帧)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址,缺省 127.0.0.1")
    parser.add_argument("--port", type=int, default=15020, help="监听端口,0 表示由系统分配")
    parser.add_argument(
        "--slave",
        type=int,
        action="append",
        default=None,
        help="从站地址 1–247,缺省 1;一条总线上挂几台就从复给几次",
    )
    parser.add_argument("--registers", required=True, help="寄存器表文件(yaml 或 json)")
    parser.add_argument(
        "--contention-log",
        default=None,
        help="抢占记录落盘路径(Json Lines);不给就打到 stderr",
    )
    parser.add_argument(
        "--fail",
        choices=FAULTS,
        default="none",
        help="故障注入:timeout / bad-crc / slow / write-reject(bad-crc 改的是 RTU 的 CRC,"
        "对没有 CRC 的 MBAP 帧不生效)",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="--fail slow 时的延迟秒数")
    parser.add_argument(
        "--frame",
        choices=FRAMES,
        default="modbus_rtu",
        help="从站说哪种帧:modbus_rtu(缺省)/ modbus_tcp(带 MBAP 头)",
    )
    parser.add_argument(
        "--stop-on-stdin-close",
        action="store_true",
        help="stdin 关闭时当停止信号(脚本启停用;Crtl+C 之外的第二种干净退出方式)",
    )
    args = parser.parse_args(argv)
    args.slave = args.slave or [1]
    for slave in args.slave:
        if not 1 <= slave <= 247:
            parser.error(f"--slave 必须是 1–247 的整数(Modbus 从站地址),拿到 {slave}")
    if args.delay < 0:
        parser.error(f"--delay 不能是负数,拿到 {args.delay}")
    return args


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parse_args(argv)
    registers = _load_registers(Path(args.registers))
    meter = MockModbusMeter(
        slaves=args.slave, registers=registers, fault=args.fail, delay=args.delay
    )
    contention = ContentionLog(Path(args.contention_log) if args.contention_log else None)
    server = MockServer(meter, args.host, args.port, contention, args.frame)
    try:
        host, port = server.start()
    except OSError as exc:
        # 不打印"监听成功"就退出:这一行就是给"端口上到底是谁"用的。
        print(f"mock 启动失败:{args.host}:{args.port} 端口已被占用,无法监听({exc})", file=sys.stderr)
        print(
            "换一个 --port,或者先停掉占用它的进程(每个 mock 启动时会打印自己的 pid)",
            file=sys.stderr,
            flush=True,
        )
        return 1
    slaves = ",".join(str(slave) for slave in args.slave)
    # 这一行是给脚本用的:--port 0 时从里面读实际端口;pid 用来查"谁占着这个端口"。
    print(
        f"mock Modbus 从站监听 {host}:{port} | 从站地址 {slaves} "
        f"| 寄存器 {len(registers)} 个 | 帧格式 {args.frame} | 故障注入 {args.fail} | pid {os.getpid()}"
        + (f" | 抢占记录 {args.contention_log}" if args.contention_log else ""),
        flush=True,
    )
    try:
        if args.stop_on_stdin_close:
            threading.Thread(target=_stop_on_stdin_close, args=(server,), daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if contention.count:
            print(
                f"mock:本轮记录到 {contention.count} 次总线抢占(同一条总线上同时来了两个请求者)",
                file=sys.stderr,
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
