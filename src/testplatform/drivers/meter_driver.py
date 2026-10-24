"""
MeterDriver —— 电表走点表驱动的通用读取路径(drivers/point_table_driver.py)。

这个文件里和规约有关的东西只有两个 read_raw:**一个点表项一次读请求**、收回应答帧并校验;
以及块读 read_raw_many:Modbus 有寄存器地址,所以地址相邻的点能合并成一次请求
(见 _plan_read_blocks);从站用异常应答拒掉一块时降级为逐点重试(见 _retry_point_by_point),
并把这个块记在驱动实例上,下一轮不再顺带扫它扫过的空隙(见 _modbus_block_reader)。帧怎么拼、校验怎么算、字节序怎么解,全在 protocol/ 里;
读哪些点、地址在哪、倍率多少,全在 yaml 的点表里。所以换一个同规约的型号
(不同地址/不同倍率)只需要改 yaml,这个文件一个字都不动。

注册键是 (设备类型, 规约):("meter", "modbus_rtu") 与 ("meter", "dlt645")。
两个规约共用同一个 PointTableDriver —— 第二规约接入时通用读取路径一行未改。
DL/T645 不注入块读:它的数据标识不是连续地址,一次请求只能拿一个标识。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from testplatform.config.manifest import Model, Point
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.protocol import dlt645, modbus_rtu, modbus_tcp
from testplatform.transport.interfaces import ByteStreamTransport

from .point_table_driver import PointTableDriver, ReadRaw, ReadRawMany, WriteRaw
from .registry import register_device

# 一次读请求最多读多少个寄存器。协议 PDU 上限是 125 个(0x7D),取 120 留 5 个余量:
# 有些从站把上限实现得更严(按字节数算),一旦超限回来的不是数据而是异常码 03。
MAX_REGISTERS_PER_REQUEST = 120
# 两个点之间空几个寄存器以内还值得合并成一次请求。空隙里的寄存器也占应答字节和一次往返,
# 超过 10 个就另发一次请求更划算。被拒过的空隙不走这条:合并反而不划算(见 _plan_read_blocks)。
MAX_GAP_TO_MERGE = 10


@dataclass
class _ReadBlock:
    """一次读请求覆盖的连续寄存器区间,以及这段区间里装了哪几个读数键。"""

    function: int
    address: int
    count: int
    keys: List[str] = field(default_factory=list)


def _register_count(point: Point) -> int:
    """点表项的 length 是字节数,寄存器是 2 字节:不足一个寄存器的也占一整个。"""
    return (point.length + 1) // 2


# 被拒过的块区间:(功能码, 起(含), 止(不含))。
RejectedSpan = Tuple[int, int, int]


def _span_already_rejected(
    rejected: Sequence[RejectedSpan], function: int, start: int, end: int
) -> bool:
    """[start, end) 这段空隙里有没有寄存器已经被一个被拒的块顺带扫过:有就再不扫它。

    不认识这个空隙里到底是哪一个地址没实现 —— 整块被拒,从站不说是谁。所以整段都不再扫:
    扫它要付的是一次注定被拒的往返,而省下的是这次往返里顺带读到的几个点。
    """
    return any(
        function == rejected_function and start < rejected_end and rejected_start < end
        for rejected_function, rejected_start, rejected_end in rejected
    )


def _plan_read_blocks(
    points: Mapping[str, Point], rejected: Sequence[RejectedSpan] = ()
) -> List[_ReadBlock]:
    """把一张点表合并成尽量少的读请求:按 (功能码, 地址) 排序后贪心扫描。

    三个条件同时成立才接上当前块:功能码相同(0x03 与 0x04 是两次不同的请求,绝不合并)、
    空隙不超过 MAX_GAP_TO_MERGE、合并后宽度不超过 MAX_REGISTERS_PER_REQUEST。
    任一不满足就另起一块。

    rejected 是这台表上被拒过的块区间:跨空隙的合并只要碰上它就不做,只剩真正相邻的
    (空隙 0 个寄存器)还能合并。真机电表几乎总有空洞,否则每轮都白付一次被拒的往返。
    """
    ordered = sorted(
        (
            (point.function, point.address, _register_count(point), key)
            for key, point in points.items()
        ),
        key=lambda item: (item[0], item[1]),
    )
    blocks: List[_ReadBlock] = []
    for function, address, count, key in ordered:
        current = blocks[-1] if blocks else None
        if current is not None and function == current.function:
            end = current.address + current.count
            gap = address - end
            width = max(address + count, end) - current.address
            mergeable_gap = gap == 0 or (
                gap <= MAX_GAP_TO_MERGE
                and not _span_already_rejected(rejected, function, end, address)
            )
            if mergeable_gap and width <= MAX_REGISTERS_PER_REQUEST:
                current.count = width
                current.keys.append(key)
                continue
        blocks.append(_ReadBlock(function=function, address=address, count=count, keys=[key]))
    return blocks


def _read_registers(
    codec: Any,
    transport: ByteStreamTransport,
    unit: int,
    function: int,
    address: int,
    count: int,
) -> bytes:
    """发一帧"读 count 个寄存器"并把它那几个寄存器字节交上来。

    codec 是帧格式本身:RTU 直接传 protocol/modbus_rtu 模块,MBAP 传 _MbapLink(它只补上
    帧头里的事务号)。两个 codec 暴露同一组名字(build_read_request / response_length /
    parse_read_response),所以块规划、降级重试、解码这段只有一份。
    """
    transport.send(codec.build_read_request(unit, address, count, function))
    # 应答多长由应答头算(正常/异常应答长度不同):transport 因此能收满一帧再交上来,
    # 而不是把"这次 read 到的多少字节"当一帧。
    frame = transport.receive(timeout=2.0, expected=codec.response_length)
    return codec.parse_read_response(frame, unit, function)


@dataclass(frozen=True)
class UnreadablePoint:
    """降级后逐点重试仍然读不到的点表项:名字、地址、从站给的异常码。"""

    key: str
    address: int
    code: int


class ModbusPointsUnreadable(ValueError):
    """块读降级、逐点重试之后仍然有点读不到:这一次读数失败。

    读不到就是失败:不填默认值,也不拿上一次的值顶上 —— 判据缺量只会给出"看着正常"的假结论。
    但失败要失败得具体:哪些点读到了、哪些点没读到、从站给的异常码是什么,全部写在消息里。
    """

    def __init__(self, failures: List[UnreadablePoint], readable_keys: List[str]) -> None:
        self.failures = tuple(failures)
        self.readable_keys = tuple(readable_keys)
        failed = "; ".join(
            f"{point.key}(地址 {point.address}, 异常码 {point.code:#04x})" for point in failures
        )
        super().__init__(
            f"块读降级后逐点重试,仍有 {len(failures)} 个点读不到: {failed};"
            f"同一次读数里读到了 {len(readable_keys)} 个点: {list(readable_keys)}"
        )


def _retry_point_by_point(
    codec: Any,
    transport: ByteStreamTransport,
    slave: int,
    block: _ReadBlock,
    points: Mapping[str, Point],
) -> tuple[Dict[str, bytes], List[UnreadablePoint]]:
    """把被拒的块拆成一个点一次请求:能读到的交字节,读不到的留下名字与异常码。

    只重试点表里真有的点 —— 空隙里那些没人要的寄存器不再碰,读失败也就只落在真读不到的点上。
    重试途中再遇到超时 / CRC 错照样原样上抛:那不是"这个地址不认",拆得更小也没用。
    """
    readable: Dict[str, bytes] = {}
    unreadable: List[UnreadablePoint] = []
    for key in block.keys:
        point = points[key]
        try:
            readable[key] = _read_registers(
                codec, transport, slave, point.function, point.address, _register_count(point)
            )
        except modbus_rtu.SlaveExceptionResponse as exc:
            unreadable.append(UnreadablePoint(key=key, address=point.address, code=exc.code))
    return readable, unreadable


def _report_degraded_block(
    transport: Any,
    block: _ReadBlock,
    cause: modbus_rtu.SlaveExceptionResponse,
    readable: Mapping[str, bytes],
    unreadable: List[UnreadablePoint],
) -> None:
    """降级留痕。证据层是套在传输外面的一层装饰:套了就顺手写一条,没开记录就什么都不做。

    没有这条记录,现场只能看到"这台表这次读数突然慢了好几倍",看不出为什么。
    """
    report = getattr(transport, "report_degraded_block", None)
    if report is None:
        return
    report(
        {
            "function": block.function,
            "address": block.address,
            "count": block.count,
            "exception_code": cause.code,
            "readable": list(readable),
            "unreadable": [
                {"point": point.key, "address": point.address, "exception_code": point.code}
                for point in unreadable
            ],
        }
    )


def _modbus_block_reader(
    transport: ByteStreamTransport, slave: int, codec: Any = modbus_rtu
) -> ReadRawMany:
    """整张点表 → 读数键的原始寄存器字节:按块规划,一次请求读尽量多的点。

    功能码由点表项决定(缺省读保持寄存器 0x03;很多电表只实现读输入寄存器 0x04):
    请求发哪个码,应答就按哪个码校,两边共用一个来源,不会一边 03 一边 04。

    帧格式由 codec 给(RTU 缺省;MBAP 传 _MbapLink):块规划、降级、解码都在这一个函数里,
    第二帧格式接入时不另写一套读表逻辑 —— 换的只是收发那几个字节。

    从站用异常应答拒掉一块(常见于合并时顺带读了空隙里没实现的寄存器)时降级:
    把这块拆开逐点重试,能读到的点照旧返回,只有真读不到的点进 ModbusPointsUnreadable。

    降级会把块区间记下来(状态只活在这个驱动实例上,不写进证据也不写清单):下一轮只合并
    真正相邻的点,不再扫那些空隙。冷启动时不知道这台表有没有空洞,所以第一轮总是先试合并。
    batch_fallback 只在第一次降级时写一条 —— 重复记录会把稳态的空洞读成"设备一直在报异常"。
    """
    rejected: List[RejectedSpan] = []

    def read_raw_many(points: Mapping[str, Point]) -> Dict[str, bytes]:
        raw: Dict[str, bytes] = {}
        for block in _plan_read_blocks(points, rejected):
            try:
                payload = _read_registers(
                    codec, transport, slave, block.function, block.address, block.count
                )
            except modbus_rtu.SlaveExceptionResponse as exc:
                if len(block.keys) == 1:
                    raise  # 一个点一块已经拆到最小:同一个请求再发一遍不会变出数据来
                # 只对异常应答降级:那是设备在说"这个地址 / 长度我不认",拆小还有救。
                # 超时与 CRC 错不在此列,它们原样上抛 —— 链路没回话时重试只会更慢,还会盖住真问题。
                first_degradation = not rejected
                rejected.append((block.function, block.address, block.address + block.count))
                readable, unreadable = _retry_point_by_point(
                    codec, transport, slave, block, points
                )
                raw.update(readable)
                if first_degradation:
                    _report_degraded_block(transport, block, exc, readable, unreadable)
                if unreadable:
                    raise ModbusPointsUnreadable(unreadable, list(readable)) from exc
                continue
            for key in block.keys:
                point = points[key]
                start = (point.address - block.address) * 2
                raw[key] = payload[start : start + _register_count(point) * 2]
        return raw

    return read_raw_many


def _modbus_reader(transport: ByteStreamTransport, slave: int, codec: Any) -> ReadRaw:
    """单点路径:一个点表项 = 一次读寄存器请求。

    内部就是"只含这一个点的块",不另写一套解析 —— 单点和块读共用同一段收发代码。
    """
    read_raw_many = _modbus_block_reader(transport, slave, codec)

    def read_raw(point: Point) -> bytes:
        return read_raw_many({point.key: point})[point.key]

    return read_raw


def _modbus_rtu_reader(transport: ByteStreamTransport, slave: int) -> ReadRaw:
    """RTU 单点读取(既有调用方按这个名字取):就是通用单点路径 + RTU 帧格式。"""
    return _modbus_reader(transport, slave, modbus_rtu)


def dlt645_reader(transport: ByteStreamTransport, meter_address: str) -> ReadRaw:
    """一个点表项 = 一次读数据命令:表地址在地址域,点表项 address 是 4 字节数据标识。

    值字节低字节先到,正好对上点表里 byteorder: little —— 和 Modbus 的 BCD 同一个写法,
    通用读取路径不需要知道这是第二个规约。

    电表与终端(terminal_driver)共用这一份:终端上行也是 DL/T645,差的是型号不是帧。
    """

    def read_raw(point: Point) -> bytes:
        transport.send(dlt645.build_read_request(meter_address, point.address))
        # 帧长 = 12 + L,而 L 在应答头里:transport 先收头再按 L 收满剩下的部分。
        frame = transport.receive(timeout=2.0, expected=dlt645.response_length)
        return dlt645.parse_read_response(frame, meter_address, point.address)

    return read_raw


def _modbus_writer(transport: ByteStreamTransport, slave: int, codec: Any) -> WriteRaw:
    """写寄存器:功能码由寄存器个数决定 —— 1 个走 0x06 单寄存器写,更多走 0x10。

    写应答是回显,不是数据:0x06 回显地址 + 值,0x10 回显起始地址 + 寄存器数(没有数据段)。
    值到底生效没有由驱动层的回读判,这里只确认"回的确实是刚才那一帧"。超时原样上抛
    (链路没回话),异常应答抛 SlaveExceptionResponse(设备拒了这一写)—— 两者归因不同。
    帧格式由 codec 给,写路径不因帧格式分叉。
    """

    def write_raw(point: Point, raw: bytes) -> None:
        # 奇数 length(如 3 字节 BCD)补齐到整寄存器:寄存器是 2 字节,补齐的那一个字节
        # 不参与回读比较(见 PointTableDriver.write)。
        registers = raw + b"\x00" * (len(raw) % 2)
        if len(registers) == 2:
            transport.send(
                codec.build_write_single_request(
                    slave, point.address, int.from_bytes(registers, "big")
                )
            )
            frame = transport.receive(timeout=2.0, expected=codec.write_response_length)
            codec.parse_write_response(frame, slave, point.address)
            return
        count = len(registers) // 2
        transport.send(codec.build_write_multiple_request(slave, point.address, registers))
        frame = transport.receive(timeout=2.0, expected=codec.write_response_length)
        codec.parse_write_multiple_response(frame, slave, point.address, count)

    return write_raw


def _modbus_rtu_writer(transport: ByteStreamTransport, slave: int) -> WriteRaw:
    """RTU 写寄存器(既有调用方按这个名字取):通用写路径 + RTU 帧格式。"""
    return _modbus_writer(transport, slave, modbus_rtu)


class _MbapLink:
    """MBAP 上的收发:帧格式全在 protocol/modbus_tcp.py,这里只补上它缺的两维:事务号的生成与比对。

    每次请求 +1(到 0xFFFF 回卷成 0):一个请求对应一个应答,事务号就是这两者的配对凭据,
    所以不需要随机起点。发请求时记下它,解析应答时交上去 —— 事务号对不上就抛
    TransactionMismatch,不自动重发:迟到应答意味着上位机时序已经错了,重发只会把问题藏起来。
    """

    def __init__(self) -> None:
        self._transaction = 0
        # 当前未完成那个请求的事务号;None = 还没有请求发出去过(此时无从比对)。
        self._pending_transaction: int | None = None

    def _next(self) -> int:
        self._transaction = (self._transaction + 1) & 0xFFFF
        self._pending_transaction = self._transaction
        return self._transaction

    def build_read_request(
        self, unit: int, address: int, count: int, function: int = modbus_tcp.READ_HOLDING_REGISTERS
    ) -> bytes:
        return modbus_tcp.build_read_request(self._next(), unit, address, count, function)

    def build_write_single_request(
        self, unit: int, address: int, value: int, function: int = modbus_tcp.WRITE_SINGLE_REGISTER
    ) -> bytes:
        return modbus_tcp.build_write_single_request(self._next(), unit, address, value, function)

    def build_write_multiple_request(
        self,
        unit: int,
        address: int,
        registers: bytes,
        function: int = modbus_tcp.WRITE_MULTIPLE_REGISTERS,
    ) -> bytes:
        return modbus_tcp.build_write_multiple_request(
            self._next(), unit, address, registers, function
        )

    # MBAP 的长度字段对读 / 写 / 异常应答都成立(不像 RTU 要按功能码分两套):
    # 两个名字都指向同一个算路,写路径因此不用知道这个差别。
    response_length = staticmethod(modbus_tcp.response_length)
    write_response_length = staticmethod(modbus_tcp.response_length)

    # 解析应答时带上当前未完成那个请求的事务号:迟到的应答只有事务号能认出来。
    # 三个 parse_* 自己拿 _pending_transaction(keyword-only 必填,想漏都漏不掉),调用方不用改一行。
    def parse_read_response(
        self, frame: bytes, unit: int, function: int = modbus_tcp.READ_HOLDING_REGISTERS
    ) -> bytes:
        return modbus_tcp.parse_read_response(
            frame, unit, function, expected_transaction=self._pending_transaction
        )

    def parse_write_response(
        self,
        frame: bytes,
        unit: int,
        address: int,
        function: int = modbus_tcp.WRITE_SINGLE_REGISTER,
    ) -> int:
        return modbus_tcp.parse_write_response(
            frame, unit, address, function, expected_transaction=self._pending_transaction
        )

    def parse_write_multiple_response(
        self,
        frame: bytes,
        unit: int,
        address: int,
        count: int,
        function: int = modbus_tcp.WRITE_MULTIPLE_REGISTERS,
    ) -> None:
        modbus_tcp.parse_write_multiple_response(
            frame, unit, address, count, function, expected_transaction=self._pending_transaction
        )


@register_device("meter", "modbus_rtu")
def _build_meter(
    device_id: str, spec: dict, model: Model, transport: ByteStreamTransport, criteria: CriteriaProfile
) -> PointTableDriver:
    meter = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(
        meter,
        model,
        transport,
        _modbus_rtu_reader(transport, spec["bus_address"]),
        _modbus_block_reader(transport, spec["bus_address"]),
        write_raw=_modbus_rtu_writer(transport, spec["bus_address"]),
    )


@register_device("meter", "modbus_tcp")
def _build_modbus_tcp_meter(
    device_id: str, spec: dict, model: Model, transport: ByteStreamTransport, criteria: CriteriaProfile
) -> PointTableDriver:
    """Modbus TCP 电表:同一个 PointTableDriver、同一套块读 / 降级,只把帧换成 MBAP。

    bus_address 是 MBAP 头里的单元号(网关后面那些串口从站的地址)。一个 _MbapLink 同时交给
    读、块读、写三条路:事务号因此是一条递增的序列,不会三条路各从 0 开始。
    """
    meter = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    link = _MbapLink()
    return PointTableDriver(
        meter,
        model,
        transport,
        _modbus_reader(transport, spec["bus_address"], link),
        _modbus_block_reader(transport, spec["bus_address"], link),
        write_raw=_modbus_writer(transport, spec["bus_address"], link),
    )


@register_device("meter", "dlt645")
def _build_dlt645_meter(
    device_id: str, spec: dict, model: Model, transport: ByteStreamTransport, criteria: CriteriaProfile
) -> PointTableDriver:
    meter = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(meter, model, transport, dlt645_reader(transport, spec["bus_address"]))
