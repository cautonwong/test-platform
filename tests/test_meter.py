"""
电表:点表驱动的通用读取路径 + Modbus RTU 编解码,端到端一条完整的链。

    读数键("有功电量") → 点表项(地址/长度/编码/倍率/字节序/字序/功能码) → Modbus RTU 帧
    → 物理量 → 判据集判定 → 合格/不合格

用例里不出现寄存器地址、也不出现 CRC:地址在型号的点表里,帧在 protocol/ 里。
结构和 test_inverter.py / test_battery.py 一样 —— 写 contract 的用例会在收集阶段
自动对 configs/devices.yaml 里每一台电表各展开一次。
"""
import copy
import json
import time
from pathlib import Path

import pytest
import yaml

from tests_support.manifest_factory import build, manifest
from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.config.manifest import parse_manifest
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.drivers import meter_driver
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.protocol import modbus_rtu
from testplatform.reporting.evidence import EvidenceSession
from testplatform.transport.simulators import SimulatedModbusSlave

BASE_MANIFEST = manifest(meters=["meter_a"])


def _build(tmp_path, manifest):
    return build(tmp_path, manifest)


@pytest.fixture()
def two_meters(tmp_path):
    """同一个型号、同一份点表,只有模拟从站里的寄存器值不同:230.0V 与 250.0V。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    out_of_range = copy.deepcopy(manifest["devices"][0])
    out_of_range["id"] = "meter_b"
    out_of_range["bus_address"] = 2
    out_of_range["connection"]["simulated_registers"] = {0: 0x0001, 1: 0xE240, 2: 2500}
    manifest["devices"].append(out_of_range)

    root = _build(tmp_path, manifest)
    yield root
    root.teardown()


# ========================= 契约测试:对每一台电表都要成立 =========================

@case(id="M_00_00_01_000002_01", clause="DLT645-4.5.1", tags=["一致性", "通信"])
@pytest.mark.contract
def test_meter_connects(meter):
    meter.connect()
    assert meter.device.state.name == "CONNECTED"


@case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2", tags=["一致性", "读数", "验收"])
@pytest.mark.contract
def test_meter_reads_physical_value_by_reading_key(meter):
    """读数键进、物理量出:用例不知道 Address 0 是两个寄存器、倍率是 0.01。"""
    meter.connect()

    energy = meter.read("有功电量")
    voltage = meter.read("电压")

    # configs/devices.yaml 里模拟从站的寄存器:0x0001E240 × 0.01 / 2300 × 0.1
    assert energy == pytest.approx(1234.56)
    assert voltage == pytest.approx(230.0)


@case(id="M_00_00_01_000002_02", clause="DLT645-4.5.4", tags=["一致性", "记录", "验收"])
@pytest.mark.contract
def test_meter_reading_is_recorded_in_history(meter):
    meter.connect()
    before = meter.device.readings_recorded

    meter.read_all()

    assert meter.device.readings_recorded == before + 1
    assert meter.device.latest_reading.readings["有功电量"] == pytest.approx(1234.56)


@case(id="M_00_00_01_000002_03", clause="DLT645-4.5.5", tags=["一致性", "读数"])
@pytest.mark.contract
def test_meter_unknown_reading_key_names_the_available_ones(meter):
    """读数键写错要报出可用键,而不是一句 KeyError。"""
    meter.connect()

    with pytest.raises(KeyError) as excinfo:
        meter.read("无功电量")

    assert "有功电量" in str(excinfo.value)


# ========================= 判据集:同一个读数,两种结论 =========================

def test_same_reading_passes_or_fails_by_criteria_profile(two_meters):
    """同一个读数键(电压)、同一个型号、同一份判据集:
    230.0V 在 198–242 内判合格,250.0V 超出上限判不合格,并说清超出多少。"""
    healthy = two_meters.get("meter_a")
    faulty = two_meters.get("meter_b")
    for driver in (healthy, faulty):
        driver.connect()

    passed = healthy.read_all()
    failed = faulty.read_all()

    assert passed.readings["电压"] == pytest.approx(230.0)
    assert passed.passed is True

    assert failed.readings["电压"] == pytest.approx(250.0)
    assert failed.passed is False
    assert [(b.quantity, round(b.exceeded_by, 6)) for b in failed.breaches] == [("电压", 8.0)]


# ========================= 同规约新型号:只改 yaml,零 Python =========================

def test_new_model_of_the_same_protocol_needs_only_yaml(tmp_path):
    """换一个同规约型号(不同寄存器地址、不同编码 bcd/int、不同倍率):
    只在清单里加一段 models,平台里没有一行新代码。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["models"]["dtsd1352_bcd"] = {
        "type": "meter",
        "protocol": "modbus_rtu",
        "criteria": "meter_220v",
        "capabilities": ["有功电量", "电压"],
        "point_table": {
            "有功电量": {
                "address": 10,
                "length": 4,
                "encoding": "bcd",
                "scale": 0.01,
                "byteorder": "little",
                "unit": "kWh",
            },
            "电压": {"address": 12, "length": 2, "encoding": "int", "scale": 0.1, "unit": "V"},
        },
    }
    manifest["devices"].append(
        {
            "id": "meter_bcd",
            "model": "dtsd1352_bcd",
            "access": "serial",
            "mode": "simulator",
            "bus_address": 3,
            "connection": {
                "port": "/dev/ttyUSB9",
                "baudrate": 9600,
                # BCD 低字节先发:34 12 00 00 → 倒过来是 00001234 → ×0.01 = 12.34 kWh
                "simulated_registers": {10: 0x3412, 11: 0x0000, 12: 2300},
            },
        }
    )
    root = _build(tmp_path, manifest)
    try:
        meter = root.get("meter_bcd")
        meter.connect()

        assert meter.read("有功电量") == pytest.approx(12.34)
        assert meter.read("电压") == pytest.approx(230.0)
    finally:
        root.teardown()


# =================== 点表两项:功能码与字序 ===================

class _RecordingModbusSlave(SimulatedModbusSlave):
    """进程内从站加一行记录:把发出去的请求帧留下来。

    从站对 03 / 04 回同一份寄存器(它不需要区分),所以"到底发了哪个功能码"
    只能看帧本身 —— 帧留在传输层,不进驱动。
    """

    def __init__(self, registers):
        super().__init__(registers)
        self.requests: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.requests.append(data)
        super().send(data)


def test_two_points_send_their_own_function_codes():
    """同一份点表里 0x03 与 0x04 并存:帧里的功能码各自正确,应答各按自己的码校、解析。

    很多电表类设备只实现读输入寄存器(0x04),逆变器常用读保持寄存器(0x03)。
    不写 function 的点仍是 0x03 —— 和改动前发出的帧逐字节相同。
    """
    model = parse_manifest(
        {
            "models": {
                "dtsd1352_two_codes": {
                    "type": "meter",
                    "protocol": "modbus_rtu",
                    "capabilities": ["有功电量", "输入电压"],
                    "point_table": {
                        "有功电量": {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01},
                        "输入电压": {
                            "address": 10,
                            "length": 2,
                            "encoding": "uint",
                            "scale": 0.1,
                            "function": "read_input_registers",
                        },
                    },
                }
            }
        }
    ).models["dtsd1352_two_codes"]
    slave = _RecordingModbusSlave({0: 0x0001, 1: 0xE240, 10: 2300})
    driver = PointTableDriver(
        Meter("meter_two_codes", "meter_two_codes", CriteriaProfile(name="none")),
        model,
        slave,
        meter_driver._modbus_rtu_reader(slave, 7),
    )

    driver.connect()
    reading = driver.read_all()

    assert [frame.hex() for frame in slave.requests] == [
        "070300000002c46d",  # 不写 function:0x03,与改动前的同一向量逐字节相同
        "0704000a000111ae",  # function: read_input_registers → 0x04
    ]
    assert reading.readings["有功电量"] == pytest.approx(1234.56)
    assert reading.readings["输入电压"] == pytest.approx(230.0)


def test_word_order_little_assembles_the_words_in_the_other_order(tmp_path):
    """同一串寄存器字节、同一个读数地址,只换字序 → 两个完全不同的物理量。

    0x0001E240:高字先是 1234.56 kWh;低字先是 0xE2400001 × 0.01 —— 差四个数量级,
    但两边都像"一个正常电量",这就是只认 byteorder 时最难查的那种错。
    """
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["models"]["dtsd1352_word_order"] = {
        "type": "meter",
        "protocol": "modbus_rtu",
        "criteria": "meter_220v",
        "capabilities": ["有功电量", "反向有功电量", "电压"],
        "point_table": {
            "有功电量": {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01},
            "电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1},
            "反向有功电量": {
                "address": 0,
                "length": 4,
                "encoding": "uint",
                "scale": 0.01,
                "word_order": "little",
            },
        },
    }
    manifest["devices"].append(
        {
            "id": "meter_word_order",
            "model": "dtsd1352_word_order",
            "access": "serial",
            "mode": "simulator",
            "bus_address": 4,
            "connection": {
                "port": "/dev/ttyUSB9",
                "baudrate": 9600,
                "simulated_registers": {0: 0x0001, 1: 0xE240, 2: 2300},
            },
        }
    )
    root = _build(tmp_path, manifest)
    try:
        meter = root.get("meter_word_order")
        meter.connect()

        assert meter.read("有功电量") == pytest.approx(1234.56)
        assert meter.read("反向有功电量") == pytest.approx(37958451.21)
    finally:
        root.teardown()


# ========================= 清单校验:装配前报错 =========================

def test_meter_without_bus_address_fails_before_assembly(tmp_path):
    """Modbus RTU 的请求帧要带从站地址,缺了就是清单写错了,不能等到读数的时侯才炸。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    del manifest["devices"][0]["bus_address"]

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "bus_address" in str(excinfo.value)
    assert "meter_a" in str(excinfo.value)


# =================== 块规划:地址相邻的点合并成一次请求 ===================
#
# 一个点一次请求在一台 20 点的电表上就是 20 次往返(9600 波特下每次十几毫秒),
# 而且真总线上每多发一帧就多一次抢占与帧错位的机会。合并只是"少发几次请求":
# 一次读数仍然是一次 record_reading(全部点),判据仍一次看全所有物理量。


def _points(count, base=0, **fields):
    """count 个地址连续的点,每个点 length=2(一个寄存器)。"""
    return {
        f"点{index:03d}": {"address": base + index, "length": 2, "encoding": "uint", **fields}
        for index in range(count)
    }


def _batch_model(point_table):
    return parse_manifest(
        {
            "models": {
                "批量电表": {
                    "type": "meter",
                    "protocol": "modbus_rtu",
                    "capabilities": [next(iter(point_table))],
                    "point_table": point_table,
                }
            }
        }
    ).models["批量电表"]


def _batch_driver(model, slave, batched=True):
    """batched=False 就是合并前的行为:一个点表项一次请求。"""
    return PointTableDriver(
        Meter("批量电表", "批量电表", CriteriaProfile(name="none")),
        model,
        slave,
        meter_driver._modbus_rtu_reader(slave, 1),
        **({"read_raw_many": meter_driver._modbus_block_reader(slave, 1)} if batched else {}),
    )


def _requests_of(model, registers, batched=True):
    """把总线上发出去的请求帧解析成 (从站, 功能码, 起始寄存器, 寄存器数)。"""
    slave = _RecordingModbusSlave(registers)
    driver = _batch_driver(model, slave, batched)
    driver.connect()
    driver.read_all()
    return [modbus_rtu.parse_read_request(frame) for frame in slave.requests]


def test_twenty_adjacent_points_cost_one_request_instead_of_twenty():
    """20 个地址连续的点:合并前 20 帧(一个点一次),合并后 1 帧。

    省下的不只是 19 帧本身,而是 19 次往返和 19 次"多发一帧就可能错位"的机会。
    """
    model = _batch_model(_points(20))
    registers = {index: index for index in range(20)}

    before = _requests_of(model, registers, batched=False)
    after = _requests_of(model, registers)

    assert len(before) == 20, f"合并前该是 20 帧(一个点一次),实际 {len(before)} 帧"
    assert len(after) == 1, f"合并后 20 个点该在 1 帧里,实际 {len(after)} 帧"
    assert after[0][1:] == (modbus_rtu.READ_HOLDING_REGISTERS, 0, 20)


def test_points_further_apart_than_the_gap_threshold_are_not_merged():
    """空隙远大于阈值就不合并:合并会把大量没人要的寄存器也读回来,还要多收一段应答。"""
    spacing = meter_driver.MAX_GAP_TO_MERGE + 40
    table = {
        f"点{index}": {"address": index * spacing, "length": 2, "encoding": "uint"}
        for index in range(3)
    }

    requests = _requests_of(_batch_model(table), {index * spacing: index for index in range(3)})

    assert [(function, address, count) for _, function, address, count in requests] == [
        (modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),
        (modbus_rtu.READ_HOLDING_REGISTERS, spacing, 1),
        (modbus_rtu.READ_HOLDING_REGISTERS, 2 * spacing, 1),
    ]


def test_a_gap_exactly_at_the_threshold_is_still_merged():
    """阈值是闭区间(空隙 <= MAX_GAP_TO_MERGE 就合并):边界上的点仍算相邻。"""
    gap = meter_driver.MAX_GAP_TO_MERGE
    table = {
        "前": {"address": 0, "length": 2, "encoding": "uint"},
        "后": {"address": 1 + gap, "length": 2, "encoding": "uint"},
    }

    blocks = meter_driver._plan_read_blocks(_batch_model(table).point_table)

    assert [(block.address, block.count, block.keys) for block in blocks] == [
        (0, 1 + gap + 1, ["前", "后"])
    ]


def test_more_points_than_the_request_cap_are_split_into_several_requests():
    """300 个连续点超过单次上限:自动切成 120 / 120 / 60,没有一帧超上限。"""
    model = _batch_model(_points(300))

    requests = _requests_of(model, {index: index for index in range(300)})

    assert [(address, count) for _, _, address, count in requests] == [(0, 120), (120, 120), (240, 60)]
    assert all(count <= meter_driver.MAX_REGISTERS_PER_REQUEST for *_, count in requests)
    # 上限取 120 而不是 125:协议 PDU 上限 125 个寄存器,留 5 个余量给"实现更严的从站"。
    assert meter_driver.MAX_REGISTERS_PER_REQUEST <= 125


def test_points_of_different_function_codes_are_never_merged():
    """0x03 与 0x04 是两次不同的请求:地址再相邻也各自成块。

    这里四点在地址上完全连续(0/1/2/3),唯一的分界线是功能码 ——
    跨码合并会让一半的点按错的功能码去解析应答。
    """
    table = {
        "保持0": {"address": 0, "length": 2, "encoding": "uint"},
        "保持1": {"address": 1, "length": 2, "encoding": "uint"},
        "输入2": {
            "address": 2,
            "length": 2,
            "encoding": "uint",
            "function": "read_input_registers",
        },
        "输入3": {
            "address": 3,
            "length": 2,
            "encoding": "uint",
            "function": "read_input_registers",
        },
    }

    requests = _requests_of(_batch_model(table), {index: index for index in range(4)})

    assert [(function, address, count) for _, function, address, count in requests] == [
        (modbus_rtu.READ_HOLDING_REGISTERS, 0, 2),
        (modbus_rtu.READ_INPUT_REGISTERS, 2, 2),
    ]


def test_single_point_read_path_is_still_one_request():
    """单点路径没退化:一个点表项进来,只发一帧、只读它自己那几个寄存器。"""
    model = _batch_model(_points(3))
    slave = _RecordingModbusSlave({0: 11, 1: 22, 2: 2300})

    raw = meter_driver._modbus_rtu_reader(slave, 1)(model.point_table["点002"])

    assert [modbus_rtu.parse_read_request(frame) for frame in slave.requests] == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1)
    ]
    assert raw == (2300).to_bytes(2, "big")


def test_reading_one_key_reads_only_that_point_and_records_nothing():
    """按读数键取一个量:只读那一个点、只解那一个量,不进 history。

    取值不是测量:一次可判的读数(全部点 + 判据结论 + 证据)只由 read_all() 出,判据因此
    不会在"只有一半量的读数"上判合格。这条用例锁住这层语义 —— 别把"顺带记录"加回来。"""
    model = _batch_model(_points(3))
    slave = _RecordingModbusSlave({0: 11, 1: 22, 2: 2300})
    driver = _batch_driver(model, slave)
    driver.connect()

    assert driver.read("点002") == 2300.0
    assert _frames_of(slave) == [(1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1)]
    assert driver.device.history == ()


def test_three_single_reads_on_a_three_point_table_cost_three_frames():
    """3 点表连续读三个量 = 3 帧(修复前 9 帧:每次单读都顺带读整张表)。

    断帧数不断耗时:慢机器上的墙钟抖动会让耗时断言假绿。
    """
    model = _batch_model(_points(3))
    slave = _RecordingModbusSlave({0: 11, 1: 22, 2: 2300})
    # batched=False 是逐点读规约的形状(vendor_ascii / DL/T645):一个点一帧,没有块读可合并
    driver = _batch_driver(model, slave, batched=False)
    driver.connect()

    assert [driver.read(key) for key in ("点000", "点001", "点002")] == [11.0, 22.0, 2300.0]

    assert _frames_of(slave) == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 1, 1),
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1),
    ]
    assert driver.device.history == ()


# ============== 合并的收益:慢总线上少一次往返就少一段墙钟 ==============

class _SlowModbusSlave(_RecordingModbusSlave):
    """每次往返固定睡 delay 秒。9600 波特下一次请求的往返就是这个量级(十几毫秒),
    放大到 20 ms 才好在用例里观测合并前后的差别。"""

    def __init__(self, registers, delay):
        super().__init__(registers)
        self._delay = delay

    def receive(self, timeout=2.0, expected=None):
        time.sleep(self._delay)
        return super().receive(timeout=timeout, expected=expected)


def test_batching_cuts_the_wall_clock_of_one_device_read():
    """同一张 20 点表、同一个 20 ms 往返:逐点读 20 次往返,块读 1 次。

    单台设备 ~400 ms → ~20 ms;265 台就是几十秒的差别(总线上还不能并行)。
    """
    delay = 0.02
    model = _batch_model(_points(20))
    registers = {index: index for index in range(20)}

    def read(batched=True):
        slave = _SlowModbusSlave(registers, delay)
        driver = _batch_driver(model, slave, batched)
        driver.connect()
        started = time.monotonic()
        driver.read_all()
        return time.monotonic() - started

    per_point = read(batched=False)
    batched = read()

    assert per_point > 20 * delay * 0.8, f"20 次往返该 >300 ms,实际 {per_point * 1000:.0f} ms"
    assert batched < 5 * delay, f"1 次往返该 <100 ms,实际 {batched * 1000:.0f} ms"


# ============== 块读被拒时降级:异常应答 → 逐点重试 ==============
#
# 合并会在空隙里顺带读"没人要的寄存器":真实电表对没实现的地址回异常码 03(非法数据值)
# 或 02(非法数据地址),整帧被拒 —— 于是这一块里本来读得到的点也一起没了,
# 比逐点读更差。降级就是把这块拆开逐点重试:异常应答才拆,超时不拆。

class _GapAwareModbusSlave(_RecordingModbusSlave):
    """假从站,模拟真机:unimplemented 里的地址一旦被请求(哪怕只是顺带扫到),整帧回异常应答。"""

    def __init__(self, registers, unimplemented=(), code=modbus_rtu.ILLEGAL_DATA_VALUE):
        super().__init__(registers)
        self._unimplemented = set(unimplemented)
        self._code = code

    def receive(self, timeout=2.0, expected=None):
        slave, function, address, count = modbus_rtu.parse_read_request(self._last_sent)
        if any(addr in self._unimplemented for addr in range(address, address + count)):
            return modbus_rtu.build_exception_response(slave, self._code, function)
        return super().receive(timeout=timeout, expected=expected)


class _SilentModbusSlave(_RecordingModbusSlave):
    """链路在、从站不回话(线序 / 波特率不对):收帧超时,一帧应答都没有。"""

    def receive(self, timeout=2.0, expected=None):
        raise TimeoutError("假从站不回")


def _gap_model():
    """两个点隔着一个没人要的寄存器(地址 1):块读会顺带扫到它,逐点读不会。"""
    return _batch_model(
        {
            "有功电量": {"address": 0, "length": 2, "encoding": "uint", "scale": 0.01},
            "电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1},
        }
    )


def _session_records(session, device_id="批量电表"):
    path = session.path_of(device_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _frames_of(slave):
    """总线上已经发出去的所有请求帧,解析成 (从站, 功能码, 起始寄存器, 寄存器数)。"""
    return [modbus_rtu.parse_read_request(frame) for frame in slave.requests]


def test_a_block_rejected_with_exception_03_is_retried_point_by_point():
    """块读(0 起 3 个寄存器)被异常码 03 拒掉后拆开逐点重试:两个点都读到,读数照常交出。

    这就是真机上的主场景:空隙里的地址没实现,点表里的点全都实现 ——
    不降级则整块失败,两个点一个都拿不到。
    """
    model = _gap_model()
    # 空隙地址 1 没实现;两个点自己的地址都在
    slave = _GapAwareModbusSlave({0: 11, 2: 2300}, unimplemented={1})
    driver = _batch_driver(model, slave)
    driver.connect()

    reading = driver.read_all()

    assert reading.readings == {"有功电量": pytest.approx(0.11), "电压": pytest.approx(230.0)}
    assert driver.device.readings_recorded == 1, "一次读数仍是一次 record_reading(全部点)"
    assert [modbus_rtu.parse_read_request(frame) for frame in slave.requests] == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 3),  # 先试合并的块,被拒
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),  # 降级:逐点重试,只碰点表里真有的点
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1),
    ], "降级后只重试点表里的点,空隙地址不再碰"


def test_points_still_unreadable_after_the_retry_fail_with_their_exception_code():
    """降级重试后还有点读不到:读不到就是失败(不用默认值、不拿上一次的值顶上),
    但失败要失败得具体 —— 谁读到了、谁没读到、从站给的异常码是什么。"""
    model = _gap_model()
    # 空隙地址 1 与「电压」的地址 2 都没实现:块读被拒,逐点重试时电压也读不到
    slave = _GapAwareModbusSlave({0: 11}, unimplemented={1, 2})
    driver = _batch_driver(model, slave)
    driver.connect()

    with pytest.raises(meter_driver.ModbusPointsUnreadable) as excinfo:
        driver.read_all()

    failure = excinfo.value
    assert [(point.key, point.address, point.code) for point in failure.failures] == [
        ("电压", 2, modbus_rtu.ILLEGAL_DATA_VALUE)
    ]
    assert failure.readable_keys == ("有功电量",), "读到的点不能跟着一起丢,要能在失败里看出来"
    assert "电压(地址 2, 异常码 0x03)" in str(failure)
    assert driver.device.latest_reading is None, "缺点的读数不许记成一条读数(判据会缺量)"


def test_the_degraded_block_is_recorded_in_the_evidence(tmp_path):
    """降级要在证据里留痕:这条记录解释"这次读数为什么突然多发了几帧"。"""
    session = EvidenceSession(tmp_path / "evidence")
    session.open_device("批量电表")
    slave = _GapAwareModbusSlave({0: 11, 2: 2300}, unimplemented={1})
    driver = _batch_driver(_gap_model(), session.wrap("批量电表", slave))
    driver.connect()
    try:
        driver.read_all()
    finally:
        session.close()

    fallbacks = [r for r in _session_records(session) if r["kind"] == "batch_fallback"]
    assert len(fallbacks) == 1, f"降级该留一条记录,实际 {len(fallbacks)} 条"
    record = fallbacks[0]
    assert {key: record[key] for key in ("function", "address", "count", "exception_code")} == {
        "function": modbus_rtu.READ_HOLDING_REGISTERS,
        "address": 0,
        "count": 3,
        "exception_code": modbus_rtu.ILLEGAL_DATA_VALUE,
    }
    assert record["readable"] == ["有功电量", "电压"]
    assert record["unreadable"] == []


def test_a_rejected_block_is_remembered_so_the_next_read_stops_scanning_the_gap():
    """降级不白付:被拒过的块记在驱动实例上,下一轮只读点表里真有的点,不再扫空隙。

    真机电表的寄存器地址几乎总有空洞,所以这不是偶发的兜底路径,而是稳态:
    两个点 + 一个空洞,合并要 3 次往返,不合并只需 2 次。
    """
    model = _gap_model()
    slave = _GapAwareModbusSlave({0: 11, 2: 2300}, unimplemented={1})
    driver = _batch_driver(model, slave)
    driver.connect()

    first = driver.read_all()
    first_frames = _frames_of(slave)
    second = driver.read_all()

    assert first.readings == second.readings == {
        "有功电量": pytest.approx(0.11),
        "电压": pytest.approx(230.0),
    }
    assert first_frames == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 3),  # 冷启动不知道有空洞:先试合并
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),  # 被拒:降级逐点
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1),
    ]
    assert _frames_of(slave)[len(first_frames) :] == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 1),  # 被拒过的空隙不再扫:2 个点 2 次往返
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 2, 1),
    ], "第二轮不该再出现被拒的块"


def test_adjacent_points_next_to_a_rejected_gap_still_merge():
    """降级不是"全局关闭块读":只是不再扫被拒过的空隙,真正相邻的点照旧合并。"""
    table = {
        name: {"address": address, "length": 2, "encoding": "uint"}
        for name, address in (("点A", 0), ("点B", 1), ("点C", 4), ("点D", 5))
    }
    slave = _GapAwareModbusSlave(
        {0: 1, 1: 2, 4: 3, 5: 4}, unimplemented={2, 3}  # 地址 2、3 是没人要的空隙
    )
    driver = _batch_driver(_batch_model(table), slave)
    driver.connect()

    driver.read_all()
    first_frames = _frames_of(slave)
    reading = driver.read_all()

    assert first_frames[0] == (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 6), "先试合并的块"
    assert reading.readings == {"点A": 1.0, "点B": 2.0, "点C": 3.0, "点D": 4.0}
    assert _frames_of(slave)[len(first_frames) :] == [
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 0, 2),  # A、B 相邻,仍合并
        (1, modbus_rtu.READ_HOLDING_REGISTERS, 4, 2),  # C、D 相邻,仍合并
    ], "4 个点 2 次往返:空洞不再扫,相邻的仍合并"


def test_the_fallback_record_is_written_once_not_once_per_read_cycle(tmp_path):
    """`batch_fallback` 只在第一次降级时写一条:一轮 1440 次读写 1440 条会被读成"设备一直在报异常"。"""
    session = EvidenceSession(tmp_path / "evidence")
    session.open_device("批量电表")
    slave = _GapAwareModbusSlave({0: 11, 2: 2300}, unimplemented={1})
    driver = _batch_driver(_gap_model(), session.wrap("批量电表", slave))
    driver.connect()
    try:
        driver.read_all()
        driver.read_all()
    finally:
        session.close()

    records = _session_records(session)
    assert len([r for r in records if r["kind"] == "batch_fallback"]) == 1
    sends = [r for r in records if r["kind"] == "frame" and r["direction"] == "send"]
    assert len(sends) == 3 + 2, f"第一轮 3 帧、第二轮 2 帧,实际 {len(sends)} 帧"


def test_a_timeout_does_not_degrade_to_point_by_point_retries(tmp_path):
    """超时是链路问题(没回话),不是"这个地址我不认":不降级。

    重试只会把 2 秒的超时乘以点数,还会把"线断了"伪装成"某个点读不到"。
    这里数的是发出去的帧:超时后只有那一帧块读,没有逐点重试的帧。
    """
    session = EvidenceSession(tmp_path / "evidence")
    session.open_device("批量电表")
    slave = _SilentModbusSlave({0: 11, 2: 2300})
    driver = _batch_driver(_gap_model(), session.wrap("批量电表", slave))
    driver.connect()

    with pytest.raises(TimeoutError):
        driver.read_all()
    session.close()

    assert len(slave.requests) == 1, f"超时后不该再发请求,实际发了 {len(slave.requests)} 帧"
    records = _session_records(session)
    assert [r["kind"] for r in records if r["kind"] == "batch_fallback"] == [], "超时不是降级"
    sends = [r for r in records if r["kind"] == "frame" and r["direction"] == "send"]
    assert len(sends) == 1 and records[-1]["error"] == "timeout"


def test_a_healthy_device_never_writes_a_fallback_record(tmp_path):
    """全正常的设备:行为与修复前逐字节一致 —— 一帧块读、两条帧记录、没有降级记录。"""
    session = EvidenceSession(tmp_path / "evidence")
    session.open_device("批量电表")
    model = _batch_model(_points(20))
    slave = _RecordingModbusSlave({index: index for index in range(20)})
    driver = _batch_driver(model, session.wrap("批量电表", slave))
    driver.connect()
    try:
        reading = driver.read_all()
    finally:
        session.close()

    assert len(slave.requests) == 1, "20 个连续点仍是一帧 —— 降级不该改变成功路径的帧数"
    assert reading.readings["点019"] == 19.0
    records = _session_records(session)
    assert [r["kind"] for r in records if r["kind"] == "batch_fallback"] == []
    assert [r["direction"] for r in records if r["kind"] == "frame"] == ["send", "receive"]


# ========================= 写原语:只有点表声明过可写的点才能写 =========================
#
# 写和读是两条不同的承诺:写应答只证明"设备接受了这一写",值有没有生效要看回读。
# 所以 write() 返回的是回读到的物理量,不是刚写下去的那个数。

# 可写点:地址 3、一个寄存器、倍率 1 —— 写 1601 就是寄存器里的 1601(0x0641)。
WRITABLE_POINT = {"address": 3, "length": 2, "encoding": "uint", "writable": True}
# 带倍率的可写点:写 160.1 时才需要按 scale 反算成 1601。
SCALED_WRITABLE_POINT = {**WRITABLE_POINT, "scale": 0.1}


def _write_model(point_table):
    return parse_manifest(
        {
            "models": {
                "可写电表": {
                    "type": "meter",
                    "protocol": "modbus_rtu",
                    "capabilities": [next(iter(point_table))],
                    "point_table": point_table,
                }
            }
        }
    ).models["可写电表"]


def _write_driver(model, transport):
    return PointTableDriver(
        Meter("可写电表", "可写电表", CriteriaProfile(name="none")),
        model,
        transport,
        meter_driver._modbus_rtu_reader(transport, 1),
        write_raw=meter_driver._modbus_rtu_writer(transport, 1),
    )


class _WriteRecords:
    """传输外面挂一个 report_write:真跑时这一层是 evidence.py 的 RecordingTransport,
    这里只把记录收在内存里,好看清写动作留了什么痕。其余方法原样转给从站。"""

    def __init__(self, inner):
        self._inner = inner
        self.records = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def report_write(self, record):
        self.records.append(record)


class _RefusingModbusSlave(_RecordingModbusSlave):
    """设备明确拒了这次写:回异常应答(0x86 + 异常码),不是"没回话"。"""

    def _write_single_register(self, request, expected):
        slave, _function, _address, _value = modbus_rtu.parse_write_single_request(request)
        return modbus_rtu.build_exception_response(
            slave, modbus_rtu.ILLEGAL_DATA_VALUE, modbus_rtu.WRITE_SINGLE_REGISTER
        )


class _IgnoredWriteSlave(_RecordingModbusSlave):
    """写应答照旧回显(设备说"收到了"),但值没进寄存器:写成功 ≠ 值生效。"""

    def _write_single_register(self, request, expected):
        address = modbus_rtu.parse_write_single_request(request)[2]
        before = self._registers[address]
        answer = super()._write_single_register(request, expected)
        self._registers[address] = before
        return answer


def test_writing_a_point_without_writable_fails_and_names_the_writable_keys():
    """没声明 writable 的点写不了:许可写在点表里,用例不该靠猜哪个点能写。"""
    model = _write_model(
        {
            "有功电量": {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01},
            "有功计量模式": WRITABLE_POINT,
        }
    )
    driver = _write_driver(model, _RecordingModbusSlave({0: 0, 1: 0, 3: 0}))

    with pytest.raises(ValueError) as excinfo:
        driver.write("有功电量", 1)

    message = str(excinfo.value)
    assert "没有声明 writable" in message
    assert "可写的点: ['有功计量模式']" in message


def test_writing_an_unknown_key_names_the_available_ones():
    """读数键写错要和 read() 同一个口径:报出可用键,而不是一句空的 KeyError。"""
    driver = _write_driver(_write_model({"有功计量模式": WRITABLE_POINT}), _RecordingModbusSlave({3: 0}))

    with pytest.raises(KeyError) as excinfo:
        driver.write("有功计量方式", 1)

    assert "可用读数键" in str(excinfo.value)
    assert "有功计量模式" in str(excinfo.value)


def test_write_encodes_with_the_scale_and_validates_the_echo():
    """倍率 0.1 的可写点写 160.1:寄存器整数是 160.1 / 0.1 = 1601(0x0641),
    帧是 0x06 单寄存器写;回声校验通过后立刻读回一次,返回值就是回读值。"""
    slave = _RecordingModbusSlave({3: 0})
    driver = _write_driver(_write_model({"有功计量模式": SCALED_WRITABLE_POINT}), slave)
    driver.connect()

    read_back = driver.write("有功计量模式", 160.1)

    assert read_back == pytest.approx(160.1)
    assert [frame.hex(" ") for frame in slave.requests] == [
        "01 06 00 03 06 41 ba 5a",  # 写:功能码 0x06、寄存器 3、值 0x0641
        "01 03 00 03 00 01 74 0a",  # 回读:同一个点,一次读寄存器请求
    ]


def test_a_read_back_that_disagrees_is_not_reported_as_success():
    """写应答通过了(设备回显)但回读不一致:返回的是回读值,证据里 verified: false。
    回读不一致不是写这一层能判的结论 —— 设备不合格要由判据层判,这里只如实反映。"""
    transport = _WriteRecords(_IgnoredWriteSlave({3: 7}))
    driver = _write_driver(_write_model({"有功计量模式": WRITABLE_POINT}), transport)

    assert driver.write("有功计量模式", 1601) == pytest.approx(7.0), "返回的必须是回读值"
    assert transport.records == [
        {"key": "有功计量模式", "value": 1601, "verified": False, "read_back": 7.0}
    ]


def test_write_timeout_and_write_rejection_are_different_exceptions():
    """写超时 = 设备 / 链路没回话(TimeoutError);写被拒 = 设备明确回答不干(SlaveExceptionResponse)。
    两类都不是"设备不合格",但归因栏位不同,所以异常类型必须分得开。"""
    model = _write_model({"有功计量模式": WRITABLE_POINT})

    with pytest.raises(TimeoutError):
        _write_driver(model, _SilentModbusSlave({3: 0})).write("有功计量模式", 1)

    with pytest.raises(modbus_rtu.SlaveExceptionResponse) as excinfo:
        _write_driver(model, _RefusingModbusSlave({3: 0})).write("有功计量模式", 1)
    assert excinfo.value.code == modbus_rtu.ILLEGAL_DATA_VALUE


def test_a_four_byte_writable_point_is_written_with_0x10():
    """length > 2 的可写点自动走 0x10:发送的寄存器数与字节数由点表的 length 决定,
    不由用例选 —— 用例只说"写这个点的这个值"。"""
    point = {"address": 3, "length": 4, "encoding": "uint", "writable": True}
    slave = _RecordingModbusSlave({3: 0, 4: 0})
    driver = _write_driver(_write_model({"有功电量阈值": point}), slave)
    driver.connect()

    assert driver.write("有功电量阈值", 4000000000) == pytest.approx(4000000000.0)
    assert [frame.hex(" ") for frame in slave.requests] == [
        "01 10 00 03 00 02 04 ee 6b 28 00 e9 4e",  # 0x10:起始 3、2 个寄存器、数据 4 字节
        "01 03 00 03 00 02 34 0b",  # 回读:同一个点,一次读寄存器请求
    ]


def test_float32_write_uses_the_same_ordering_the_read_path_uses():
    """float32 + 字序 little:写下去的两个寄存器是低字先(0x5000 / 0x449A)。

    写和读共用同一个字序重排,所以回读解出来的就是刚写下去的那个数。
    """
    point = {
        "address": 6,
        "length": 4,
        "encoding": "float32",
        "word_order": "little",
        "writable": True,
    }
    slave = _RecordingModbusSlave({6: 0, 7: 0})
    driver = _write_driver(_write_model({"功率给定": point}), slave)
    driver.connect()

    assert driver.write("功率给定", 1234.5) == pytest.approx(1234.5)
    assert slave.requests[0].hex(" ") == "01 10 00 06 00 02 04 50 00 44 9a d1 ee"


def test_a_little_endian_writable_point_puts_the_low_byte_first_in_the_0x06_frame():
    """byteorder: little 的单寄存器点写 0x1234:帧里必须是 34 12,不是 12 34。

    判据是**发出去的 hex**。回读对不对证明不了这件事:模拟从站把值当整数存下的,
    写错字节序也照样回读一致 —— 读写都错时自洽,现场是"设了不生效"。
    """
    model = _write_model({"有功计量模式": {**WRITABLE_POINT, "byteorder": "little"}})
    slave = _RecordingModbusSlave({3: 0})
    driver = _write_driver(model, slave)
    driver.connect()

    driver.write("有功计量模式", 0x1234)

    assert slave.requests[0].hex(" ") == "01 06 00 03 34 12 ef 07", "little 点位的寄存器字节应是 34 12"


def test_a_big_endian_writable_point_keeps_the_big_endian_frame():
    """byteorder: big(以及缺省)的单寄存器点帧不变:还是 12 34。

    little 那条守"小端真发小端",这条守"大端 / 缺省没被改坏"—— 只留前者的话,
    把编码写死成小端也能全绿。
    """
    explicit = _write_model({"有功计量模式": {**WRITABLE_POINT, "byteorder": "big"}})
    default = _write_model({"有功计量模式": WRITABLE_POINT})
    frames = []
    for model in (explicit, default):
        slave = _RecordingModbusSlave({3: 0})
        driver = _write_driver(model, slave)
        driver.connect()
        driver.write("有功计量模式", 0x1234)
        frames.append(slave.requests[0].hex(" "))

    assert frames == ["01 06 00 03 12 34 74 bd"] * 2, "big 与缺省必须是同一个帧"


def test_a_bcd_password_is_written_as_three_bytes_plus_the_register_padding():
    """BCD 字符串写:120000 是 6 位十进制 → 3 字节 12 00 00,补一个字节凑整寄存器。

    补齐的那个字节不在回读比较里:它不归这个点,设备在那儿放什么都不算写错。
    """
    point = {"address": 8, "length": 3, "encoding": "bcd", "writable": True}
    slave = _RecordingModbusSlave({8: 0, 9: 0})
    transport = _WriteRecords(slave)
    driver = _write_driver(_write_model({"密码": point}), transport)
    driver.connect()

    assert driver.write("密码", 120000) == pytest.approx(120000.0)
    assert [frame.hex(" ") for frame in slave.requests] == [
        "01 10 00 08 00 02 04 12 00 00 00 f7 71",  # 0x10:2 个寄存器,第 4 个字节是补齐
        "01 03 00 08 00 02 45 c9",
    ]
    assert transport.records == [{"key": "密码", "value": 120000, "verified": True, "read_back": 120000.0}]


def test_a_six_byte_bcd_clock_is_written_as_three_registers():
    """时钟 YYMMDDhhmmss:6 字节 BCD = 3 个寄存器,一次 0x10。"""
    point = {"address": 4, "length": 6, "encoding": "bcd", "writable": True}
    slave = _RecordingModbusSlave({4: 0, 5: 0, 6: 0})
    driver = _write_driver(_write_model({"时钟": point}), slave)
    driver.connect()

    assert driver.write("时钟", 202601022359) == pytest.approx(202601022359.0)
    assert slave.requests[0].hex(" ") == "01 10 00 04 00 03 06 20 26 01 02 23 59 d0 c4"


def test_reading_a_point_and_writing_the_value_back_reproduces_the_registers():
    """往返不动点:读出来的值原样写回去,寄存器字节一个不差。

    这是整张票的核心 —— 反算错一位(倍率方向反了、字节序反了、字序只倒了一半)
    在这里就先把字节对不上,不必等到现场看到"写下去的值变了"。
    """
    model = _write_model(
        {
            "时钟": {"address": 4, "length": 6, "encoding": "bcd", "writable": True},
            "密码": {"address": 8, "length": 3, "encoding": "bcd", "writable": True},
            "功率给定": {
                "address": 6,
                "length": 4,
                "encoding": "float32",
                "word_order": "little",
                "writable": True,
            },
            "有功电量": {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01},
        }
    )
    slave = _RecordingModbusSlave(
        {
            0: 0x0001,
            1: 0xE240,
            4: 0x2026,
            5: 0x0102,
            6: 0x2359,
            7: 0x0000,
            8: 0x0012,
            9: 0x0000,
        }
    )
    driver = _write_driver(model, slave)
    driver.connect()
    before = dict(slave._registers)

    # 写帧的数据段就是刚才读到的那串寄存器字节。
    for key, write_frame in (
        ("时钟", "01 10 00 04 00 03 06 20 26 01 02 23 59 d0 c4"),
        ("密码", "01 10 00 08 00 02 04 00 12 00 00 52 0c"),
    ):
        value = driver.read(key)
        driver.write(key, value)
        assert slave.requests[-2].hex(" ") == write_frame
        assert driver.read(key) == pytest.approx(value)

    # 两次写一个寄存器都没改动:写回去的和读出来的逐字节相同。
    assert slave._registers == before
    assert driver.read("时钟") == pytest.approx(202601022359.0)
    assert driver.read("密码") == pytest.approx(1200.0)


def test_a_value_beyond_what_the_point_can_hold_is_refused_not_truncated():
    """2 字节写 70000:截断成 4464 是个看着合理的值,设备会接受、回读也一致 —— 必须报错。"""
    driver = _write_driver(_write_model({"有功计量模式": WRITABLE_POINT}), _RecordingModbusSlave({3: 0}))

    with pytest.raises(ValueError) as excinfo:
        driver.write("有功计量模式", 70000)

    message = str(excinfo.value)
    assert "有功计量模式" in message and "70000" in message
    assert "0–65535" in message


FACTORY_MANIFEST = Path(__file__).resolve().parents[1] / "configs" / "devices.yaml"


def test_write_then_read_back_closes_the_loop_on_the_factory_manifest(tmp_path):
    """出厂清单 meter_01(模拟从站)上的一次真闭环:清单 → 组合根 → 驱动 → Modbus 帧 → 从站内存。

    write("有功计量模式", 1601) 之后,同一实例上普通的读(走块读路径)也读到 1601;
    证据里留下 kind: write 与回读结果。写没搅乱同一次读数里的其他点。
    """
    session = EvidenceSession(tmp_path / "evidence")
    root = CompositionRoot.build(str(FACTORY_MANIFEST), evidence=session)
    try:
        meter = root.get("meter_01")
        meter.connect()

        assert meter.writable_keys() == ("有功计量模式",)
        assert meter.write("有功计量模式", 1601) == pytest.approx(1601.0)
        assert meter.read_all().readings["有功计量模式"] == pytest.approx(1601.0)
        assert meter.read_all().readings["电压"] == pytest.approx(230.0)
    finally:
        root.teardown()

    records = _session_records(session, "meter_01")
    writes = [record for record in records if record["kind"] == "write"]
    assert len(writes) == 1, f"一次写动作该留一条 write 记录,实际 {len(writes)} 条"
    assert {key: writes[0][key] for key in ("key", "value", "verified", "read_back")} == {
        "key": "有功计量模式",
        "value": 1601,
        "verified": True,
        "read_back": 1601.0,
    }
    sends = [record["hex"] for record in records if record["kind"] == "frame" and record["direction"] == "send"]
    assert sends == [
        "010600030641ba5a",  # 写单个寄存器:地址 3、值 1601
        "010300030001740a",  # 写后回读:只读这一个点
        "0103000000044409",  # 普通读全部点:仍然一帧
        "0103000000044409",
    ]


def test_a_six_byte_writable_point_closes_the_loop_on_the_in_process_simulator(tmp_path):
    """length > 2 的可写点(6 字节 BCD 时钟)在 mode: simulator 下写 → 回读闭环。

    清单 → 组合根 → 驱动 → 0x10 帧 → 模拟从站寄存器,每一步都是真实现:这里没有测试内的
    桩从站。以前这一写会落进模拟器的"不支持的功能码 0x10",报出来像驱动 bug。
    """
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["models"]["dtsd1352_modbus"]["point_table"]["时钟"] = {
        "address": 3,
        "length": 6,
        "encoding": "bcd",
        "writable": True,
    }
    # 地址 3/4/5 是时钟那三个寄存器;合并成一帧读全表时中间不留空洞。
    manifest["devices"][0]["connection"]["simulated_registers"].update({3: 0, 4: 0, 5: 0})

    session = EvidenceSession(tmp_path / "evidence")
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    root = CompositionRoot.build(str(path), evidence=session)
    try:
        meter = root.get("meter_a")
        meter.connect()

        assert root.mode_of("meter_a") == "simulator"
        assert meter.write("时钟", 202601022359) == pytest.approx(202601022359.0)
        assert meter.read("时钟") == pytest.approx(202601022359.0)
    finally:
        root.teardown()

    records = _session_records(session, "meter_a")
    sends = [
        record["hex"]
        for record in records
        if record["kind"] == "frame" and record["direction"] == "send"
    ]
    assert sends[0].startswith("01100003000306"), f"6 字节的点该走 0x10: {sends[0]}"
    writes = [record for record in records if record["kind"] == "write"]
    assert {key: writes[0][key] for key in ("key", "value", "verified", "read_back")} == {
        "key": "时钟",
        "value": 202601022359,
        "verified": True,
        "read_back": 202601022359.0,
    }


def test_write_then_read_back_is_judged_by_an_equals_criterion(tmp_path):
    """写 1601 → 读回应等于 1601:这条以前只能手写 assert,现在是一张判据。

    第一次读(从站寄存器里是 0)判不过,报出期望值与实测值;写进去之后再读,判过 ——
    写的验证和读的判据是同一条链,不是两份各写一半的断言。
    """
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["models"]["dtsd1352_modbus"]["point_table"]["有功计量模式"] = dict(WRITABLE_POINT)
    manifest["criteria_profiles"]["meter_220v"].append(
        {"quantity": "有功计量模式", "equals": "1601"}
    )
    manifest["devices"][0]["connection"]["simulated_registers"][3] = 0

    root = _build(tmp_path, manifest)
    try:
        meter = root.get("meter_a")
        meter.connect()

        before = meter.read_all()
        assert before.passed is False
        (breach,) = before.breaches
        assert breach.describe() == "有功计量模式 期望 '1601',实测 0.0"

        assert meter.write("有功计量模式", 1601) == pytest.approx(1601.0)
        assert meter.read_all().passed is True
    finally:
        root.teardown()
