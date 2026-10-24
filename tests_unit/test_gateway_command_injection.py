"""gateway_ssh 的 `local_bus` 不能把 shell 命令注入到网关主机上。

`local_bus` 是唯一从清单流进 shell 命令行的值(transport/gateway_transport.py 里拼
`bus-agent send/recv --bus ...`)。清单会被复制传播,供应商回一份 yaml 就可能带进
`can0; id > /tmp/pwned #`,而平台在网关主机上以 gateway_user(常是工程账号甚至 root)执行它。

两道防线各锁一遍:
- 装配期白名单:含 `;` / `|` / `$()` / 反引号 / 空格 / 换行的值直接报错,消息带设备 id、
  字段名、当前值和允许的字符集(配置错要能直接改)。
- 拼命令时 shlex.quote:就算有形态混过白名单,也只会当成一个字符串交给 bus-agent。
  / 合法值同时也必须让命令串与修复前逐字节相同:调用约定一变,真网关上的 bus-agent 可能
  就不认,那种失败要到真机才出现。
"""
import base64
import shlex

import pytest

from testplatform.config.manifest import (
    GATEWAY_LOCAL_BUS_MAX_LENGTH,
    parse_manifest,
)
from testplatform.transport.gateway_transport import GatewaySshTransport

_MODEL = {
    "type": "inverter",
    "protocol": "vendor_ascii",
    "capabilities": ["SOC"],
    "point_table": {"SOC": {"address": 0, "length": 6, "encoding": "ascii"}},
}

_INJECTIONS = [
    "can0; id > /tmp/pwned #",
    "can0`id`",
    "can0$(id)",
    "can0|id",
    "can0 id",
    "can0\nid",
    "can0\n",
    "can0 && id",
    "can0'id",
    'can0"id',
    "can0>out",
]

# 参数注入:字符集全部通过,shlex.quote 也挡不住 —— 得靠"禁前导 -"这一条。
OPTIONS_AS_VALUES = ["-x", "--data", "-", "--bus=can0"]

_LEGAL = [
    "can0",
    "vcan0",
    "/dev/ttyUSB0",
    "/dev/ttyS1",
    "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-0-if00-port0",
]


def _manifest(local_bus, mode: str = "real") -> dict:
    return {
        "models": {"inv_series": _MODEL},
        "devices": [
            {
                "id": "inv_01",
                "model": "inv_series",
                "access": "gateway_ssh",
                "mode": mode,
                "connection": {
                    "gateway_host": "10.0.0.5",
                    "gateway_user": "test",
                    "local_bus": local_bus,
                },
            }
        ],
    }


# --------------------------- 第一道防线:装配期白名单 ---------------------------


@pytest.mark.parametrize("mode", ["real", "simulator"])
@pytest.mark.parametrize("local_bus", _INJECTIONS + OPTIONS_AS_VALUES)
def test_a_non_bus_local_bus_is_rejected_before_assembly(local_bus, mode):
    """注入形态在装配期就拒,两种 mode 都拒:模拟台上写错的值等到切真机才露头。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest(local_bus, mode))

    message = str(caught.value)
    assert "inv_01" in message  # 是哪台设备
    assert "local_bus" in message  # 是哪个字段
    assert repr(local_bus) in message  # 当前值原样回显,不用去翻清单
    assert "A-Za-z0-9_.:/-" in message  # 允许的字符集
    assert "'-'" in message  # 前导 - 也不行,否则 bus-agent 会把它当选项
    assert str(GATEWAY_LOCAL_BUS_MAX_LENGTH) in message  # 最大长度


@pytest.mark.parametrize("local_bus", _LEGAL)
def test_a_bus_name_or_device_path_passes_assembly(local_bus):
    """总线名与设备节点路径都是合法值:/ 要留着(/dev/... 是 tty 的写法),by-id 路径超 32 字符。"""
    manifest = parse_manifest(_manifest(local_bus))

    assert manifest.devices[0]["connection"]["local_bus"] == local_bus


def test_a_local_bus_longer_than_the_limit_is_rejected():
    """超长的值只能是拼接出来的,总线名与 tty 路径都到不了这么长。"""
    too_long = "/dev/" + "x" * GATEWAY_LOCAL_BUS_MAX_LENGTH

    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest(too_long))

    assert "local_bus" in str(caught.value)


def test_an_empty_local_bus_is_rejected():
    """空总线名在装配期就该拒:到了网关主机上只是 bus-agent 的一句参数错误。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest(""))

    assert "local_bus" in str(caught.value)


# --------------------------- 第二道防线:拼命令时 quote ---------------------------


class _Result:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _RecordingSsh:
    """假 SshTransport:记下真正会送到网关主机上执行的命令串。"""

    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.commands: list[tuple[str, float]] = []
        self._stdout = stdout
        self._exit_code = exit_code
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def exec_command(self, command: str, timeout: float = 10.0):
        self.commands.append((command, timeout))
        return _Result(self._exit_code, stdout=self._stdout)


def _transport(local_bus: str, stdout: str = "") -> tuple[GatewaySshTransport, _RecordingSsh]:
    transport = GatewaySshTransport("10.0.0.5", "test", local_bus)
    ssh = _RecordingSsh(stdout)
    transport._ssh = ssh
    transport._connected = True
    return transport, ssh


def test_the_send_command_is_byte_identical_for_a_legal_bus():
    """调用约定不能变:quote 对白名单字符集是恒等,命令串与修复前逐字节相同。

    右侧的期望串就是修复前的 f-string 写法 —— 这条断言挡住"顺手改了拼命令格式"。
    """
    payload = base64.b64encode(b"\x01\x02\x03").decode()
    transport, ssh = _transport("can0")

    transport.send(b"\x01\x02\x03")

    assert ssh.commands[0][0] == f"bus-agent send --bus can0 --data {payload}"


def test_the_receive_command_is_byte_identical_for_a_legal_bus():
    """同 send:合法总线的 recv 命令串一字不差(含 timeout 的写法)。"""
    transport, ssh = _transport("/dev/ttyUSB0", stdout=base64.b64encode(b"abc").decode())

    transport.receive(timeout=2.0)

    assert ssh.commands[0][0] == "bus-agent recv --bus /dev/ttyUSB0 --timeout 2.0"


@pytest.mark.parametrize("local_bus", _LEGAL)
def test_quote_is_identity_for_every_legal_bus(local_bus):
    """白名单字符集全在 shlex 安全集内,所以 quote 不改变合法值 —— 这是上面逐字节断言的前提。"""
    assert shlex.quote(local_bus) == local_bus


def test_the_base64_payload_is_not_pasted_as_raw_text():
    """payload 是 base64,不含 shell 元字符;send 里再 quote 一次也不会改动它。"""
    # b"\xfb\xff\xff" 的 base64 是 "+///":同时带 + 和 /,壳层里最容易出事的两个字符都在。
    assert base64.b64encode(b"\xfb\xff\xff").decode() == "+///"
    transport, ssh = _transport("can0")

    transport.send(b"\xfb\xff\xff")

    assert ssh.commands[0][0] == "bus-agent send --bus can0 --data +///"


def test_a_bus_that_bypassed_the_whitelist_still_reaches_the_shell_as_one_argument():
    """第二道防线的意义:就算有值绕过装配期(直接 new 传输),也只是一个 argv 里的字符串。"""
    hostile = "can0; id > /tmp/pwned #"
    payload = base64.b64encode(b"x").decode()
    transport, ssh = _transport(hostile)

    transport.send(b"x")
    transport.receive()

    send, _ = ssh.commands[0]
    recv, _ = ssh.commands[1]
    assert shlex.split(send) == ["bus-agent", "send", "--bus", hostile, "--data", payload]
    assert shlex.split(recv) == ["bus-agent", "recv", "--bus", hostile, "--timeout", "2.0"]


def test_the_command_keeps_the_agent_byte_semantics():
    """quote 只动命令串的引号,不动字节:base64 编解码照旧(半帧仍报 IncompleteFrameError 那条路)。"""
    transport, _ = _transport("can0", stdout=base64.b64encode(b"ab").decode())

    assert transport.receive() == b"ab"
