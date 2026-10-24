"""
Modbus 异常应答的判定只有一份(RTU 与 MBAP 共用),两条路径因此同一套降级 / 重试 / 留痕。

从站回异常码(RTU 的 0x83、MBAP 的 0x83 —— PDU 逐字节相同)时,后果不该取决于帧格式:
块读被拒 → 拆开逐点重试 → 能读到的点照旧交出、被拒的块进 batch_fallback 记录。
一份判断拆成两份实现,迟早有一边漏掉,而漏掉的那一边会把异常应答当普通应答继续解,
读出来的是一坨"看着正常"的错误数据。

所以这里锁两件事:
1. 结构:异常码标志位与 is_exception_response 各只有一处定义,MBAP 用的就是 RTU 那一个对象;
2. 行为:同一份故障注入(合并块扫到没实现的寄存器 → 整帧回异常码 03)喂给两种帧,
   请求序列、读数、降级留痕逐项相同。
"""
import ast
from pathlib import Path

import pytest

from testplatform.config.manifest import Point
from testplatform.drivers import meter_driver
from testplatform.protocol import modbus_rtu, modbus_tcp
from testplatform.transport.simulators import SimulatedModbusSlave

PROTOCOL_DIR = Path(modbus_rtu.__file__).parent
# 点表:两个点之间空着地址 1 —— 合并成一块(0 起 3 个)时会顺带扫到它。
UNIMPLEMENTED = {1}
REGISTERS = {0: 0x0001, 2: 2300}


def _gap_points():
    return {
        "有功电量": Point(key="有功电量", address=0, length=2, encoding="uint", scale=0.01),
        "电压": Point(key="电压", address=2, length=2, encoding="uint", scale=0.1),
    }


class _GapRejectingSlave(SimulatedModbusSlave):
    """模拟真机:合并块扫到没实现的地址(哪怕只是顺带扫到),整帧回异常码 03。

    帧格式由基类给(SimulatedModbusSlave 的 frames 参数),所以同一份"从站行为"能产出
    RTU 帧也能产出 MBAP 帧 —— 这正是两条路径能拿同一份故障注入比较的前提。
    """

    def __init__(self, frames: str) -> None:
        super().__init__(dict(REGISTERS), frames=frames)
        self.requests = []  # (功能码, 起始寄存器, 寄存器数):帧格式无关的形状
        self.degraded = []  # report_degraded_block 收到的记录(证据层的那个钩子)

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        parsed = self._framing.parse(self._last_sent)
        self.requests.append((parsed.function, parsed.address, parsed.count))
        if any(address in UNIMPLEMENTED for address in range(parsed.address, parsed.address + parsed.count)):
            # 异常应答是完整一帧:MBAP 的长度字段 / RTU 的 CRC 都由 protocol 层算好。
            return self._framing.exception_response(parsed, modbus_rtu.ILLEGAL_DATA_VALUE)
        return super().receive(timeout=timeout, expected=expected)

    def report_degraded_block(self, record) -> None:
        self.degraded.append(record)


@pytest.mark.parametrize(
    "frames, codec",
    [
        pytest.param("modbus_rtu", modbus_rtu, id="RTU"),
        pytest.param("modbus_tcp", meter_driver._MbapLink(), id="TCP"),
    ],
)
def test_a_rejected_block_degrades_point_by_point_in_both_frames(frames, codec):
    """异常码 → 块降级逐点重试 → 读数照常交出,且两种帧的请求序列逐项相同。

    变异:让 MBAP 的解析绕过共用判定(把异常应答当普通应答解) → 这条用例红在 TCP 参数上。
    """
    slave = _GapRejectingSlave(frames)

    raw = meter_driver._modbus_block_reader(slave, 1, codec)(_gap_points())

    assert raw == {"有功电量": b"\x00\x01", "电压": b"\x08\xfc"}
    assert slave.requests == [
        (modbus_rtu.READ_HOLDING_REGISTERS, 0, 3),  # 先试合并的块,被拒
        (modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),  # 降级:逐点重试,空隙地址不再碰
        (modbus_rtu.READ_HOLDING_REGISTERS, 2, 1),
    ]


@pytest.mark.parametrize(
    "frames, codec",
    [
        pytest.param("modbus_rtu", modbus_rtu, id="RTU"),
        pytest.param("modbus_tcp", meter_driver._MbapLink(), id="TCP"),
    ],
)
def test_the_degraded_block_leaves_the_same_trace_in_both_frames(frames, codec):
    """降级要在证据里留痕,而且两种帧留的痕迹一样:异常码、块区间、读到 / 没读到的点。

    没有这条记录,现场只看到"这次读数突然多发了几帧",看不出为什么。
    """
    slave = _GapRejectingSlave(frames)

    meter_driver._modbus_block_reader(slave, 1, codec)(_gap_points())

    assert slave.degraded == [
        {
            "function": modbus_rtu.READ_HOLDING_REGISTERS,
            "address": 0,
            "count": 3,
            "exception_code": modbus_rtu.ILLEGAL_DATA_VALUE,
            "readable": ["有功电量", "电压"],
            "unreadable": [],
        }
    ]


def test_the_exception_code_is_judged_in_exactly_one_place():
    """结构断言:异常码标志位只定义一处,判定函数只有一份,MBAP 用的就是 RTU 那个对象。

    变异:在 modbus_tcp.py 里再写一个 `pdu[0] == function | 0x80` → 字面量计数变 2,红;
    在那边复制一个同名函数 → 不再是同一个对象、定义数变 2,红。
    """
    assert modbus_tcp.is_exception_response is modbus_rtu.is_exception_response

    literals = {}
    for module in (modbus_rtu, modbus_tcp):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        literals[module.__name__] = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and node.value == 0x80
        ]
    assert literals == {modbus_rtu.__name__: [0x80], modbus_tcp.__name__: []}

    definitions = [
        node.name
        for path in PROTOCOL_DIR.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "is_exception_response"
    ]
    assert definitions == ["is_exception_response"]
