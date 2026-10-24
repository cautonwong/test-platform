"""
本模块里的每一个 Protocol,概念上都是"drivers/ 里的具体驱动"这个消费者
按自己的需要定义出来的最小契约——放在 transport/ 目录只是因为多个驱动
会复用同一种协议族的"形状"(比如 Inverter 和 Battery 都可能用串口),
集中声明可以避免重复定义相同接口。

有一条依赖方向必须被严格遵守:
    real_transports.py / simulators.py / gateway_transport.py
    都 import 这里的 Protocol 去"实现"它们;
    这个文件本身永远不 import 任何一个具体实现。
也就是说,基础设施依赖这里的抽象,这里的抽象不知道基础设施的存在。

每种协议的接口形状都不同,这是刻意的(接口隔离原则):
字节流式(串口/TCP/经网关透传)、总线帧式(CAN)、请求响应式(HTTP)、
命令执行式(SSH)、发布订阅式(MQTT)——想用一个大而全的 Transport
接口描述所有这些,只会产出一堆每个实现都用不上的空方法。

所有传输的 has_client() 只回答一个问题:"底层客户端/句柄对象还在吗"(connect 之后有,
disconnect 之后没有)。它不代表链路健康 —— TCP 对端拔线或 RST、USB 转串口被拔掉之后,
对象都还在,它仍然是 True。链路死没死以 send/receive 抛出的实际异常为准(证据层按异常
记 timeout / disconnect)。名字刻意不叫 is_connected:那种名字会让人拿它当重连判据。
"""
from __future__ import annotations

from typing import Callable, Protocol, Tuple, runtime_checkable

# "这一次要收多少字节"。定长帧给字节数;长度要先看应答头才能算出来的规约
# (Modbus 应答里的字节数、DL/T645 的 L)给一个「已收到的字节 → 整帧长度」的函数,
# 头还不够时它返回 None;None 表示长度未知(比如一行 ASCII 文本),读到链路静默为止。
# 帧边界由知道规约的一方(驱动的 reader)说,transport 只负责收满,不认识任何规约。
FrameLength = int | Callable[[bytes], "int | None"] | None


def expected_bytes(expected: FrameLength, head: bytes) -> int | None:
    """把 FrameLength 统一成一个字节数:int 原样返回,函数按已收到的 head 算,None 仍是 None。"""
    if expected is None or isinstance(expected, int):
        return expected
    return expected(head)


class IncompleteFrameError(TimeoutError):
    """短读超时:这一次收到的字节不够一帧,半帧绝不交给上层。

    继承 TimeoutError 是为了让证据层按既有规则记成 error: timeout;单独一个类型是为了
    让"彻底没收到"和"只收到半帧"在排障时分得开 —— 半帧解出的"看着正常"的假读数最难查。
    """


@runtime_checkable
class ByteStreamTransport(Protocol):
    """串口 / TCP / 经工控机网关透传的本地总线 ——
    这三者对驱动层来说形状完全一样:连接 + 发送字节 + 接收字节。"""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def has_client(self) -> bool:
        """底层客户端/句柄对象还在吗(语义见模块文档:不代表链路健康,不能当重连判据)。"""
        ...

    def send(self, data: bytes) -> None: ...

    def receive(
        self,
        timeout: float = 2.0,
        expected: FrameLength = None,
        read_gap_ms: float = 50.0,
    ) -> bytes:
        """收一帧。expected 是调用方按规约给的期望帧长(见 FrameLength)。

        给了 expected 就必须收满再返回:短读继续读,超时还差字节时抛 IncompleteFrameError。
        底层读的缓冲区大小不是帧的边界,也不许静默截断。

        expected 为 None(长度未知)时读到"这一轮还能读到的全部":每次读之间等
        read_gap_ms,连续静默即认为这一轮结束。它只承诺**不截断**,**不承诺帧完整**——
        帧完整性由知道规约的上层解出来判。read_gap_ms 是读取参数不是帧边界,所以显式在
        签名上:对端字节间隔比它大时这一轮会提前收尾,调大它,而不是去查协议。
        总时长 timeout 用完对端还在发字节就报错,不把半帧当一帧交上去。
        链路被对端关掉要抛 ConnectionError(不是超时):断链与超时的现场结论不同
        (测试无效 / 可能不合格),各传输按自己的能力把两者分开,分不开的(串口)写明理由。
        """
        ...


@runtime_checkable
class CanTransport(Protocol):
    """CAN 总线:帧结构(仲裁 ID + 数据)和字节流不同,单独定义接口。"""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def has_client(self) -> bool:
        """同 ByteStreamTransport.has_client:只表示底层客户端对象还在,不代表链路健康。"""
        ...

    def send_frame(self, arbitration_id: int, data: bytes) -> None: ...

    def receive_frame(self, timeout: float = 2.0) -> Tuple[int, bytes]: ...


class HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> dict: ...


@runtime_checkable
class HttpTransport(Protocol):
    """HTTP/REST:请求-响应模型,和字节流完全不同的形状。"""

    def request(
        self, method: str, path: str, json: dict | None = None, timeout: float = 5.0
    ) -> HttpResponse: ...


class SshResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class SshTransport(Protocol):
    """SSH:对主机(比如工控机)执行命令,而不是读写寄存器——
    也是 GatewaySshTransport 底层复用的能力。"""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def has_client(self) -> bool:
        """同 ByteStreamTransport.has_client:只表示底层客户端对象还在,不代表链路健康。"""
        ...

    def exec_command(self, command: str, timeout: float = 10.0) -> SshResult: ...


@runtime_checkable
class MqttTransport(Protocol):
    """MQTT:发布订阅,没有"请求-响应"这个概念,接口形状必须单独设计,
    不能生硬套用 send/receive。"""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def has_client(self) -> bool:
        """同 ByteStreamTransport.has_client:只表示底层客户端对象还在,不代表链路健康。"""
        ...

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> None: ...

    def subscribe(self, topic: str, handler: Callable[[str, bytes], None]) -> None: ...
