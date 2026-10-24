"""
一种从站,两种帧:同一份寄存器表、同一套故障注入,既能产出 RTU 帧也能产出 MBAP 帧。

这一层验的是三件事:
1. 两种帧格式**行为一致** —— 同一份点表、同一份寄存器值读出来的物理量必须一模一样,
   写进去的寄存器也一样;所以是同一个用例体跑两种格式,不是各写一套。
2. 帧格式错配**报错可辨认** —— RTU 帧喂给 MBAP 解析(和反过来)不许解出"看着正常"的值。
3. 规约注册 —— `protocol: modbus_tcp` 只放行 `access: tcp`,装得上、读得出,驱动是同一个
   PointTableDriver(通用读取路径一行没抄第二份);`access: serial` 在装配期就拒。
"""
import copy

import pytest

from tests_support.manifest_factory import build
from testplatform.config.manifest import parse_manifest
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import registered_keys
from testplatform.protocol import modbus_rtu, modbus_tcp
from testplatform.transport.simulators import SimulatedModbusSlave

# 同一份点表:两种帧格式共用它。0x0001E240 × 0.01 = 1234.56 kWh,2300 × 0.1 = 230.0 V。
POINT_TABLE = {
    "有功电量": {
        "address": 0,
        "length": 4,
        "encoding": "uint",
        "scale": 0.01,
        "unit": "kWh",
        "writable": True,
    },
    "电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1, "unit": "V"},
    "计量模式": {"address": 3, "length": 2, "encoding": "uint", "writable": True},
}
REGISTERS = {0: 0x0001, 1: 0xE240, 2: 2300, 3: 0}


def _model(protocol: str) -> dict:
    return {
        "type": "meter",
        "protocol": protocol,
        "criteria": "meter_220v",
        "capabilities": ["有功电量", "电压"],
        "point_table": copy.deepcopy(POINT_TABLE),
    }


def _bench(*, rtu_access: str = "serial", tcp_access: str = "tcp", **tcp_connection) -> dict:
    """一份清单里两台电表:同一个型号点表,一台说 RTU、一台说 MBAP。

    两台设备的寄存器表逐字相同,所以"读出来的值一样"不可能是碰巧。
    `tcp_connection` 用来把 MBAP 那一台掰坏(帧格式选错 / 接入方式选错)。
    """
    tcp_connection.setdefault("simulated_registers", dict(REGISTERS))
    tcp_connection.setdefault("simulated_frames", "modbus_tcp")
    return {
        "criteria_profiles": {
            "meter_220v": [
                {"quantity": "电压", "min": 198, "max": 242},
                {"quantity": "有功电量", "min": 0},
            ]
        },
        "models": {
            "dtsd1352_rtu": _model("modbus_rtu"),
            "dtsd1352_mbap": _model("modbus_tcp"),
        },
        "devices": [
            {
                "id": "meter_rtu",
                "model": "dtsd1352_rtu",
                "access": rtu_access,
                "mode": "simulator",
                "bus_address": 1,
                "connection": {"port": "/dev/ttyUSB9", "simulated_registers": dict(REGISTERS)},
            },
            {
                "id": "meter_tcp",
                "model": "dtsd1352_mbap",
                "access": tcp_access,
                "mode": "simulator",
                "bus_address": 1,
                "connection": tcp_connection,
            },
        ],
    }


# ====================== 同一份寄存器表:两种帧格式行为一致 ======================

def test_the_two_frame_formats_read_the_same_point_table(tmp_path):
    """同一个驱动、同一份点表、同一份寄存器:RTU 与 MBAP 读出来的物理量逐字相同。"""
    root = build(tmp_path, _bench())
    meter_rtu, meter_tcp = root.get("meter_rtu"), root.get("meter_tcp")
    try:
        # 通用读取路径被复用:两台设备交出来的是同一个驱动类,不是为 MBAP 另写的一份。
        assert type(meter_tcp) is PointTableDriver
        assert type(meter_tcp) is type(meter_rtu)

        meter_rtu.connect()
        meter_tcp.connect()
        reading_rtu = meter_rtu.read_all()
        reading_tcp = meter_tcp.read_all()
    finally:
        root.teardown()

    assert reading_tcp.readings == reading_rtu.readings
    assert reading_tcp.readings == {"有功电量": 1234.56, "电压": 230.0, "计量模式": 0.0}


def test_the_two_frame_formats_write_the_same_register(tmp_path):
    """写路径同样只差帧头:同一句 write 在两种格式下都写进寄存器,回读到同一个值。"""
    root = build(tmp_path, _bench())
    meter_rtu, meter_tcp = root.get("meter_rtu"), root.get("meter_tcp")
    try:
        meter_rtu.connect()
        meter_tcp.connect()
        written_rtu = meter_rtu.write("计量模式", 1)
        written_tcp = meter_tcp.write("计量模式", 1)
        read_back_tcp = meter_tcp.read("计量模式")
    finally:
        root.teardown()

    assert written_rtu == written_tcp == 1.0
    assert read_back_tcp == 1.0


def test_a_two_register_write_takes_the_same_write_multiple_path(tmp_path):
    """4 字节的点写下去是两个寄存器,所以走 0x10:两种帧格式同一条路,回读到同一个值。"""
    root = build(tmp_path, _bench())
    meter_rtu, meter_tcp = root.get("meter_rtu"), root.get("meter_tcp")
    try:
        meter_rtu.connect()
        meter_tcp.connect()
        written_rtu = meter_rtu.write("有功电量", 4321.0)
        written_tcp = meter_tcp.write("有功电量", 4321.0)
        read_back_tcp = meter_tcp.read("有功电量")
    finally:
        root.teardown()

    assert written_rtu == pytest.approx(4321.0)
    assert written_tcp == pytest.approx(4321.0)
    assert read_back_tcp == pytest.approx(4321.0)


def test_a_mbap_driver_reading_a_simulator_that_speaks_rtu_fails_loudly(tmp_path):
    """帧格式写错(清单说 MBAP、模拟从站仍出 RTU)必须报错:RTU 帧的 CRC 校验过不去。"""
    root = build(tmp_path, _bench(simulated_frames="modbus_rtu"))
    meter = root.get("meter_tcp")
    try:
        meter.connect()
        with pytest.raises(ValueError) as caught:
            meter.read_all()
    finally:
        root.teardown()

    assert "CRC" in str(caught.value)


# ====================== 同一套故障注入:两种帧格式回同一个异常码 ======================

# 写一个没声明过的寄存器地址:两种格式的请求帧与"被拒"的应答帧各一份字节向量。
REFUSED_WRITE = {
    "modbus_rtu": (
        modbus_rtu.build_write_single_request(1, 9, 5),
        modbus_rtu.build_exception_response(1, modbus_rtu.ILLEGAL_DATA_ADDRESS, 0x06),
    ),
    "modbus_tcp": (
        modbus_tcp.build_write_single_request(7, 1, 9, 5),
        modbus_tcp.build_exception_response(7, 1, modbus_rtu.ILLEGAL_DATA_ADDRESS, 0x06),
    ),
}

# 写多个寄存器、地址不在表里:同样的拒绝,异常码 02。
REFUSED_MULTI_WRITE = {
    "modbus_rtu": (
        modbus_rtu.build_write_multiple_request(1, 9, bytes.fromhex("000a000b")),
        modbus_rtu.build_exception_response(1, modbus_rtu.ILLEGAL_DATA_ADDRESS, 0x10),
    ),
    "modbus_tcp": (
        modbus_tcp.build_write_multiple_request(8, 1, 9, bytes.fromhex("000a000b")),
        modbus_tcp.build_exception_response(8, 1, modbus_rtu.ILLEGAL_DATA_ADDRESS, 0x10),
    ),
}


@pytest.mark.parametrize("frames", sorted(REFUSED_WRITE))
def test_write_to_an_undeclared_register_is_refused_in_both_frame_formats(frames):
    """故障注入只有一份:两种帧格式对着同一份拒绝规则,各回自己的异常应答帧。"""
    request, expected = REFUSED_WRITE[frames]
    slave = SimulatedModbusSlave({3: 0}, frames=frames)
    slave.connect()

    slave.send(request)

    assert slave.receive() == expected


@pytest.mark.parametrize("frames", sorted(REFUSED_MULTI_WRITE))
def test_write_multiple_to_an_undeclared_register_is_refused_in_both_frame_formats(frames):
    request, expected = REFUSED_MULTI_WRITE[frames]
    slave = SimulatedModbusSlave({3: 0}, frames=frames)
    slave.connect()

    slave.send(request)

    assert slave.receive() == expected


# 写成功那一条:两种帧格式写同一个寄存器、回读同一个值。
VALID_WRITE = {
    "modbus_rtu": (
        modbus_rtu.build_write_single_request(1, 3, 5),
        modbus_rtu.build_read_request(1, 3, 1),
        lambda frame: int.from_bytes(modbus_rtu.parse_read_response(frame, 1), "big"),
    ),
    "modbus_tcp": (
        modbus_tcp.build_write_single_request(7, 1, 3, 5),
        modbus_tcp.build_read_request(7, 1, 3, 1),
        lambda frame: int.from_bytes(
            modbus_tcp.parse_read_response(frame, 1, expected_transaction=7), "big"
        ),
    ),
}


@pytest.mark.parametrize("frames", sorted(VALID_WRITE))
def test_a_valid_write_reaches_the_same_register_in_both_frame_formats(frames):
    """写成功那一条也一样:值进同一张表,应答是请求帧的回显(不回数据),回读能读到。"""
    request, read_request, decode = VALID_WRITE[frames]
    slave = SimulatedModbusSlave({3: 0}, frames=frames)
    slave.connect()

    slave.send(request)

    assert slave.receive() == request, "0x06 的应答就是请求帧原样回显"
    slave.send(read_request)
    assert decode(slave.receive()) == 5


# ====================== 帧格式错配:报错可辨认,不静默解出值 ======================

def test_an_rtu_frame_fed_to_the_mbap_simulator_is_named_as_such():
    """RTU 帧喂给 MBAP 从站:报错点明是 RTU 帧,而不是解出一帧假应答。"""
    slave = SimulatedModbusSlave({0: 1}, frames="modbus_tcp")
    slave.connect()

    slave.send(modbus_rtu.build_read_request(1, 0, 1))

    with pytest.raises(ValueError) as caught:
        slave.receive()
    assert "RTU" in str(caught.value)


def test_a_mbap_frame_fed_to_the_rtu_simulator_is_refused():
    """反过来也一样:MBAP 帧没有 CRC,RTU 从站的第一步校验就过不去。"""
    slave = SimulatedModbusSlave({0: 1}, frames="modbus_rtu")
    slave.connect()

    slave.send(modbus_tcp.build_read_request(1, 1, 0, 1))

    with pytest.raises(ValueError) as caught:
        slave.receive()
    assert "CRC" in str(caught.value)


def test_an_unknown_frame_format_is_refused_at_construction():
    """帧格式只有两个取值:写错了在造模拟从站时就报,并列出可用的。"""
    with pytest.raises(ValueError) as caught:
        SimulatedModbusSlave({0: 1}, frames="modbus_tcp_rtu")

    message = str(caught.value)
    assert "modbus_rtu" in message and "modbus_tcp" in message


# ============================ 规约注册与接入方式 ============================

def test_modbus_tcp_assembles_on_tcp(tmp_path):
    """清单里写 protocol: modbus_tcp + access: tcp 能装配 —— 与 RTU 读同一份点表。"""
    root = build(tmp_path, _bench())
    try:
        assert type(root.get("meter_tcp")) is PointTableDriver
        assert root.reading_keys_of("meter_tcp") == tuple(POINT_TABLE)
    finally:
        root.teardown()


@pytest.mark.parametrize("access", ["serial", "gateway_ssh"])
def test_modbus_tcp_on_a_serial_link_is_refused_before_assembly(access):
    """MBAP 帧没有 CRC、靠 TCP 保证字节完整:串口上跑不了,装配期就拒。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_bench(tcp_access=access))

    message = str(caught.value)
    assert "modbus_tcp" in message
    assert access in message
    assert "tcp" in message  # 报错要说出它允许哪一种接入方式


def test_modbus_tcp_is_registered_as_a_meter_protocol():
    assert ("meter", "modbus_tcp") in registered_keys()
