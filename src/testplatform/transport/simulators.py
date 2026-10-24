"""
所有协议的模拟器实现,全部满足和 real_transports.py / gateway_transport.py
完全相同的 Protocol(定义在 interfaces.py)。

这是"消费者定义接口"换来的最直接好处:真实设备和模拟器之间切换,
只需要改 devices.yaml 里的一个 mode 字段,domain/drivers 层代码一行不用改。

serial / tcp / gateway_ssh 这三种协议在模拟模式下共用同一个通用实现,
因为对驱动层来说它们的"形状"完全一样(都是 ByteStreamTransport)——
真实世界里它们走的物理链路完全不同,但这种差异只存在于 real_transports.py
和 gateway_transport.py 里,模拟器完全不需要关心。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, Mapping, Tuple

from testplatform.protocol import dlt645, modbus_rtu, modbus_tcp

from .registry import SIMULATOR_FAULT_KINDS
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


def _frame(answer: bytes, expected: FrameLength, what: str) -> bytes:
    """模拟器和真机保持同一个帧边界语义:调用方要多少就得给够,否则报错而不是给半帧。"""
    total = expected_bytes(expected, answer)
    if total is not None and len(answer) < total:
        raise IncompleteFrameError(
            f"{what}: 应答只有 {len(answer)} 字节,调用方要一帧 {total} 字节: {answer.hex()}"
        )
    return answer


# ---------------------------------------------------- 通用字节流模拟器 ----

class SimulatedByteStreamTransport:
    """响应规则由 response_fn 注入(接收"最近一次发送的请求字节",
    返回要回复的字节)。新增一种设备的模拟行为不需要改这个类,
    只需要在构造时传入不同的 response_fn,或者按命令关键字分支处理。
    """

    def __init__(self, response_fn: Callable[[bytes], bytes] | None = None) -> None:
        self._connected = False
        self._response_fn = response_fn or (lambda req: b"")
        self._last_sent: bytes = b""

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> None:
        self._last_sent = data

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        return _frame(self._response_fn(self._last_sent), expected, "字节流模拟器")


@dataclass(frozen=True)
class _SlaveRequest:
    """帧格式无关的请求:两种帧解析成同一个形状,寄存器表与故障注入只认它。

    事务号只有 MBAP 有(RTU 帧里没有这个字段,恒 0):它在这一层只是"顺手带过来的",
    不参与任何判定 —— 应答时原样回给客户端,其余字段两种格式一一对应。
    """

    transaction: int
    unit: int
    function: int
    address: int
    count: int = 0
    value: int = 0
    payload: bytes = b""


class _Framing:
    """一种帧格式对从站暴露的几个动作。两种格式差两件事:功能码在哪儿、字节怎么拼。

    其余(哪个寄存器、报什么异常码、什么时候拒)都属于从站行为,不在这一层里 —— 所以
    换帧格式不用重写那份逻辑,也就不会出现"RTU 版对、MBAP 版漏了一条"。
    """

    function_at: int

    def function(self, frame: bytes) -> int | None:
        return frame[self.function_at] if len(frame) > self.function_at else None

    def parse(self, frame: bytes) -> _SlaveRequest:
        raise NotImplementedError

    def read_response(self, request: _SlaveRequest, payload: bytes) -> bytes:
        raise NotImplementedError

    def write_response(self, request: _SlaveRequest) -> bytes:
        raise NotImplementedError

    def exception_response(self, request: _SlaveRequest, code: int) -> bytes:
        raise NotImplementedError


class _RtuFraming(_Framing):
    """RTU 帧的收发细节:从站地址 | PDU | CRC,全部走 protocol/modbus_rtu.py。"""

    function_at = 1

    def parse(self, frame: bytes) -> _SlaveRequest:
        function = self.function(frame)
        if function == modbus_rtu.WRITE_SINGLE_REGISTER:
            slave, _, address, value = modbus_rtu.parse_write_single_request(frame)
            return _SlaveRequest(0, slave, function, address, value=value)
        if function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
            slave, _, address, count, payload = modbus_rtu.parse_write_multiple_request(frame)
            return _SlaveRequest(0, slave, function, address, count=count, payload=payload)
        slave, function, address, count = modbus_rtu.parse_read_request(frame)
        return _SlaveRequest(0, slave, function, address, count=count)

    def read_response(self, request: _SlaveRequest, payload: bytes) -> bytes:
        return modbus_rtu.build_read_response(request.unit, payload, request.function)

    def write_response(self, request: _SlaveRequest) -> bytes:
        if request.function == modbus_rtu.WRITE_SINGLE_REGISTER:
            return modbus_rtu.build_write_single_request(
                request.unit, request.address, request.value
            )
        return modbus_rtu.build_write_multiple_response(
            request.unit, request.address, request.count
        )

    def exception_response(self, request: _SlaveRequest, code: int) -> bytes:
        return modbus_rtu.build_exception_response(request.unit, code, request.function)


class _MbapFraming(_Framing):
    """MBAP 帧的收发细节:7 字节头 | PDU,全部走 protocol/modbus_tcp.py。"""

    function_at = modbus_tcp.MBAP_HEADER_BYTES

    def parse(self, frame: bytes) -> _SlaveRequest:
        function = self.function(frame)
        if function == modbus_rtu.WRITE_SINGLE_REGISTER:
            transaction, unit, _, address, value = modbus_tcp.parse_write_single_request(frame)
            return _SlaveRequest(transaction, unit, function, address, value=value)
        if function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
            transaction, unit, _, address, count, payload = modbus_tcp.parse_write_multiple_request(
                frame
            )
            return _SlaveRequest(transaction, unit, function, address, count=count, payload=payload)
        transaction, unit, function, address, count = modbus_tcp.parse_read_request(frame)
        return _SlaveRequest(transaction, unit, function, address, count=count)

    def read_response(self, request: _SlaveRequest, payload: bytes) -> bytes:
        return modbus_tcp.build_read_response(
            request.transaction, request.unit, payload, request.function
        )

    def write_response(self, request: _SlaveRequest) -> bytes:
        if request.function == modbus_rtu.WRITE_SINGLE_REGISTER:
            return modbus_tcp.build_write_single_request(
                request.transaction, request.unit, request.address, request.value
            )
        return modbus_tcp.build_write_multiple_response(
            request.transaction, request.unit, request.address, request.count
        )

    def exception_response(self, request: _SlaveRequest, code: int) -> bytes:
        return modbus_tcp.build_exception_response(
            request.transaction, request.unit, code, request.function
        )


# 清单 connection.simulated_frames 的取值 → 帧格式。默认 modbus_rtu:既有配置一字不用改。
_FRAME_FORMATS = {"modbus_rtu": _RtuFraming(), "modbus_tcp": _MbapFraming()}


def _declared_faults(
    where: str, declared: Mapping[str, Any] | None, supported: Iterable[str]
) -> Dict[str, Any]:
    """把清单里的 connection.simulated_faults 变成一份能用的故障表。

    两种错都当场报(装配期):种类名不认识(白名单是 registry 里的 SIMULATOR_FAULT_KINDS)、
    或者种类认识但这个模拟器根本造不出来(比如 MBAP 帧上的坏 CRC —— 那个帧里没有 CRC)。
    静默忽略等于让"故障已注入"变成假绿。
    """
    if declared is None:
        return {}
    if not isinstance(declared, Mapping):
        raise ValueError(
            f"{where}: simulated_faults 必须是映射(种类 → 参数),拿到 {declared!r};"
            f"可用种类: {list(SIMULATOR_FAULT_KINDS)}"
        )
    unknown = sorted(set(declared) - set(SIMULATOR_FAULT_KINDS))
    if unknown:
        raise ValueError(
            f"{where}: 不认识的故障种类 {unknown};可用种类: {list(SIMULATOR_FAULT_KINDS)}"
        )
    unsupported = sorted(set(declared) - set(supported))
    if unsupported:
        raise ValueError(
            f"{where}: 这个模拟器造不出 {unsupported};它支持的故障种类: {list(supported)}"
        )
    faults: Dict[str, Any] = {}
    for kind, setting in declared.items():
        if kind == "slow":
            try:
                seconds = float(setting)
            except (TypeError, ValueError):
                raise ValueError(f"{where}: slow 要给秒数(数字),拿到 {setting!r}") from None
            if seconds <= 0:
                raise ValueError(f"{where}: slow 的秒数必须大于 0,拿到 {setting!r}")
            faults["slow"] = seconds
        else:
            faults[kind] = bool(setting)
    return faults


class SimulatedModbusSlave:
    """Modbus 从站模拟器:按寄存器表应答 03/04 读请求、0x06 写单个 / 0x10 写多个寄存器。

    寄存器表与故障注入只有一份(见 _read 与两个 _write_*):帧格式由 frames 选,同一份表、同一套拒绝规则
    既能产出 RTU 帧也能产出 MBAP 帧。两种格式的差异只在 _RtuFraming / _MbapFraming 里 ——
    换帧格式不换读表逻辑,也就不存在"RTU 版对、MBAP 版漏了一条"这种只在一边发生的偏差。

    和固定文本模拟器一样只是字节管道:帧怎么拼、CRC / 长度字段怎么算委托给 protocol 层,
    这里只负责"某个寄存器地址上现在是多少"。registers 的键是寄存器地址,值是 16 位无符号数。
    写进去的值就在这张表里,所以同一实例上的下一次读能读到它。
    """

    # 能力声明:这个模拟器真能造出来的故障。坏 CRC 只在 RTU 帧下声明 —— MBAP 帧里没有 CRC,
    # 声明它就是编一个我们造不出来的能力。报告与目录从 registry.simulator_capabilities 读它。
    FAULTS = ("timeout", "slow", "bad-crc", "link-lost")
    FAULTS_WITHOUT_CRC = ("timeout", "slow", "link-lost")

    def __init__(
        self,
        registers: Dict[int, int],
        frames: str = "modbus_rtu",
        faults: Mapping[str, Any] | None = None,
    ) -> None:
        if frames not in _FRAME_FORMATS:
            raise ValueError(
                f"模拟从站只支持帧格式 {sorted(_FRAME_FORMATS)},拿到 {frames!r}"
                f"(RTU 帧写 modbus_rtu;带 MBAP 头的写 modbus_tcp)"
            )
        self._registers = {int(address): int(value) for address, value in registers.items()}
        self._framing = _FRAME_FORMATS[frames]
        self.supported_faults = (
            self.FAULTS_WITHOUT_CRC if frames == "modbus_tcp" else self.FAULTS
        )
        self._faults = _declared_faults(
            f"Modbus 从站模拟器({frames})", faults, self.supported_faults
        )
        self._connected = False
        self._last_sent: bytes = b""

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> None:
        self._last_sent = data

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        """功能码在帧里的位置由帧格式定,其余全是同一个从站行为。

        三个私有钩子收的都是原始帧字节(parse 在钩子里做),因为写路径的测试子类就是按这个
        形状接上去的(如 tests/test_meter.py 里"设备拒了这次写"的模拟从站)。
        """
        self._apply_faults()
        function = self._framing.function(self._last_sent)
        if function == modbus_rtu.WRITE_SINGLE_REGISTER:
            answer = self._write_single_register(self._last_sent, expected)
        elif function == modbus_rtu.WRITE_MULTIPLE_REGISTERS:
            answer = self._write_multiple_registers(self._last_sent, expected)
        else:
            answer = self._read(self._last_sent, expected)
        return self._break_crc(answer)

    def _apply_faults(self) -> None:
        """清单里声明的故障:装配时定死,不支持运行中切换(见 registry.simulator_capabilities)。

        断链先于超时:两者同时声明时,链路不在才是更根本的那件事。延迟在答之前发生 ——
        要验的是"应答来得晚",不是"解析得慢"。
        """
        if self._faults.get("link-lost"):
            raise ConnectionError("注入的故障 link-lost:模拟从站按清单设定断链")
        if self._faults.get("timeout"):
            raise TimeoutError("注入的故障 timeout:模拟从站按清单设定不回答")
        delay = self._faults.get("slow")
        if delay:
            time.sleep(delay)

    def _break_crc(self, answer: bytes) -> bytes:
        """坏 CRC:只翻最后一字节。帧长度依旧对,所以走的是"校验不过"而不是"半帧"。"""
        if not self._faults.get("bad-crc"):
            return answer
        broken = bytearray(answer)
        broken[-1] ^= 0xFF
        return bytes(broken)

    def inject_point(self, point: Any, raw: bytes) -> bytes:
        """数据注入:按点表把一个值直接写进寄存器表,并回读一份原始字节给调用方验证。

        不经协议、不发帧、不产生 write 记录:注入只回答"设备现在报什么",不回答
        "产品能不能写设备" —— 后者必须走协议写(0x06 / 0x10),证据里才有 write 那条。
        回读用读路径同一个 `_word`,所以调用方拿到的是"下一次读真会读到的字节"。
        """
        words = [raw[index : index + 2] for index in range(0, len(raw), 2)]
        if len(raw) != point.length or not words:
            raise ValueError(
                f"注入 {point.key!r} 的字节数必须是点表 length={point.length}(整寄存器),"
                f"拿到 {len(raw)} 字节: {raw.hex()}"
            )
        addresses = [point.address + index for index in range(len(words))]
        missing = [address for address in addresses if address not in self._registers]
        if missing:
            raise KeyError(
                f"模拟从站没有寄存器 {missing}(点 {point.key!r} 从 {point.address} 起算),"
                f"connection.simulated_registers 里已声明: {sorted(self._registers)}"
            )
        for address, word in zip(addresses, words):
            self._registers[address] = int.from_bytes(word, "big")
        return b"".join(self._word(address) for address in addresses)

    def _read(self, request: bytes, expected: FrameLength) -> bytes:
        parsed = self._framing.parse(request)
        payload = b"".join(
            self._word(parsed.address + offset) for offset in range(parsed.count)
        )
        return _frame(self._framing.read_response(parsed, payload), expected, "Modbus 从站模拟器")

    def _write_single_register(self, request: bytes, expected: FrameLength) -> bytes:
        """0x06:值进内存表,应答是请求帧原样回显(单元号 + 地址 + 值,不换一个字)。

        地址不存在时回异常应答 02:和独立 mock 从站(tools/mock_modbus_meter.py)同一个答案、
        也同一个异常码。写路径不许猜"地址不在表里"是不是可以用一个默认值顶上。
        """
        parsed = self._framing.parse(request)
        if parsed.address not in self._registers:
            answer = self._framing.exception_response(parsed, modbus_rtu.ILLEGAL_DATA_ADDRESS)
        else:
            self._registers[parsed.address] = parsed.value
            answer = self._framing.write_response(parsed)
        return _frame(answer, expected, "Modbus 从站模拟器")

    def _write_multiple_registers(self, request: bytes, expected: FrameLength) -> bytes:
        """0x10:先整帧校验再写 —— 一个寄存器越界就一个都不写,应答回显起始地址 + 寄存器数。

        异常码按 Modbus 规范,与独立 mock 从站逐字节相同:个数非法(0 个 / 超过 0x7B)是
        03 非法数据值(拆小重发能过),地址不在表里是 02 非法数据地址。
        """
        parsed = self._framing.parse(request)
        if not 1 <= parsed.count <= modbus_rtu.WRITE_MULTIPLE_MAX_REGISTERS:
            answer = self._framing.exception_response(parsed, modbus_rtu.ILLEGAL_DATA_VALUE)
        elif any(
            parsed.address + offset not in self._registers for offset in range(parsed.count)
        ):
            answer = self._framing.exception_response(parsed, modbus_rtu.ILLEGAL_DATA_ADDRESS)
        else:
            for offset in range(parsed.count):
                self._registers[parsed.address + offset] = int.from_bytes(
                    parsed.payload[offset * 2 : offset * 2 + 2], "big"
                )
            answer = self._framing.write_response(parsed)
        return _frame(answer, expected, "Modbus 从站模拟器")

    def _word(self, address: int) -> bytes:
        if address not in self._registers:
            raise KeyError(
                f"模拟从站没有寄存器 {address},connection.simulated_registers 里已声明: "
                f"{sorted(self._registers)}"
            )
        return self._registers[address].to_bytes(2, "big")


class SimulatedDlt645Bus:
    """DL/T645 从站模拟器:模拟"一条串口线上挂着多块表",按地址域选表应答。

    meters 的键是表地址(12 位 BCD,如 "000000000001"),值是该表的
    {数据标识: 值的原始字节}(十六进制串,低字节先、未加 0x33)。
    一台终端下挂多块表时它们共用这一个对象 —— 这正是物理上的一条总线。
    """

    def __init__(self, meters: Dict[str, Dict[str, Any]]) -> None:
        self._meters: Dict[str, Dict[str, bytes]] = {
            address: {
                data_id: _hex_bytes(value, f"模拟表 {address} 的数据标识 {data_id}")
                for data_id, value in points.items()
            }
            for address, points in meters.items()
        }
        self._connected = False
        self._last_sent: bytes = b""

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> None:
        self._last_sent = data

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        address, data_id = dlt645.parse_read_request(self._last_sent)
        return _frame(
            dlt645.build_read_response(address, data_id, self._value(address, data_id)),
            expected,
            "DL/T645 总线模拟器",
        )

    def _value(self, address: str, data_id: str) -> bytes:
        if address not in self._meters:
            raise KeyError(
                f"模拟总线上没有表 {address},connection.simulated_645 里已声明: "
                f"{sorted(self._meters)}"
            )
        points = self._meters[address]
        if data_id not in points:
            raise KeyError(
                f"模拟表 {address} 没有数据标识 {data_id},connection.simulated_645 里已声明: "
                f"{sorted(points)}"
            )
        return points[data_id]


def _hex_bytes(value: Any, where: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f'{where}: 原始字节要写成十六进制字符串(如 "0022"),拿到 {value!r}')
    return bytes.fromhex(value)


# 三相台架设定的帧头(与 drivers/grid_simulator_driver.py::apply_three_phase 同一个帧)。
# 帧是 `SET_GRID3 量名=值 …`:量名沿用上游条款的写法,台架按同一份量名回。
GRID_THREE_PHASE_SET = "SET_GRID3"


class SimulatedGridSimulator(SimulatedByteStreamTransport):
    """进程内三相台架:把 `SET_GRID3 量名=值 …` 的设定原样回给驱动。

    这只能证明驱动按点表量名下发、按同一份量名读回 —— 真机上这些值来自台架本体,
    量的地址与编码到时候才有定义(型号点表里现在是占位地址)。
    其它帧(如单相 `SET_GRID v hz`)仍走 simulated_response 的固定应答。
    """

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        request = self._last_sent.decode().strip()
        prefix = GRID_THREE_PHASE_SET + " "
        if request.startswith(prefix):
            answer = (request[len(prefix) :] + "\n").encode()
            return _frame(answer, expected, "三相台架模拟器")
        return super().receive(timeout, expected)


def _make_fixed_response_fn(fixed_text: str) -> Callable[[bytes], bytes]:
    """devices.yaml 里配一个 simulated_response 字符串,
    不管发的是什么指令都固定回复这个值——够用于演示和大多数冒烟测试。
    需要"按指令区分回复内容"的更真实模拟时,可以在具体 driver 的工厂函数里
    直接构造一个自定义 response_fn 传进来,不需要改这个文件。"""

    payload = (fixed_text + "\n").encode()
    return lambda _req: payload


@register_transport("serial", "simulator")
@register_transport("tcp", "simulator")
@register_transport("gateway_ssh", "simulator")
def _build_bytestream_sim(cfg: dict) -> ByteStreamTransport:
    # 只有 Modbus 从站实现了故障注入。其余模拟器收到 simulated_faults 就报装配期错误:
    # 静默忽略会让"我这轮注入了超时"变成假绿,而用例根本没过那条路径。
    if "simulated_faults" in cfg and "simulated_registers" not in cfg:
        raise ValueError(
            f"这台模拟器不支持故障注入:清单里声明了 connection.simulated_faults,"
            f"但模拟对象不是 Modbus 从站 —— 目前只有它实现了故障注入"
            f"(connection.simulated_registers)"
        )
    # 二进制规约的应答是按帧拼出来的,固定文本对它没有意义。
    if "simulated_645" in cfg:
        return SimulatedDlt645Bus(cfg["simulated_645"])
    if "simulated_registers" in cfg:
        return SimulatedModbusSlave(
            cfg["simulated_registers"],
            frames=cfg.get("simulated_frames", "modbus_rtu"),
            faults=cfg.get("simulated_faults"),
        )
    fixed_text = cfg.get("simulated_response", "0 0 0")
    response_fn = _make_fixed_response_fn(fixed_text)
    if cfg.get("simulated_grid"):
        return SimulatedGridSimulator(response_fn=response_fn)
    return SimulatedByteStreamTransport(response_fn=response_fn)


# ------------------------------------------------------------ CAN 模拟器 ----

class SimulatedCanTransport:
    """简单的回环队列:发出去的帧可以被收到,方便验证基本收发链路。"""

    def __init__(self) -> None:
        self._connected = False
        self._queue: Deque[Tuple[int, bytes]] = deque()

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send_frame(self, arbitration_id: int, data: bytes) -> None:
        self._queue.append((arbitration_id, data))

    def receive_frame(self, timeout: float = 2.0):
        if not self._queue:
            raise TimeoutError("模拟 CAN 总线无数据可读")
        return self._queue.popleft()


@register_transport("can", "simulator")
def _build_can_sim(cfg: dict) -> CanTransport:
    return SimulatedCanTransport()


# ----------------------------------------------------------- HTTP 模拟器 ----

class _SimHttpResponse:
    def __init__(self, status_code: int, json_body: dict) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = str(json_body)

    def json(self) -> dict:
        return self._json


class SimulatedHttpTransport:
    def __init__(self, fixed_response: dict | None = None) -> None:
        self._fixed_response = fixed_response or {"status": "ok"}

    def request(self, method: str, path: str, json: dict | None = None, timeout: float = 5.0):
        return _SimHttpResponse(200, self._fixed_response)


@register_transport("http", "simulator")
def _build_http_sim(cfg: dict) -> HttpTransport:
    return SimulatedHttpTransport(fixed_response=cfg.get("simulated_response"))


# ------------------------------------------------------------ SSH 模拟器 ----

class _SimSshResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class SimulatedSshTransport:
    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def exec_command(self, command: str, timeout: float = 10.0):
        return _SimSshResult(0, stdout=f"simulated output for: {command}", stderr="")


@register_transport("ssh", "simulator")
def _build_ssh_sim(cfg: dict) -> SshTransport:
    return SimulatedSshTransport()


# ----------------------------------------------------------- MQTT 模拟器 ----

class SimulatedMqttTransport:
    """本地内存实现发布订阅:publish 直接同步回调已注册的 handler,
    不需要真实 broker,适合在 CI 里跑涉及 MQTT 的逻辑测试。"""

    def __init__(self) -> None:
        self._connected = False
        self._handlers: Dict[str, Callable[[str, bytes], None]] = {}

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        handler = self._handlers.get(topic)
        if handler is not None:
            handler(topic, payload)

    def subscribe(self, topic: str, handler: Callable[[str, bytes], None]) -> None:
        self._handlers[topic] = handler


@register_transport("mqtt", "simulator")
def _build_mqtt_sim(cfg: dict) -> MqttTransport:
    return SimulatedMqttTransport()

