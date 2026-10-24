"""真机传输的"模拟器永远测不出来"的隐患,以及六类真机传输各自的契约。

全部用假库:serial / paramiko / python-can / requests / paho-mqtt 用 monkeypatch
注入 sys.modules,socket 是标准库里的假对象。所以这些用例在没有装任何可选依赖的
环境里也照跑 —— 它们锁的是"真机上会挂、模拟器里永远绿"的行为,不是"库装没装"。

被锁的行为:
1. receive 收满一帧才返回:短读继续读,超时还差字节就抛 IncompleteFrameError,半帧不外流;
   帧长由调用方(驱动的 reader)按规约给,transport 不认识任何规约。
   链路没了不是超时:TCP 的 recv 返回 b"" 只表示对端关闭,这一路径招的是 ConnectionError。
2. exec_command 先读完 stdout/stderr 再取退出码:输出超过 SSH 通道窗口时不会互相等死。
3. has_client() 只说"底层句柄还在",不冒充链路健康:对端 RST、USB 串口被拔掉之后它仍是 True,
   链路死没死以 send/receive 抛出的异常为准。
4. 六类的构造参数、缺可选依赖时的安装指引、与各自 Protocol 的形状一致。
5. CAN 接口类型进清单,缺省按平台给,未知值在装配前报错并列出可用值 ——
   三条校验都从 parse_manifest 进:清单里真的能配出一台 CAN 设备。
6. 真串口的换向卫生:send 前清掉输入缓冲的残留(不清就是下一帧错位、解出假读数);
   inter_frame_delay_ms 给帧间静默,缺省 0 时一次 sleep 都不调。
   """
from __future__ import annotations

import copy
import logging
import re
import socket
import sys
import time
import types
from pathlib import Path

import pytest
import yaml

from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.config.manifest import CAN_INTERFACES, parse_manifest
from testplatform.protocol import dlt645, modbus_rtu
from testplatform.transport import real_transports, simulators
from testplatform.transport.gateway_transport import GatewaySshTransport
from testplatform.transport.interfaces import (
    ByteStreamTransport,
    CanTransport,
    HttpTransport,
    IncompleteFrameError,
    MqttTransport,
    SshTransport,
)
from testplatform.transport.real_transports import (
    MqttBrokerUnreachable,
    RealCanTransport,
    RealHttpTransport,
    RealMqttTransport,
    RealSerialTransport,
    RealSshTransport,
    RealTcpTransport,
    default_can_interface,
)
from testplatform.transport.registry import build_transport, registered_keys


class _FakeSerialPort:
    """假串口:把应答按 chunks 逐段交出来,交完就一直返回空(等于链路静默/超时)。"""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = [bytes(chunk) for chunk in chunks]
        self.timeout: float | None = None
        self.is_open = True
        self.written = b""
        self.reset_count = 0

    def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= size:
            return self._chunks.pop(0)
        self._chunks[0] = chunk[size:]
        return chunk[:size]

    def write(self, data: bytes) -> None:
        self.written += data

    def reset_input_buffer(self) -> None:
        """真实 pyserial 的同名方法:丢掉输入缓冲里还没读的字节。"""
        self.reset_count += 1
        self._chunks = []

    def feed(self, data: bytes) -> None:
        """模拟设备在某个时刻才把应答放到线上(假串口没有时间概念,测试自己决定时点)。"""
        self._chunks.append(bytes(data))

    def close(self) -> None:
        self.is_open = False


def _inject_serial(monkeypatch, port: _FakeSerialPort) -> None:
    """把假 serial 模块塞进 sys.modules:real_transports 是在方法里 import 它的。"""
    module = types.ModuleType("serial")
    module.Serial = lambda *args, **kwargs: port
    monkeypatch.setitem(sys.modules, "serial", module)


def _serial(monkeypatch, *chunks: bytes) -> RealSerialTransport:
    _inject_serial(monkeypatch, _FakeSerialPort(*chunks))
    transport = RealSerialTransport(port="COM3", baudrate=9600)
    transport.connect()
    return transport


class _FakeClock:
    """假时钟:换掉 real_transports 里的 time 模块名。sleep 只记录不真等,
    所以「有没有 sleep」和「等了多久」都是可断言的,不靠跑得快不快要"看上去"。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeSocket:
    """假 socket:recv 也按段交出字节;交完就与真实 socket 一样静默到超时 ——
    等满 settimeout 给的时限抛 socket.timeout(真实 recv 返回 b"" 只表示对端关闭,
    那种情形看下面的 _ClosingSocket)。"""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = [bytes(chunk) for chunk in chunks]
        self.timeout: float | None = None
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            raise socket.timeout("timed out")
        chunk = self._chunks[0]
        if len(chunk) <= size:
            return self._chunks.pop(0)
        self._chunks[0] = chunk[size:]
        return chunk[:size]

    def sendall(self, data: bytes) -> None:
        pass

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


class _PacedSocket(_FakeSocket):
    """按"这块字节什么时候到"交出字节:每次 recv 推进假时钟。

    所以"分片间隔多长 / 静默多久"是可断言的,不靠跑得快不快要"看上去";
    要测的那个分支(继续读还是收工)才不会被真实调度时序弄成偶绿。
    """

    def __init__(self, clock: _FakeClock, *plan: tuple[bytes, float]) -> None:
        super().__init__()
        self._clock = clock
        self._plan = list(plan)
        self.waits: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout
        self.waits.append(timeout)

    def recv(self, size: int) -> bytes:
        if not self._plan:
            raise socket.timeout("timed out")
        chunk, spent = self._plan.pop(0)
        self._clock.now += spent  # 这块字节在路上花了这么久
        return chunk


class _ClosingSocket(_FakeSocket):
    """对端正常关闭(FIN):recv 不抛异常、返回 b""—— 真实 socket 上 b"" 只有这一个含义。"""

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        return super().recv(size)


def _tcp_with(monkeypatch, sock) -> RealTcpTransport:
    # socket 是标准库,不是可选依赖:直接换掉 create_connection 就够,sys.modules 不用动。
    monkeypatch.setattr(real_transports.socket, "create_connection", lambda *args, **kwargs: sock)
    transport = RealTcpTransport(host="10.0.0.5", port=502)
    transport.connect()
    return transport


def _tcp(monkeypatch, *chunks: bytes) -> RealTcpTransport:
    return _tcp_with(monkeypatch, _FakeSocket(*chunks))


# ------------------------------------------------------- 短读:收满一帧 ----

def test_serial_receive_joins_a_reply_that_arrives_in_two_pieces(monkeypatch):
    """9600 波特下的长应答常被拆成两段:交上来的必须是完整一帧,不是前半段。"""
    reply = modbus_rtu.build_read_response(1, b"\x00\x22\x01\x2c")
    transport = _serial(monkeypatch, reply[:4], reply[4:])

    frame = transport.receive(timeout=0.5, expected=modbus_rtu.response_length)

    assert frame == reply
    assert modbus_rtu.parse_read_response(frame, 1) == b"\x00\x22\x01\x2c"


def test_tcp_receive_joins_a_reply_that_arrives_in_two_pieces(monkeypatch):
    reply = dlt645.build_read_response("000000000001", "00010000", b"\x78\x56\x34\x12")
    transport = _tcp(monkeypatch, reply[:6], reply[6:])

    frame = transport.receive(timeout=0.5, expected=dlt645.response_length)

    assert frame == reply
    assert dlt645.parse_read_response(frame, "000000000001", "00010000") == b"\x78\x56\x34\x12"


def test_short_read_within_the_timeout_does_not_end_the_frame(monkeypatch):
    """半帧之后链路还会继续出字节:只要没超时就不许提前收工。"""
    reply = dlt645.build_read_response("000000000001", "00010000", b"\x78\x56\x34\x12")
    transport = _serial(monkeypatch, reply[:10], reply[10:11], reply[11:])

    assert transport.receive(timeout=0.5, expected=dlt645.response_length) == reply


# ------------------------------------------------- 半帧 + 超时:报错不外流 ----

def test_serial_receive_raises_instead_of_handing_up_half_a_frame(monkeypatch):
    """只收到半帧就超时:必须抛异常 —— 半帧往下解只会得到"看着正常"的假读数。"""
    reply = modbus_rtu.build_read_response(1, b"\x00\x22\x01\x2c")
    transport = _serial(monkeypatch, reply[:4])

    with pytest.raises(IncompleteFrameError) as caught:
        transport.receive(timeout=0.2, expected=modbus_rtu.response_length)

    # 报错要能排障:差多少字节、收到了什么都在消息里。
    assert "4" in str(caught.value) and "9" in str(caught.value)
    assert reply[:4].hex() in str(caught.value)


def test_half_a_frame_is_a_timeout_so_evidence_records_it_as_one(monkeypatch):
    """证据层按 TimeoutError 认超时:半帧也要落成 error: timeout,不能变成异常类名。"""
    transport = _serial(monkeypatch, b"\x01\x03")

    with pytest.raises(TimeoutError):
        transport.receive(timeout=0.2, expected=9)


# -------------------------------- 断链 vs 设备不回话:两种结论 --------------------------------

def test_tcp_receive_calls_a_closed_peer_a_link_failure_not_a_timeout(monkeypatch):
    """对端正常关闭(拔网线、设备断电、串口服务器重启)时 recv 返回 b"":这是断链,不是超时。
    现场处置完全不同:超时可能是设备问题(判不合格),断链是台架问题(判测试无效)。"""
    transport = _tcp_with(monkeypatch, _ClosingSocket())

    with pytest.raises(ConnectionError) as caught:
        transport.receive(timeout=0.5, expected=9)

    assert not isinstance(caught.value, TimeoutError), "断链不能落进超时那一族异常"
    assert "关闭" in str(caught.value)


def test_tcp_receive_when_the_peer_closes_mid_frame_is_a_link_failure_too(monkeypatch):
    """收到一半时对端关了:异常原样上抛,不包成 IncompleteFrameError(那会被记成超时)。"""
    transport = _tcp_with(monkeypatch, _ClosingSocket(b"\x01\x03"))

    with pytest.raises(ConnectionError) as caught:
        transport.receive(timeout=0.5, expected=9)

    assert not isinstance(caught.value, TimeoutError)


def test_tcp_receive_times_out_as_incomplete_frame_when_the_device_does_not_answer(monkeypatch):
    """设备收下请求、一个字不回:真实 socket 等满剩余时间抛 socket.timeout,
    收帧这一层仍以 IncompleteFrameError 收场,报错里带已收字节数(证据仍记 error: timeout)。"""
    transport = _tcp_with(monkeypatch, _FakeSocket())

    with pytest.raises(IncompleteFrameError) as caught:
        transport.receive(timeout=0.2, expected=9)

    assert not isinstance(caught.value, ConnectionError)
    assert "收到 0 字节" in str(caught.value)


def test_serial_still_calls_a_dead_link_a_timeout_because_serial_has_no_eof(monkeypatch):
    """串口 read 超时返回 b"",拔线抛 SerialException:没有"对端关闭"这个信号,
    所以串口只能把断链和沉默都归成超时 —— 硬件语义,不是 TCP 那条路漏了。"""
    transport = _serial(monkeypatch)  # 一个字都不给

    with pytest.raises(IncompleteFrameError) as caught:
        transport.receive(timeout=0.2, expected=9)

    assert not isinstance(caught.value, ConnectionError)


# ---------------------------------------------- 长帧:不按缓冲区静默截断 ----

def test_serial_receive_returns_a_frame_longer_than_one_read_buffer(monkeypatch):
    """帧长超过旧的 256 字节缓冲区:收满,不截断(截断出来的帧比没收到更危险)。"""
    reply = bytes(range(256)) + bytes(range(44))
    transport = _serial(monkeypatch, reply)

    assert transport.receive(timeout=0.5, expected=len(reply)) == reply


def test_unknown_length_receive_is_not_capped_at_one_read_buffer(monkeypatch):
    """长度未知的一行文本(vendor_ascii)也不会被 256 截断。"""
    line = b"220.1 50.02 " + b"0 " * 200 + b"\n"
    transport = _serial(monkeypatch, line)

    assert transport.receive(timeout=0.5) == line


# ---------------------- 长度未知(expected=None):不截断一轮读取 ----------------------
# 承诺边界:receive(None) 只承诺**不把一轮读截在半路**,**不承诺帧完整** ——
# 帧完整性由知道规约的上层解出来判。三种结局必须分得开:
#   gap 内静默         → 一轮结束,返回已读字节(不报错)
#   timeout 耗尽、字节还在滴 → IncompleteFrameError,消息点名 read_gap_ms / timeout / 已读字节
#   EOF / 断链          → ConnectionError 原样上抛,不混进上面两种


@case(id="T_00_00_13_000000_01", clause="free: 平台:传输", tags=["传输", "契约"])
def test_tcp_unknown_length_receive_joins_a_frame_that_arrives_in_two_pieces(monkeypatch):
    """TCP 是流协议,一次 recv 可能只给出半个帧:expected=None 也必须读到这一轮静默才返回。

    这条是**正常分片**(两块间隔 10ms < read_gap_ms=50),不是断链 —— 断链是 recv 返回 b"",
    见下面那条。一块就收工的话,上层拿到的是半行,"解不出来"比"没收到"更难查。
    """
    clock = _FakeClock()
    monkeypatch.setattr(real_transports, "time", clock)
    line = b"220.1 50.02 0.0\n"
    sock = _PacedSocket(clock, (line[:8], 0.0), (line[8:], 0.010))
    transport = _tcp_with(monkeypatch, sock)

    assert transport.receive(timeout=2.0) == line
    # 分片之间的等待是 gap(50ms),不是把整个 timeout 等满:快设备不该每帧白等 2 秒。
    assert sock.waits[1] == pytest.approx(0.05)


@case(id="T_00_00_13_000000_02", clause="free: 平台:传输", tags=["传输", "契约"])
def test_tcp_unknown_length_receive_reports_the_exhausted_timeout_not_half_a_frame(monkeypatch):
    """对端一直在滴字节、总时长用完:报错,不把"还在流里"的半帧当一帧返回。

    消息要能排障:已读多少字节、read_gap_ms 与 timeout 各是多少。这条证明的是
    "**时长不够**",不是"这一轮被截断" —— 截断与否只有上层知道规约才判得出来。
    """
    clock = _FakeClock()
    monkeypatch.setattr(real_transports, "time", clock)
    sock = _PacedSocket(clock, *[(b"\x01", 0.05)] * 8)
    transport = _tcp_with(monkeypatch, sock)

    with pytest.raises(IncompleteFrameError) as caught:
        transport.receive(timeout=0.2, read_gap_ms=50)

    message = str(caught.value)
    assert re.search(r"已读 \d+ 字节", message), message
    assert "read_gap_ms=50" in message, message
    assert "timeout=0.2" in message, message


@case(id="T_00_00_13_000000_03", clause="free: 平台:传输", tags=["传输", "契约"])
def test_a_read_gap_longer_than_the_timeout_is_rejected_before_a_single_read(monkeypatch):
    """gap 比总时长还长 = gap 永远等不到,读取退化成一次 recv —— 那就把本票的 bug 配回来了。

    配错要在开读之前报,不是跑起来看到一个诡异的结果(同族:可配置项把保护关掉)。
    """
    clock = _FakeClock()
    monkeypatch.setattr(real_transports, "time", clock)
    sock = _PacedSocket(clock)
    transport = _tcp_with(monkeypatch, sock)

    with pytest.raises(ValueError) as caught:
        transport.receive(timeout=0.2, read_gap_ms=10_000)

    assert "read_gap_ms=10000" in str(caught.value) and "timeout=0.2" in str(caught.value)
    # gap=0 = 一次 recv(本票要修的那个 bug),也是配置错误;同样在开读之前拦下。
    with pytest.raises(ValueError):
        transport.receive(timeout=0.2, read_gap_ms=0)
    assert sock.waits == [], "配错了也在开读之前拦下,不能先 recv 一次再报"


@case(id="T_00_00_13_000000_04", clause="free: 平台:传输", tags=["传输", "契约"])
def test_tcp_unknown_length_receive_still_calls_a_closed_peer_a_link_failure(monkeypatch):
    """expected=None 也要把断链与"这一轮读完了"分开:recv 返回 b"" 是对端关闭,不是静默。
    11 号那张票讲的就是这条不能被误记成 timeout。"""
    transport = _tcp_with(monkeypatch, _ClosingSocket())

    with pytest.raises(ConnectionError) as caught:
        transport.receive(timeout=0.5)

    assert not isinstance(caught.value, TimeoutError)


# ------------------------ 真串口卫生:换向前清缓冲 + 帧间隔 ------------------------

def test_send_discards_the_leftover_bytes_of_the_previous_request(monkeypatch):
    """上一帧的残留(从站多发的一个字节、上一次超时后迟到的应答)必须在 send 前清掉:
    它留在缓冲里会让下一帧从头错位,错位解出来的往往是一个"看着正常"的假读数。"""
    port = _FakeSerialPort(b"\x01")  # 上一次请求的残留,一个字节就够錯位
    _inject_serial(monkeypatch, port)
    transport = RealSerialTransport(port="COM3", baudrate=9600)
    transport.connect()

    transport.send(modbus_rtu.build_read_request(1, 0, 1))
    reply = modbus_rtu.build_read_response(1, b"\x00\x22")
    port.feed(reply)  # 设备在这次请求之后才应答

    assert port.reset_count == 1
    assert transport.receive(timeout=0.5, expected=modbus_rtu.response_length) == reply
    assert modbus_rtu.parse_read_response(reply, 1) == b"\x00\x22"


def test_two_sends_on_the_same_connection_are_separated_by_the_configured_gap(monkeypatch):
    """9600 下 3.5 字符 ≈ 4ms:严格的从站会因为背靠背发送把两帧粘起来。
    配置了就必须等到位 —— 走清单 → 工厂这条路,顺带锁住键真的传到了传输层。"""
    clock = _FakeClock()
    monkeypatch.setattr(real_transports, "time", clock)
    _inject_serial(monkeypatch, _FakeSerialPort())
    transport = build_transport(
        "serial", "real", {"port": "COM3", "baudrate": 9600, "inter_frame_delay_ms": 4}
    )
    transport.connect()

    transport.send(modbus_rtu.build_read_request(1, 0, 1))
    transport.send(modbus_rtu.build_read_request(1, 0, 1))

    assert clock.sleeps == [pytest.approx(0.004)]
    assert clock.now - 1000.0 == pytest.approx(0.004)


def test_the_default_inter_frame_delay_never_sleeps(monkeypatch):
    """缺省 0 = 现有行为不变:sleep 一次都不能被调到,不能给所有跑法平白加延迟。"""
    clock = _FakeClock()
    monkeypatch.setattr(real_transports, "time", clock)
    _inject_serial(monkeypatch, _FakeSerialPort())
    transport = RealSerialTransport(port="COM3", baudrate=9600)
    transport.connect()

    transport.send(modbus_rtu.build_read_request(1, 0, 1))
    transport.send(modbus_rtu.build_read_request(1, 0, 1))

    assert clock.sleeps == [], "缺省帧间隔不算通过快速跑完:它必须一次 sleep 都没调"
    assert clock.now == 1000.0


# -------------------------------------------- 帧长由规约层算,不由 transport ----

def test_modbus_response_length_reads_the_byte_count_from_the_reply_header():
    normal = modbus_rtu.build_read_response(1, b"\x00\x22\x01\x2c")
    assert modbus_rtu.response_length(b"") is None  # 连从站地址都没到
    assert modbus_rtu.response_length(normal[:2]) is None  # 功能码到了,字节数还没到
    assert modbus_rtu.response_length(normal[:3]) == 9 == len(normal)
    # 异常应答固定 5 字节:按正常应答的字节数去等会白等到超时,把真正的错误盖掉。
    assert modbus_rtu.response_length(b"\x01\x83\x02") == 5


def test_dlt645_response_length_follows_the_l_field():
    frame = dlt645.build_read_response("000000000001", "00010000", b"\x78\x56\x34\x12")
    assert dlt645.response_length(frame[:9]) is None  # L 在第 10 字节
    assert dlt645.response_length(frame[:10]) == len(frame)


# ------------------------------------------- SSH:输出超过通道窗口不死锁 ----

class _FakeSshHost:
    """假 SSH 主机:输出超过通道窗口(~64 KiB)时,远端会卡在 write 上等对端读走。

    这时先 recv_exit_status() 就是在互相等死 —— 这里把死锁显式抛出来,
    而不是让用例真的挂住,所以没有 paramiko 也测得出来。
    """

    WINDOW = 64 * 1024

    def __init__(self, stdout: bytes, stderr: bytes = b"", exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.blocked = 0  # 还堆在通道窗口里、没被读走的字节数
        self.stdout = _FakeOutputStream(self, stdout)
        self.stderr = _FakeOutputStream(self, stderr)

    def exec_command(self, command: str, timeout: float | None = None):
        self.blocked = min(len(self.stdout.payload), self.WINDOW)
        return None, self.stdout, self.stderr

    def close(self) -> None:
        pass


class _FakeOutputStream:
    def __init__(self, host: _FakeSshHost, payload: bytes) -> None:
        self._host = host
        self.payload = payload
        self.channel = self  # paramiko 里退出码走 stdout.channel

    def read(self) -> bytes:
        data, self.payload = self.payload, b""
        self._host.blocked = 0  # 读走了,远端才能继续写、才能退出
        return data

    def recv_exit_status(self) -> int:
        if self._host.blocked:
            raise RuntimeError(
                f"死锁:{self._host.blocked} 字节还堆在通道窗口里,先等退出码永远等不到"
            )
        return self._host.exit_code


def _inject_paramiko(monkeypatch, host: _FakeSshHost, server_key=None) -> None:
    """假 paramiko。connect() 里调一次 missing_host_key:真 paramiko 在 client 自己没加载
    主机密钥时就是每次建连问策略 —— 不调钩子,三态就一条也测不出来。"""
    module = types.ModuleType("paramiko")

    class SSHClient:
        def set_missing_host_key_policy(self, policy) -> None:
            self.policy = policy

        def connect(self, hostname, *args, **kwargs) -> None:
            self.policy.missing_host_key(self, hostname, server_key or _SERVER_KEY)

        def exec_command(self, command, timeout=None):
            return host.exec_command(command, timeout)

        def close(self) -> None:
            pass

    module.SSHClient = SSHClient
    module.HostKeys = _FakeHostKeys
    monkeypatch.setitem(sys.modules, "paramiko", module)


class _FakePKey:
    """假公钥:只给指纹用得上的两个方法,与 paramiko.PKey 的 get_name / get_base64 同形。"""

    def __init__(self, name: str, base64: str) -> None:
        self._name = name
        self._base64 = base64

    def get_name(self) -> str:
        return self._name

    def get_base64(self) -> str:
        return self._base64


class _FakeHostKeys:
    """假 paramiko.HostKeys:读的是真 known_hosts 文件(明文行),不处理 |1|salt|hash。"""

    def __init__(self, filename=None) -> None:
        self._entries: dict[str, dict[str, _FakePKey]] = {}
        for line in Path(filename).read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) != 3 or line.lstrip().startswith("#"):
                continue
            host, name, base64 = fields
            self._entries.setdefault(host, {})[name] = _FakePKey(name, base64)

    def lookup(self, hostname):
        return self._entries.get(hostname)


# 网关/主机在三次态用例里自报的密钥。base64 只是字符串参数,没有真的做密码学。
_SERVER_KEY = _FakePKey("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIserverkey")
_OTHER_KEY = _FakePKey("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIotherkey")


def _write_known_hosts(tmp_path, host: str, *keys: _FakePKey) -> str:
    path = tmp_path / "known_hosts"
    path.write_text(
        "".join(f"{host} {key.get_name()} {key.get_base64()}\n" for key in keys), encoding="utf-8"
    )
    return str(path)


def _direct_ssh(known_hosts=None) -> RealSshTransport:
    return RealSshTransport(host="10.0.0.9", user="root", known_hosts=known_hosts)


def _gateway_ssh(known_hosts=None) -> GatewaySshTransport:
    return GatewaySshTransport("10.0.0.9", "root", "can0", known_hosts=known_hosts)


def _ssh_handle(transport):
    """底下那层真的 SSH 连接:网关把它当零件包在 _ssh 里。"""
    return getattr(transport, "_ssh", transport)


def test_ssh_exec_command_does_not_deadlock_on_output_larger_than_the_window(monkeypatch):
    """120 KiB 输出 > 通道窗口:先读完再取退出码,不阻塞、内容和退出码都对。"""
    output = b"dmesg line\n" * 12_000  # 132 KiB
    host = _FakeSshHost(output, exit_code=0)
    _inject_paramiko(monkeypatch, host)
    ssh = RealSshTransport(host="10.0.0.9", user="root")
    ssh.connect()

    result = ssh.exec_command("dmesg")

    assert result.exit_code == 0
    assert result.stdout == output.decode()


def test_ssh_exec_command_returns_exit_code_and_stderr(monkeypatch):
    host = _FakeSshHost(b"ok\n", stderr=b"warning: no such unit\n", exit_code=3)
    _inject_paramiko(monkeypatch, host)
    ssh = RealSshTransport(host="10.0.0.9", user="root")
    ssh.connect()

    result = ssh.exec_command("systemctl restart foo")

    assert (result.exit_code, result.stdout, result.stderr) == (
        3,
        "ok\n",
        "warning: no such unit\n",
    )


# ------------------------- SSH 主机密钥三态:未配置 / 匹配 / 不匹配 -------------------------

_TWO_SSH_PATHS = pytest.mark.parametrize(
    "build", [_direct_ssh, _gateway_ssh], ids=["直连ssh", "网关gateway_ssh"]
)


@_TWO_SSH_PATHS
def test_an_unconfigured_known_hosts_connects_reports_the_unverified_level_and_runs_commands(
    monkeypatch, caplog, build
):
    """未配置 known_hosts = 不校验主机密钥,与修复前完全一致:真的连上、真的能跑命令,
    并在建连时留一行日志说明这次连接的信任级别。

    断言写成“连上了并跑出了命令”(不是“没抛异常”):否则一个把连接全关掉的
    修复也能让这条绿。提示只数一次:一次建连只问一次策略,不按命令刷屏。
    """
    _inject_paramiko(monkeypatch, _FakeSshHost(b"dmesg: ok\n"))
    caplog.set_level(logging.WARNING, logger="testplatform.transport.real_transports")
    transport = build()

    transport.connect()

    assert transport.has_client() is True
    assert _ssh_handle(transport).exec_command("dmesg").stdout == "dmesg: ok\n"
    assert "10.0.0.9 的主机密钥未校验(未配置 known_hosts" in caplog.text
    assert caplog.text.count("未配置 known_hosts") == 1


@_TWO_SSH_PATHS
def test_a_matching_known_hosts_entry_connects_and_logs_the_verified_fingerprint(
    monkeypatch, tmp_path, caplog, build
):
    """配置且匹配:功能仍在(连上、能跑命令),并留下“已校验”的痕迹。

    痕迹是指纹本身:日志里出现的就是已知能对上的那个值 —— “校验跑过了”而不是“看起来正常”。
    """
    known_hosts = _write_known_hosts(tmp_path, "10.0.0.9", _SERVER_KEY)
    _inject_paramiko(monkeypatch, _FakeSshHost(b"ok\n"))
    caplog.set_level(logging.INFO, logger="testplatform.transport.real_transports")
    transport = build(known_hosts)

    transport.connect()

    assert transport.has_client() is True
    assert _ssh_handle(transport).exec_command("uptime").stdout == "ok\n"
    assert f"已校验: 10.0.0.9 {_SERVER_KEY.get_name()} {_SERVER_KEY.get_base64()}" in caplog.text
    assert "未配置 known_hosts" not in caplog.text  # 配了就不打“未校验”提示


@_TWO_SSH_PATHS
def test_a_host_key_that_disagrees_with_known_hosts_is_rejected_with_the_way_to_register(
    monkeypatch, tmp_path, build
):
    """配置且不匹配:拒绝,并告诉人怎么登记。只说“拒绝”等于让人卡死在那里。"""
    known_hosts = _write_known_hosts(tmp_path, "10.0.0.9", _OTHER_KEY)
    _inject_paramiko(monkeypatch, _FakeSshHost(b""))
    transport = build(known_hosts)

    with pytest.raises(ConnectionError) as caught:
        transport.connect()

    message = str(caught.value)
    assert f"期望: ['{_OTHER_KEY.get_name()} {_OTHER_KEY.get_base64()}']" in message
    assert f"实际: {_SERVER_KEY.get_name()} {_SERVER_KEY.get_base64()}" in message
    assert f"ssh-keyscan -p 22 10.0.0.9 >> {known_hosts}" in message


@_TWO_SSH_PATHS
def test_a_host_missing_from_known_hosts_is_rejected_as_unregistered(monkeypatch, tmp_path, build):
    """known_hosts 里没这台主机,与“指纹不对”分开说:一个是没登记,一个是登记的是别人。"""
    known_hosts = _write_known_hosts(tmp_path, "10.0.0.99", _OTHER_KEY)
    _inject_paramiko(monkeypatch, _FakeSshHost(b""))
    transport = build(known_hosts)

    with pytest.raises(ConnectionError) as caught:
        transport.connect()

    assert "该主机未登记任何密钥" in str(caught.value)


def test_a_known_hosts_file_that_vanished_says_so_instead_of_auth_failure(monkeypatch, tmp_path):
    """装配后才被删(或清单拿到另一台机器上):报“读不到 known_hosts”,不是“认证失败”。"""
    _inject_paramiko(monkeypatch, _FakeSshHost(b""))
    missing = str(tmp_path / "gone_known_hosts")

    with pytest.raises(ConnectionError) as caught:
        _direct_ssh(missing).connect()

    assert f"known_hosts 读不到: {missing}" in str(caught.value)


def test_the_gateway_transport_assembled_from_the_manifest_reads_gateway_known_hosts(tmp_path):
    """清单里的 gateway_known_hosts 必须真的被装配读到:一个“配置写了但没人读”的键,
    比没有这个键更坏 —— 它让人以为校验生效了。"""
    path = tmp_path / "known_hosts"
    path.write_text("", encoding="utf-8")

    transport = build_transport(
        "gateway_ssh",
        "real",
        {
            "gateway_host": "10.0.0.5",
            "gateway_user": "test",
            "local_bus": "can0",
            "gateway_known_hosts": str(path),
        },
    )

    assert transport._known_hosts == str(path)




# ------------------------------------------------ python-can / paho-mqtt 假库 ----

class _FakeCanMessage:
    def __init__(self, arbitration_id: int, data: bytes, is_extended_id: bool) -> None:
        self.arbitration_id = arbitration_id
        self.data = data
        self.is_extended_id = is_extended_id


class _FakeCanBus:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.sent = []
        self.shutdown_called = False

    def send(self, msg) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None):
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True


def _inject_can(monkeypatch) -> list[_FakeCanBus]:
    """假 python-can:返回造出来的 Bus 实例列表,用例断言 Bus 拿到的接口类型。"""
    created: list[_FakeCanBus] = []

    def Bus(**kwargs) -> _FakeCanBus:
        bus = _FakeCanBus(**kwargs)
        created.append(bus)
        return bus

    module = types.ModuleType("can")
    module.interface = types.SimpleNamespace(Bus=Bus)
    module.Message = _FakeCanMessage
    monkeypatch.setitem(sys.modules, "can", module)
    return created


class _FakeMqttClient:
    """假 paho:connect() 只建 TCP(返回 0),broker 认不认这把连接由 CONNACK 回调交出来。

    connack_rc=None 表示 broker 一句 CONNACK 都不回(黑洞 broker):那一条路径要等超时。
    """

    def __init__(self) -> None:
        self.broker = None
        self.looping = False
        self.published = []
        self.subscribed = []
        self.on_message = None
        self.on_connect = None
        self.connack_rc = 0

    def connect(self, broker: str, port: int) -> int:
        self.broker = (broker, port)
        if self.on_connect is not None and self.connack_rc is not None:
            # paho 的 on_connect 签名:1.x 四个参数,2.x 五个。这里按 1.x 调。
            self.on_connect(self, None, {}, self.connack_rc)
        return 0

    def loop_start(self) -> None:
        self.looping = True

    def loop_stop(self) -> None:
        self.looping = False

    def disconnect(self) -> None:
        self.broker = None

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.published.append((topic, payload, qos))

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)


def _inject_mqtt(monkeypatch, connack_rc: int | None = 0) -> list[_FakeMqttClient]:
    created: list[_FakeMqttClient] = []

    def Client() -> _FakeMqttClient:
        client = _FakeMqttClient()
        client.connack_rc = connack_rc
        created.append(client)
        return client

    module = types.ModuleType("paho.mqtt.client")
    module.Client = Client
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", module)
    return created


# --------------------- has_client:只说"句柄还在",不冒充链路健康 ---------------------

class _ResettingSocket(_FakeSocket):
    """对端发来 RST:recv 直接抛异常,本端的 socket 对象照样还在。"""

    def recv(self, size: int) -> bytes:
        raise ConnectionResetError("对端发来 RST")


def test_tcp_has_client_does_not_claim_a_live_link_after_the_peer_resets(monkeypatch):
    """拔线/RST 之后 has_client() 仍是 True:它是"有客户端",不是"链路活着"。

    所以要判断要不要重连,只能看 receive 抛出的异常 —— 这个名字故意不叫 is_connected。
    """
    monkeypatch.setattr(
        real_transports.socket, "create_connection", lambda *args, **kwargs: _ResettingSocket()
    )
    transport = RealTcpTransport(host="10.0.0.5", port=502)
    transport.connect()

    with pytest.raises(ConnectionResetError):
        transport.receive(timeout=0.2, expected=9)

    assert transport.has_client() is True


def test_serial_has_client_means_the_handle_exists_not_that_the_port_still_does(monkeypatch):
    """USB 转串口被拔掉后 pyserial 的对象还在(is_open 也可能是 True):has_client 只看句柄。"""
    port = _FakeSerialPort(b"")
    port.is_open = False
    _inject_serial(monkeypatch, port)
    transport = RealSerialTransport(port="COM3")
    transport.connect()

    assert transport.has_client() is True


def test_has_client_is_false_before_connect_and_after_disconnect(monkeypatch):
    """带连接生命周期的五个类:句柄有/无的边界必须一致。"""
    _inject_serial(monkeypatch, _FakeSerialPort(b""))
    _inject_paramiko(monkeypatch, _FakeSshHost(b""))
    _inject_can(monkeypatch)
    _inject_mqtt(monkeypatch)
    monkeypatch.setattr(
        real_transports.socket, "create_connection", lambda *args, **kwargs: _FakeSocket()
    )

    transports = [
        RealSerialTransport(port="COM3"),
        RealTcpTransport(host="10.0.0.5", port=502),
        RealCanTransport(channel="can0", interface="pcan"),
        RealSshTransport(host="10.0.0.9", user="root"),
        RealMqttTransport(broker="10.0.0.7"),
    ]
    for transport in transports:
        assert transport.has_client() is False
        transport.connect()
        assert transport.has_client() is True
        transport.disconnect()
        assert transport.has_client() is False


# ------------------------ 六类真机传输与各自 Protocol 的形状一致 ------------------------

def _six_real_transports():
    return [
        (RealSerialTransport(port="COM3"), ByteStreamTransport),
        (RealTcpTransport(host="10.0.0.5", port=502), ByteStreamTransport),
        (RealCanTransport(channel="can0", interface="pcan"), CanTransport),
        (RealSshTransport(host="10.0.0.9", user="root"), SshTransport),
        (RealHttpTransport(base_url="http://10.0.0.5:8080"), HttpTransport),
        (RealMqttTransport(broker="10.0.0.7"), MqttTransport),
    ]


@pytest.mark.parametrize(
    ("transport", "protocol"),
    _six_real_transports(),
    ids=[type(transport).__name__ for transport, _ in _six_real_transports()],
)
def test_each_real_transport_matches_its_protocol_shape(transport, protocol):
    """形状就是消费者(驱动、证据层)眼里的契约:少一个方法就装不进组合根。"""
    assert isinstance(transport, protocol)


def test_simulators_and_the_gateway_match_the_real_transports_shape():
    """真机与模拟器、网关透传必须同一个形状(含 has_client):换 mode 或包证据层才不会炸。"""
    aligned = [
        (simulators.SimulatedByteStreamTransport(), ByteStreamTransport),
        (simulators.SimulatedModbusSlave({0: 1}), ByteStreamTransport),
        (simulators.SimulatedDlt645Bus({}), ByteStreamTransport),
        (GatewaySshTransport("10.0.0.5", "test", "can0"), ByteStreamTransport),
        (simulators.SimulatedCanTransport(), CanTransport),
        (simulators.SimulatedSshTransport(), SshTransport),
        (simulators.SimulatedHttpTransport(), HttpTransport),
        (simulators.SimulatedMqttTransport(), MqttTransport),
    ]
    for transport, protocol in aligned:
        assert isinstance(transport, protocol), (
            f"{type(transport).__name__} 不满足 {protocol.__name__}"
        )


# --------------------------- 构造参数:必填缺失 / 类型不对 ---------------------------

@pytest.mark.parametrize(
    ("build", "missing"),
    [
        (RealSerialTransport, "port"),
        (RealTcpTransport, "host"),
        (RealHttpTransport, "base_url"),
        (RealMqttTransport, "broker"),
        (lambda: RealCanTransport(channel="can0"), "interface"),
        (lambda: RealSshTransport(host="10.0.0.9"), "user"),
    ],
)
def test_a_missing_required_argument_names_the_field(build, missing):
    with pytest.raises(TypeError, match=missing):
        build()


@pytest.mark.parametrize(
    ("build", "where"),
    [
        (lambda: RealSerialTransport(port=""), "串口"),
        (lambda: RealSerialTransport(port="COM3", baudrate="9600"), "串口"),
        (lambda: RealSerialTransport(port="COM3", timeout=0), "串口"),
        (lambda: RealTcpTransport(host="", port=502), "TCP"),
        (lambda: RealTcpTransport(host="10.0.0.5", port="502"), "TCP"),
        (lambda: RealCanTransport(channel="can0", interface="pcan", bitrate=True), "CAN"),
        (lambda: RealSshTransport(host="10.0.0.9", user="root", key_path=3), "SSH"),
        (lambda: RealHttpTransport(base_url="   "), "HTTP"),
        (lambda: RealMqttTransport(broker=""), "MQTT"),
        (lambda: RealMqttTransport(broker="10.0.0.7", port=-1), "MQTT"),
    ],
)
def test_a_bad_argument_says_which_transport_and_which_field(build, where):
    """清单里的值写错(空串、数字写成字符串)要在构造时就说清是哪个类的哪个参数。"""
    with pytest.raises(ValueError) as caught:
        build()

    assert str(caught.value).startswith(f"{where}: "), str(caught.value)


# ------------------ 可选依赖没装:给安装指引,不是裸 ModuleNotFoundError ------------------

@pytest.mark.parametrize(
    ("module", "connect", "package", "extra"),
    [
        ("serial", lambda: RealSerialTransport(port="COM3").connect(), "pyserial", "serial"),
        (
            "paramiko",
            lambda: RealSshTransport(host="10.0.0.9", user="root").connect(),
            "paramiko",
            "ssh",
        ),
        (
            "can",
            lambda: RealCanTransport(channel="can0", interface="pcan").connect(),
            "python-can",
            "can",
        ),
        (
            "requests",
            lambda: RealHttpTransport(base_url="http://10.0.0.5:8080").request("GET", "/status"),
            "requests",
            "http",
        ),
        (
            "paho.mqtt.client",
            lambda: RealMqttTransport(broker="10.0.0.7").connect(),
            "paho-mqtt",
            "mqtt",
        ),
    ],
)
def test_a_missing_optional_dependency_says_what_to_install(
    monkeypatch, module, connect, package, extra
):
    """把 sys.modules 里的库置 None 等于"这台机器没装":报错要能直接照着装。"""
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(ModuleNotFoundError) as caught:
        connect()

    message = str(caught.value)
    assert package in message, message
    assert f"pip install testplatform[{extra}]" in message, message


def test_tcp_needs_no_optional_dependency(monkeypatch):
    """TCP 用标准库 socket:六个类里只有它没有"缺依赖"这一条路径。"""
    monkeypatch.setattr(
        real_transports.socket, "create_connection", lambda *args, **kwargs: _FakeSocket()
    )

    RealTcpTransport(host="10.0.0.5", port=502).connect()


# ------------------ CAN 接口类型:进清单、按平台缺省、装配前校验未知值 ------------------

def test_can_bus_uses_the_interface_from_the_connection(monkeypatch):
    """socketcan 只在 Linux 上有:接口类型必须能从清单换成 pcan / vector / slcan。"""
    buses = _inject_can(monkeypatch)

    build_transport(
        "can", "real", {"channel": "can0", "interface": "pcan", "bitrate": 250_000}
    ).connect()

    assert buses[-1].kwargs == {"channel": "can0", "bustype": "pcan", "bitrate": 250_000}


def test_can_bus_falls_back_to_the_interface_default_of_this_platform(monkeypatch):
    buses = _inject_can(monkeypatch)

    build_transport("can", "real", {"channel": "can0"}).connect()

    assert buses[-1].kwargs["bustype"] == default_can_interface()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("linux", "socketcan"), ("win32", "pcan"), ("darwin", "slcan")],
)
def test_the_can_interface_default_is_sane_per_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(real_transports.sys, "platform", platform)

    assert default_can_interface() == expected
    assert expected in CAN_INTERFACES  # 缺省值必须是清单能接受的值


_CAN_MANIFEST = {
    "models": {
        "bms_can": {
            "type": "battery",
            "protocol": "canopen",
            "capabilities": ["SOC"],
            "point_table": {"SOC": {"address": 0, "length": 6, "encoding": "ascii"}},
        }
    },
    "devices": [{"id": "bat_can", "model": "bms_can", "access": "can"}],
}


def _can_manifest(**connection) -> dict:
    manifest = copy.deepcopy(_CAN_MANIFEST)
    manifest["devices"][0]["connection"] = connection
    return manifest


def test_an_unknown_can_interface_fails_before_assembly():
    """不等 python-can 抛 "Interface 'socket' not found":装配前就报错并列出可用值。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_can_manifest(channel="can0", interface="socket"))

    message = str(caught.value)
    assert "bat_can" in message
    assert "socket" in message
    for available in CAN_INTERFACES:
        assert available in message, message


def test_a_known_can_interface_passes_the_can_connection_check():
    """合法的接口类型不能被清单拒绝:越过这一步,报错只能落在下一层(驱动 / 工厂)。"""
    for interface in CAN_INTERFACES:
        parse_manifest(_can_manifest(channel="can0", interface=interface))


def test_a_typo_in_a_can_connection_is_rejected_before_assembly():
    """键拼错(chanel)静默取缺省就是下一个假通过:装配前拒绝,并列出可用字段。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_can_manifest(chanel="can0"))

    message = str(caught.value)
    assert "chanel" in message
    assert "channel" in message


def test_a_can_device_without_an_interface_uses_the_platform_default(monkeypatch):
    """不写 interface 是正常配置:清单放行,接口类型由平台在装配时补齐。"""
    buses = _inject_can(monkeypatch)

    manifest = parse_manifest(_can_manifest(channel="can0"))
    spec = manifest.devices[0]
    build_transport(spec["access"], "real", spec["connection"]).connect()

    assert buses[-1].kwargs["bustype"] == default_can_interface()


def test_a_can_model_only_accepts_access_can():
    """规约 × 接入方式是双向的:canopen 放行 can,但也只放行 can。"""
    manifest = copy.deepcopy(_CAN_MANIFEST)
    manifest["devices"][0]["access"] = "tcp"
    manifest["devices"][0]["connection"] = {"host": "10.0.0.5", "port": 502}

    with pytest.raises(ValueError) as caught:
        parse_manifest(manifest)

    assert "canopen" in str(caught.value)
    assert "can" in str(caught.value)


def test_a_valid_can_manifest_gets_past_the_manifest_and_stops_at_the_missing_factory(
    tmp_path,
):
    """清单层放行的证据:下一层错在"没有 canopen 的设备工厂",不是"不能配 can"。

    这台设备暂时没有驱动,所以装配在这里停下才是对的 —— 不要为了让它跑起来造一个假驱动。
    """
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(_can_manifest(channel="can0", interface="pcan")), encoding="utf-8")

    with pytest.raises(KeyError) as caught:
        CompositionRoot.build(str(path))

    message = str(caught.value)
    assert "未找到设备工厂" in message
    assert "canopen" in message


_HTTP_MQTT_SSH_MANIFEST = {
    "models": {
        "ems_rest": {
            "type": "ems",
            "protocol": "vendor_ascii",
            "capabilities": ["SOC"],
            "point_table": {"SOC": {"address": 0, "length": 6, "encoding": "ascii"}},
        }
    },
    "devices": [{"id": "ems_01", "model": "ems_rest", "access": "http"}],
}


@pytest.mark.parametrize("access", ["http", "mqtt", "ssh"])
def test_http_mqtt_ssh_are_rejected_with_a_reason_the_reader_can_act_on(access):
    """http / mqtt / ssh 的传输已实现并注册,但今天没有任何规约声明支持它们,所以清单里写就是被拒。

    拒绝对;只说"不能配这种接入"错:读的人会以为传输没写,于是去重复实现一遍。
    断言因此落在消息上 —— 它要说"传输已实现 / 没有规约声明支持 / 下一步先定规约再放行"。
    将来真放行时改这条用例;不要把它写成"这三种接入永远不能被放行"。
    """
    manifest = copy.deepcopy(_HTTP_MQTT_SSH_MANIFEST)
    manifest["devices"][0]["access"] = access

    with pytest.raises(ValueError) as caught:
        parse_manifest(manifest)

    message = str(caught.value)
    assert access in message and "已经实现并注册" in message  # 不是"没写",是"没规约用它"
    assert "还没有任何规约声明支持它" in message  # 为什么被拒
    assert "PROTOCOL_ACCESS" in message  # 下一步做什么
    assert (access, "real") in registered_keys(), message  # "已实现"不是嘴上说的


def test_the_policy_handed_to_paramiko_is_the_verifying_one(monkeypatch):
    """断言"装进去的策略是哪个"，而不只是"某处做了校验"。

    假 paramiko 不会自己校验主机密钥 —— 如果实现把 policy 设成别的东西、再把校验写在
    别处，上面三态用例仍可能全绿（测的是我们的调用，不是 paramiko 的行为）。
    这里直接把交给 `set_missing_host_key_policy` 的策略类型钉住。
    """
    from testplatform.transport.real_transports import _VerifyHostKeyPolicy

    host = _FakeSshHost("dmesg: ok")
    _inject_paramiko(monkeypatch, host)
    transport = RealSshTransport(host="10.0.0.5", user="root")  # 未配置 known_hosts:仍装校验策略,只是不比对
    transport.connect()

    assert isinstance(transport._client.policy, _VerifyHostKeyPolicy), (
        f"交给 paramiko 的策略应当是 _VerifyHostKeyPolicy，实际 {type(transport._client.policy)!r}"
    )


# ------------------ MQTT:连不上必须在 connect() 就失败,不是等到读 ------------------

def test_mqtt_connect_fails_when_the_broker_refuses_the_connect(monkeypatch):
    """broker 回 CONNACK rc=5(未授权):TCP 通、CONNECT 被拒 —— 修之前 connect() 返回 None。

    修前实测:connect() 返回 None、has_client() True,订阅照登记、消息永不到,
    之后第一次读报 TimeoutError(证据层记 timeout → 读成"设备不回")。见票 14c。
    """
    _inject_mqtt(monkeypatch, connack_rc=5)
    transport = RealMqttTransport(broker="10.0.0.7", port=1883)

    with pytest.raises(MqttBrokerUnreachable) as caught:
        transport.connect()

    message = str(caught.value)
    assert "10.0.0.7:1883" in message, message  # 哪个 broker
    assert "rc=5" in message and "未授权" in message, message  # paho 的 rc 与含义
    assert "不是被测设备不合格" in message, message  # 归因写在消息里
    assert transport.has_client() is False, "connect() 失败后不许留下'已连上'的句柄"


def test_mqtt_connect_fails_within_the_connect_timeout_when_no_connack_arrives(monkeypatch):
    """黑洞 broker(TCP 通、一句 CONNACK 都不回):connect() 不许无限等。

    没有上界时它照样立刻返回"成功",问题被推迟到读,归因就错了方向。
    """
    _inject_mqtt(monkeypatch, connack_rc=None)
    transport = RealMqttTransport(broker="10.0.0.7", port=1883, connect_timeout=0.05)

    started = time.monotonic()
    with pytest.raises(MqttBrokerUnreachable) as caught:
        transport.connect()
    elapsed = time.monotonic() - started

    message = str(caught.value)
    assert "10.0.0.7:1883" in message and "CONNACK" in message, message
    assert elapsed < 2.0, f"connect_timeout=0.05 却等了 {elapsed:.2f} 秒:超时上界没生效"
    assert transport.has_client() is False


def test_mqtt_connect_fails_when_nothing_listens_on_the_broker_port(monkeypatch):
    """端口没人听(paho 抛 ConnectionRefusedError):报"连不上 broker",不是裸 socket 错。"""
    class _Refusing:
        on_connect = None

        def connect(self, broker, port):
            raise ConnectionRefusedError(10061, "由于目标计算机积极拒绝,无法连接")

        def loop_start(self):
            raise AssertionError("建连就失败了,不该再起 loop")

    module = types.ModuleType("paho.mqtt.client")
    module.Client = _Refusing
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", module)
    transport = RealMqttTransport(broker="10.0.0.7", port=1883)

    with pytest.raises(MqttBrokerUnreachable) as caught:
        transport.connect()

    message = str(caught.value)
    assert "10.0.0.7:1883" in message and "TCP 建连失败" in message, message
    assert transport.has_client() is False


def test_mqtt_unreachable_is_attributed_to_the_bench_not_to_the_device(monkeypatch):
    """"没连上 broker"必须落在台架侧:证据记断链(closed),本轮按"测试无效"跳过。

    如果它抛 TimeoutError,证据层会记 timeout —— 那一档可能被读成设备不回(判不合格),
    正是本票要断掉的错误归因。
    """
    from testplatform.reporting.evidence import _failure_kind, _round_note

    _inject_mqtt(monkeypatch, connack_rc=5)
    transport = RealMqttTransport(broker="10.0.0.7", port=1883)
    with pytest.raises(MqttBrokerUnreachable) as caught:
        transport.connect()

    error = caught.value
    assert _failure_kind(error) == "closed", _failure_kind(error)  # 断链,不是 timeout
    note = _round_note([{"operation": "connect"}])
    assert "测试无效" in note and "不是设备不合格" in note, note
