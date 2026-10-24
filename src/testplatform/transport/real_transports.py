"""
真实硬件/网络的 Transport 实现。

每个类只做两件事:
1. 满足 interfaces.py 里消费者定义的协议形状
2. 把协议特有的、丑陋的底层库 API 风格封装在内部,不让它泄漏出去

可选依赖(pyserial / paramiko / requests / paho-mqtt / python-can)
全部在方法内部 import,这样项目里没装某个库也完全不影响其他协议正常工作——
比如一个只测 HTTP 设备的项目,不需要安装 python-can。真缺了由 _load_optional
给安装指引,不是裸 ModuleNotFoundError。

has_client() 只回答"底层客户端/句柄对象还在吗",不代表链路健康(语义见 interfaces.py)。
"""
from __future__ import annotations

import importlib
import logging
import os
import socket
import sys
import threading
import time
from typing import Any, Callable

from .interfaces import (
    ByteStreamTransport,
    CanTransport,
    FrameLength,
    HttpTransport,
    IncompleteFrameError,
    MqttTransport,
    SshTransport,
    expected_bytes,
)
from .registry import register_transport

# 长度未知(expected=None)时,一帧最多能收多长:顶到上限就报错,绝不静默截断。
_UNKNOWN_FRAME_LIMIT = 64 * 1024

# 长度未知时"多长的静默算这一轮读完了"。它是读取参数,不是帧边界:慢设备(字节间隔比它大)
# 会提前收尾,那是参数没调对,由调用方调大,所以它显式出现在 receive() 的签名上而不是藏在函数里。
_DEFAULT_READ_GAP_MS = 50.0


def _load_optional(module: str, package: str, extra: str):
    """可选依赖在方法里 import:没装某个库不影响其它协议正常工作。

    缺了要给安装指引:裸 ModuleNotFoundError("No module named 'serial'") 到现场
    只说明这台机器上没有,说不出该装什么。
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ModuleNotFoundError(
            f"缺少可选依赖 {package}:请安装 {package}:`pip install testplatform[{extra}]`"
        ) from exc


def _text(value: Any, where: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {name} 必须是非空字符串,拿到 {value!r}")
    return value


def _optional_text(value: Any, where: str, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{where}: {name} 必须是字符串或留空,拿到 {value!r}")
    return value


def _positive_int(value: Any, where: str, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{where}: {name} 必须是正整数,拿到 {value!r}")
    return value


def _positive_number(value: Any, where: str, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{where}: {name} 必须是正数,拿到 {value!r}")
    return value


def _non_negative_number(value: Any, where: str, name: str) -> float:
    """0 是合法值(表示"不等待"):帧间隔这类可关的延迟不能用 _positive_number。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}: {name} 必须是非负数(0 表示不等待),拿到 {value!r}")
    return float(value)


def default_can_interface() -> str:
    """没配 connection.interface 时用哪种 CAN 接口,按平台给合理缺省。

    socketcan 是 Linux 内核专有:把它写死就会让 Windows 上接了 CAN 设备直接起不来。
    Windows 上接 CAN 设备最常见的是 PEAK 的 pcan;其余平台默认 slcan(USB 转 CAN 串口)。
    可用值的白名单在 config/manifest.py 的 CAN_INTERFACES,装配前校验未知值。
    """
    if sys.platform.startswith("linux"):
        return "socketcan"
    if sys.platform == "win32":
        return "pcan"
    return "slcan"


def _receive_until_quiet(
    read_once: Callable[[int, float], bytes],
    timeout: float,
    gap: float,
    read_gap_ms: float,
    what: str,
) -> bytes:
    """长度未知:读到"这一轮还能读到的全部" —— 每次读之间等 gap,连续静默即认为这一轮结束。

    只承诺**不截断**(TCP 一次 recv 拿到的可能只是半个帧,一次 recv 就返回就是截断),
    **不承诺帧完整**:帧完整性由知道规约的上层解出来判,这里不认识 CRC、长度、任何规约。
    shortcut: 对端字节间隔大于 gap 时这一轮会提前收尾,调用方调大 read_gap_ms;
    总时长 timeout 用完仍在收字节则报错,绝不把半帧当一帧交上去。
    """
    deadline = time.monotonic() + timeout
    buffer = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IncompleteFrameError(
                f"{what}: 已读 {len(buffer)} 字节,read_gap_ms={read_gap_ms},timeout={timeout} 秒耗尽时"
                f"对端还在发字节 —— 不把半帧当完整一帧交给上层;对端可能发得慢,调大 timeout"
            )
        # 第一个字节还没到时按剩余总时长等(设备可能就是慢半拍才开口);
        # 已经开读之后,静默 gap 就算这一轮结束。
        idle = gap if buffer else remaining
        try:
            chunk = read_once(_UNKNOWN_FRAME_LIMIT - len(buffer), min(idle, remaining))
        except TimeoutError:
            chunk = b""
        if not chunk:
            if buffer:
                return buffer
            raise IncompleteFrameError(
                f"{what}: {timeout} 秒内一个字节都没收到(read_gap_ms={read_gap_ms})"
            ) from None
        buffer += chunk
        if len(buffer) >= _UNKNOWN_FRAME_LIMIT:
            raise ValueError(
                f"{what}: 长度未知的一帧顶到 {_UNKNOWN_FRAME_LIMIT} 字节上限,拒绝静默截断"
            )


def _receive_frame(
    read_once: Callable[[int, float], bytes],
    timeout: float,
    expected: FrameLength,
    what: str,
    read_gap_ms: float = _DEFAULT_READ_GAP_MS,
) -> bytes:
    """短读继续读,直到够 expected 或超时;超时还差字节就抛 IncompleteFrameError。

    半帧绝不交给上层:半帧要么解析失败,要么解出一个看着正常的假读数,后者现场最难查。
    read_once(size, timeout) 是一次底层读(串口 read / socket recv),它可能只给出半帧。
    底层抛出的异常原样上抛:链路没了(ConnectionError 一类)不是超时,两种现场处置完全不同 ——
    超时可能是设备问题(判不合格),断链是台架问题(判测试无效),不在这里合并、也不包成超时。
    """
    if expected is None:
        # gap=0 就是"一次 recv",正是本票要修的那个 bug;所以它是配置错误,不是爽直取值。
        gap = _positive_number(read_gap_ms, what, "read_gap_ms") / 1000.0
        if gap > timeout:
            # gap 比总时长还长 = gap 永远等不到,读取会退化成一次 recv(就是本票要修的那个 bug)。
            # 在开读之前就报:配错了要在配置期看到,不是跑起来看到一个诡异的结果。
            raise ValueError(
                f"{what}: read_gap_ms={read_gap_ms} 大于 timeout={timeout} 秒,gap 永远等不到,"
                f"读取会退化成一次 recv;调小 read_gap_ms 或调大 timeout"
            )
        return _receive_until_quiet(read_once, timeout, gap, read_gap_ms, what)

    deadline = time.monotonic() + timeout
    buffer = b""
    while True:
        total = expected_bytes(expected, buffer)
        if total is not None and len(buffer) >= total:
            return buffer
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # 知道整帧多长就只读还差的那点;头还不够算长度时一次只读 1 字节,
        # 否则底层读会为了凑满请求数干等到超时,每一帧都白等一个 timeout。
        want = total - len(buffer) if total is not None else 1
        try:
            chunk = read_once(want, remaining)
        except TimeoutError:
            # 底层读等满了剩余时间(真实 socket 静默时抛 socket.timeout,不返回 b""):
            # 收帧这一层统一按 interfaces.py 里写明的契约收成 IncompleteFrameError,证据仍记 timeout。
            break
        if not chunk:
            break  # 串口没有 EOF 语义:read 超时返回 b"",见 RealSerialTransport.read_once
        buffer += chunk

    total = expected_bytes(expected, buffer)
    raise IncompleteFrameError(
        f"{what}: {timeout} 秒内没收满一帧(收到 {len(buffer)} 字节,"
        f"整帧需要 {total if total is not None else '未知'} 字节),半帧不交给上层: {buffer.hex()}"
    )


# ---------------------------------------------------------------- 串口 ----

class RealSerialTransport:
    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 2.0,
        inter_frame_delay_ms: float = 0.0,
    ) -> None:
        self._port = _text(port, "串口", "port")
        self._baudrate = _positive_int(baudrate, "串口", "baudrate")
        self._timeout = _positive_number(timeout, "串口", "timeout")
        # 清单里按毫秒写(9600 下 3.5 字符 ≈ 4ms 是常见取值的量级),内部统一按秒算。
        self._inter_frame_delay = _non_negative_number(
            inter_frame_delay_ms, "串口", "inter_frame_delay_ms"
        ) / 1000.0
        self._last_send_at: float | None = None
        self._ser = None

    def connect(self) -> None:
        serial = _load_optional("serial", "pyserial", "serial")

        self._ser = serial.Serial(self._port, self._baudrate, timeout=self._timeout)

    def disconnect(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def has_client(self) -> bool:
        # 不看 is_open:USB 转串口被拔掉之后它照样是 True,一样不代表链路健康。
        return self._ser is not None

    def send(self, data: bytes) -> None:
        self._wait_for_frame_gap()
        # 清输入缓冲放在 send 之前,不放 receive 之前:上一帧的残留(从站多发的一个字节、
        # 上一次超时后迟到的应答)必须在新请求发出前清掉,否则下一帧从头就错位 ——
        # 错位解出来的往往是"看着正常"的假读数,比 CRC 失败更难查。
        # 放 receive 之前则会丢掉刚好先到的应答(有的设备比平台的 read 先反应过来),那是老坑。
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._last_send_at = time.monotonic()

    def _wait_for_frame_gap(self) -> None:
        """Modbus RTU 要求帧间静默 ≥3.5 字符时间:背靠背发送时,严格的从站会把两帧粘成一帧。

        只在同一条连接(同一台 transport)的两次 send 之间算:同一条物理总线上挂着多台设备、
        各自建了自己的 transport 时,这里的计时看不到别的连接,间隔不会被跨连接强制(见 README)。
        缺省 0 时一次都不调 sleep —— 不给所有跑法平白加延迟。
        """
        if self._inter_frame_delay <= 0 or self._last_send_at is None:
            return
        remaining = self._inter_frame_delay - (time.monotonic() - self._last_send_at)
        if remaining > 0:
            time.sleep(remaining)

    def receive(
        self, timeout: float = 2.0, expected: FrameLength = None, read_gap_ms: float = _DEFAULT_READ_GAP_MS
    ) -> bytes:
        def read_once(size: int, remaining: float) -> bytes:
            # pyserial 的 timeout 是每次 read 的:按剩余时间切开,短读才能继续读。
            # 串口没有 EOF 语义:read 超时就返回 b"",拔线则是 SerialException —— 硬件层面
            # 没有"对端正常关闭"这个信号,所以串口只能把断链和沉默都记成超时。不是实现偷懒。
            self._ser.timeout = remaining
            return self._ser.read(size)

        return _receive_frame(read_once, timeout, expected, f"串口 {self._port}", read_gap_ms)


@register_transport("serial", "real")
def _build_serial(cfg: dict) -> ByteStreamTransport:
    return RealSerialTransport(
        port=cfg["port"],
        baudrate=cfg.get("baudrate", 9600),
        inter_frame_delay_ms=cfg.get("inter_frame_delay_ms", 0),
    )


# ------------------------------------------------------------- TCP/IP ----

class RealTcpTransport:
    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self._host = _text(host, "TCP", "host")
        self._port = _positive_int(port, "TCP", "port")
        self._timeout = _positive_number(timeout, "TCP", "timeout")
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        self._sock = socket.create_connection((self._host, self._port), timeout=self._timeout)

    def disconnect(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def has_client(self) -> bool:
        return self._sock is not None

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def receive(
        self, timeout: float = 2.0, expected: FrameLength = None, read_gap_ms: float = _DEFAULT_READ_GAP_MS
    ) -> bytes:
        def read_once(size: int, remaining: float) -> bytes:
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(size)
            if not chunk:
                # recv 返回 b"" 只有一个含义:对端正常关闭(FIN)。超时是 settimeout 抛 socket.timeout,
                # 不是返回空 —— 把两者混成一条超时,现场就分不清是台架断了还是设备不合格。
                raise ConnectionError(f"TCP {self._host}:{self._port} 对端已关闭连接")
            return chunk

        return _receive_frame(
            read_once, timeout, expected, f"TCP {self._host}:{self._port}", read_gap_ms
        )


@register_transport("tcp", "real")
def _build_tcp(cfg: dict) -> ByteStreamTransport:
    return RealTcpTransport(host=cfg["host"], port=cfg["port"])


# ----------------------------------------------------------------- CAN ----

class RealCanTransport:
    def __init__(self, channel: str, interface: str, bitrate: int = 500_000) -> None:
        self._channel = _text(channel, "CAN", "channel")
        self._interface = _text(interface, "CAN", "interface")
        self._bitrate = _positive_int(bitrate, "CAN", "bitrate")
        self._bus = None

    def connect(self) -> None:
        can = _load_optional("can", "python-can", "can")

        self._bus = can.interface.Bus(
            channel=self._channel, bustype=self._interface, bitrate=self._bitrate
        )

    def disconnect(self) -> None:
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None

    def has_client(self) -> bool:
        return self._bus is not None

    def send_frame(self, arbitration_id: int, data: bytes) -> None:
        can = _load_optional("can", "python-can", "can")

        msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
        self._bus.send(msg)

    def receive_frame(self, timeout: float = 2.0):
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            raise TimeoutError("CAN 接收超时")
        return msg.arbitration_id, bytes(msg.data)


@register_transport("can", "real")
def _build_can(cfg: dict) -> CanTransport:
    return RealCanTransport(
        channel=cfg["channel"],
        interface=cfg.get("interface", default_can_interface()),
        bitrate=cfg.get("bitrate", 500_000),
    )


# ----------------------------------------------------------------- SSH ----

class _SshResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


logger = logging.getLogger(__name__)


def _fingerprint(key) -> str:
    """主机密钥指纹写成 "算法 base64",与 known_hosts / ssh-keyscan 输出逐字同格式:
    报错里的期望值可以直接贴进 known_hosts。"""
    return f"{key.get_name()} {key.get_base64()}"


def _known_host_fingerprints(paramiko, known_hosts: str, hostname: str) -> list[str]:
    """known_hosts 里这个主机已登记的指纹。用 paramiko.HostKeys 读,不自己解析:
    HashKnownHosts 默认开启的主机上每条记录是 |1|salt|hash,手写解析一条也匹配不上。"""
    try:
        keys = paramiko.HostKeys(filename=known_hosts)
    except OSError as exc:
        # 装配期只查“路径存在”,文件在这之后被删/被换机器仍是可能:说清是读不到,不是密码错。
        raise ConnectionError(f"known_hosts 读不到: {known_hosts}({exc}),主机密钥无法校验") from exc
    # lookup 找不到时老版本 paramiko 给 None,新版本给 {},两种都要当成“未登记”。
    return [_fingerprint(key) for key in (keys.lookup(hostname) or {}).values()]


class _VerifyHostKeyPolicy:
    """配置了 known_hosts 才比对主机密钥;未配置时与修复前完全一致(接受任意主机密钥)。

    比对放进 missing_host_key 钩子,而不是 connect() 之后:paramiko 每次建连只调它一次,
    超时重连的路径也必经它。写在 connect() 里紧跟 exec_command 的路径上会变成每条命令
    重算一次指纹,而且重连/超时能绕过。
    """

    def __init__(self, paramiko, known_hosts: str | None) -> None:
        self._paramiko = paramiko
        self._known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key) -> None:
        if self._known_hosts is None:
            # 未配置 = 不校验主机密钥(README「SSH 主机密钥」一节写明)。默认路径最像“已经安全”,
            # 所以留一行痕迹:半年后看到“我们有主机密钥校验”时,能看出这台到底配没配。
            logger.warning(
                "注意: %s 的主机密钥未校验(未配置 known_hosts,接受任意主机密钥;"
                "配置办法见 README「SSH 主机密钥:未配置 = 不校验」)",
                hostname,
            )
            return
        actual = _fingerprint(key)
        expected = _known_host_fingerprints(self._paramiko, self._known_hosts, hostname)
        if actual in expected:
            logger.info("SSH 主机密钥已校验: %s %s", hostname, actual)
            return
        raise ConnectionError(self._mismatch(hostname, actual, expected))

    def _mismatch(self, hostname: str, actual: str, expected: list[str]) -> str:
        host, port = _host_and_port(hostname)
        return (
            f"主机密钥与 {self._known_hosts} 不符({hostname}):\n"
            f"  期望: {expected or '该主机未登记任何密钥'}\n"
            f"  实际: {actual}\n"
            f"可能是网关被冒充,也可能只是换过密钥。人工确认后登记:"
            f" ssh-keyscan -p {port} {host} >> {self._known_hosts}"
        )


def _host_and_port(hostname: str) -> tuple[str, int]:
    """paramiko 传回来的名字:非 22 端口是 "[host]:port"。报错里的登记命令要拆开写。"""
    if hostname.startswith("[") and "]:" in hostname:
        host, _, port = hostname[1:].partition("]:")
        return host, int(port)
    return hostname, 22


# 清单不可达:没有规约在 PROTOCOL_ACCESS 里放行 access: ssh(见 config/manifest.py 的
# TRANSPORT_ONLY_ACCESS)。仍然有真实用途 —— gateway_transport.py 复用它做网关透传。
class RealSshTransport:
    """给工控机、PLC 主机这类"可以跑命令的设备"用,
    也被 gateway_transport.py 复用来实现网关透传。"""

    def __init__(
        self,
        host: str,
        user: str,
        key_path: str | None = None,
        password: str | None = None,
        known_hosts: str | None = None,
    ) -> None:
        self._host = _text(host, "SSH", "host")
        self._user = _text(user, "SSH", "user")
        self._key_path = _optional_text(key_path, "SSH", "key_path")
        self._password = _optional_text(password, "SSH", "password")
        self._known_hosts = _optional_text(known_hosts, "SSH", "known_hosts")
        self._client = None

    def connect(self) -> None:
        paramiko = _load_optional("paramiko", "paramiko", "ssh")

        # 路径在这里展开 ~ :装配期校验用的是展开后的路径,两处必须看同一个文件。
        known_hosts = None if self._known_hosts is None else os.path.expanduser(self._known_hosts)
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(_VerifyHostKeyPolicy(paramiko, known_hosts))
        self._client.connect(
            self._host,
            username=self._user,
            key_filename=self._key_path,
            password=self._password,
        )

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def has_client(self) -> bool:
        return self._client is not None

    def exec_command(self, command: str, timeout: float = 10.0):
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        # 先把 stdout/stderr 读完,再取退出码:命令的输出超过 SSH 通道窗口(通常 ~64KB)时,
        # 远端卡在 write 上不退出,先等退出码会与它互相等死(现场表现是"命令不返回")。
        out, err = stdout.read(), stderr.read()
        exit_code = stdout.channel.recv_exit_status()
        return _SshResult(exit_code, out.decode(), err.decode())


# 已注册但清单不可达:access: ssh 会被 parse_manifest 拒绝(没有规约声明支持它),
# 原因与放行办法见 config/manifest.py 的 TRANSPORT_ONLY_ACCESS。
@register_transport("ssh", "real")
def _build_ssh(cfg: dict) -> SshTransport:
    return RealSshTransport(
        host=cfg["host"],
        user=cfg["user"],
        key_path=cfg.get("key_path"),
        password=cfg.get("password"),
        known_hosts=cfg.get("known_hosts"),
    )


# ---------------------------------------------------------------- HTTP ----

class RealHttpTransport:
    # 清单不可达:没有规约在 PROTOCOL_ACCESS 里放行 access: http(见 config/manifest.py 的
    # TRANSPORT_ONLY_ACCESS)。有意保留:要给 EMS / 网关这类自带 REST 接口的设备用时,
    # 先定规约再放行,不是删掉这个实现。
    def __init__(self, base_url: str) -> None:
        self._base_url = _text(base_url, "HTTP", "base_url").rstrip("/")

    def request(self, method: str, path: str, json: dict | None = None, timeout: float = 5.0):
        requests = _load_optional("requests", "requests", "http")

        url = f"{self._base_url}/{path.lstrip('/')}"
        return requests.request(method, url, json=json, timeout=timeout)


# 已注册但清单不可达:access: http 会被 parse_manifest 拒绝(没有规约声明支持它),
# 原因与放行办法见 config/manifest.py 的 TRANSPORT_ONLY_ACCESS。
@register_transport("http", "real")
def _build_http(cfg: dict) -> HttpTransport:
    return RealHttpTransport(base_url=cfg["base_url"])


# ---------------------------------------------------------------- MQTT ----

class MqttBrokerUnreachable(ConnectionError):
    """MQTT broker 连不上或拒绝 CONNECT:平台的接入通路不在,不是被测设备不合格。

    继承 ConnectionError 而不是 TimeoutError:证据层按异常类型记种类(reporting/evidence.py
    的 _failure_kind),TimeoutError 会被读成"设备不回"(可能判不合格),ConnectionError 才是
    "断链"(台架侧,结论是测试无效)。归因不能靠消息文本,得靠类型。
    """


class RealMqttTransport:
    # 清单不可达:没有规约在 PROTOCOL_ACCESS 里放行 access: mqtt(见 config/manifest.py 的
    # TRANSPORT_ONLY_ACCESS)。有意保留:HEMS / 云侧走发布订阅上报时会用到。

    # paho on_connect 的 rc:0 = 成功,其余是 broker 拒绝 CONNECT 的理由(MQTT 3.1.1 CONNACK)。
    _CONNACK_REFUSED = {
        1: "broker 不支持这个协议版本",
        2: "broker 不接受这个客户端 id",
        3: "broker 不可用",
        4: "用户名或密码不对",
        5: "未授权",
    }

    def __init__(self, broker: str, port: int = 1883, connect_timeout: float = 5.0) -> None:
        self._broker = _text(broker, "MQTT", "broker")
        self._port = _positive_int(port, "MQTT", "port")
        # 连上之前也可能永远等不到 CONNACK(黑洞 broker):给上界,失败要报"连不上"而不是挂住。
        self._connect_timeout = _positive_number(connect_timeout, "MQTT", "connect_timeout")
        self._client = None

    def _unreachable(self, cause: str) -> MqttBrokerUnreachable:
        return MqttBrokerUnreachable(
            f"MQTT broker {self._broker}:{self._port} 连不上({cause}):"
            f"这是平台侧的接入通路不在,不是被测设备不合格;查 broker 是否在跑,以及地址/端口/账号"
        )

    def connect(self) -> None:
        mqtt = _load_optional("paho.mqtt.client", "paho-mqtt", "mqtt")

        client = mqtt.Client()
        connack: list[int] = []
        answered = threading.Event()

        def _on_connect(_client, _userdata, _flags, rc, properties=None) -> None:
            # paho 1.x 给 4 个参数、2.x 给 5 个:签名两者都要收,否则 2.x 上回调直接 TypeError。
            connack.append(rc)
            answered.set()

        client.on_connect = _on_connect
        try:
            rc = client.connect(self._broker, self._port)
        except OSError as exc:
            raise self._unreachable(f"TCP 建连失败: {exc}") from exc
        if rc != 0:
            raise self._unreachable(f"paho connect() 返回 {rc}")
        client.loop_start()
        # paho 的 connect() 只把 TCP 建起来:broker 认不认这把连接由 CONNACK 决定,
        # 而 CONNACK 一定晚于 loop_start。不看它就是"连不上也报可用",下游把没连上读成设备坏。
        if not answered.wait(self._connect_timeout):
            client.loop_stop()
            client.disconnect()
            raise self._unreachable(
                f"{self._connect_timeout} 秒内没收到 CONNACK(broker 没应答 CONNECT)"
            )
        if connack[0] != 0:
            client.loop_stop()
            client.disconnect()
            meaning = self._CONNACK_REFUSED.get(connack[0], "本平台没有这个返回码的说明,查 MQTT CONNACK")
            raise self._unreachable(f"broker 拒绝 CONNECT:rc={connack[0]}({meaning})")
        self._client = client

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def has_client(self) -> bool:
        return self._client is not None

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self._client.publish(topic, payload, qos=qos)

    def subscribe(self, topic: str, handler: Callable[[str, bytes], None]) -> None:
        def _on_message(client, userdata, msg) -> None:
            handler(msg.topic, msg.payload)

        self._client.on_message = _on_message
        self._client.subscribe(topic)


# 已注册但清单不可达:access: mqtt 会被 parse_manifest 拒绝(没有规约声明支持它),
# 原因与放行办法见 config/manifest.py 的 TRANSPORT_ONLY_ACCESS。
@register_transport("mqtt", "real")
def _build_mqtt(cfg: dict) -> MqttTransport:
    return RealMqttTransport(
        broker=cfg["broker"],
        port=cfg.get("port", 1883),
        connect_timeout=cfg.get("connect_timeout", 5.0),
    )
