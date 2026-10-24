"""
终端兼通路:一台 DL/T645 采集终端既是**被测设备**,也是下游电表的接入通路。

清单里下游设备用 `via:` 声明挂在哪台终端下。它自己没有物理连接:
每条请求帧里带的是自己的 bus_address,由协议层选中从站,总线归终端。
所以这里有两条规矩:

- 装配按拓扑序(终端先、下游后),下游设备借到的就是终端那条总线;
- 下游设备的 connect() 不建物理连接,终端没连时它直接报错。

终端不是电表:它有它自己的设备类型与型号(terminal)、自己的能力与点表,
所以"终端自身作为被测设备"读的是终端自己的读数键;电表型号保持原样。

用例只经 CompositionRoot.build(清单路径) 与聚合根这两条已有的缝,
不新增第三个测试缝。
"""
import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest

from tests_support.manifest_factory import build, dlt645_bench, write
from tests_support.nested_pytest import pytest_command

from testplatform.composition.root import UpstreamFault
from testplatform.domain.device import DeviceState
from testplatform.domain.exceptions import DeviceNotConnectedError

TESTS_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
REPO = Path(__file__).resolve().parent.parent

TERMINAL_ADDRESS = "000000000001"
METER_A_ADDRESS = "000000000002"
METER_B_ADDRESS = "000000000003"

BASE_MANIFEST = dlt645_bench(
    {
        TERMINAL_ADDRESS: {"02800085": "80510100"},  # 86400s 上电运行时间
        METER_A_ADDRESS: {"02010100": "0022", "00010000": "00000000"},  # 220.0V / 0kWh
        METER_B_ADDRESS: {"02010100": "0025", "00010000": "00000000"},  # 250.0V / 0kWh
    },
    meters=["meter_a", "meter_b"],
)


def _build(tmp_path, manifest):
    return build(tmp_path, manifest)


@pytest.fixture()
def bus_root(tmp_path):
    root = _build(tmp_path, copy.deepcopy(BASE_MANIFEST))
    yield root
    root.teardown()


@pytest.fixture()
def _connected_terminal(bus_root):
    bus_root.get("terminal_01").connect()


# ========================= 装配顺序:终端先、下游后 =========================

def test_terminal_is_assembled_before_the_devices_under_it(bus_root):
    """拓扑序不是靠清单书写顺序保证的:把终端放到清单最后也一样。"""
    assert list(bus_root.drivers) == ["terminal_01", "meter_a", "meter_b"]
    # 终端自己也是正常的被测设备,但它有自己的类型:契约测试按类型展开,
    # 终端下的电表不需要专门用例,终端也不靠冒充电表被展开。
    assert bus_root.ids_of_type("terminal") == ["terminal_01"]
    assert bus_root.ids_of_type("meter") == ["meter_a", "meter_b"]
    assert bus_root.role_of("terminal_01") == "dut"
    assert bus_root.model_of("terminal_01").type == "terminal"
    assert bus_root.model_of("meter_a").type == "meter"


def test_assembly_is_topological_even_when_the_manifest_lists_meters_first(tmp_path):
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"] = [manifest["devices"][1], manifest["devices"][2], manifest["devices"][0]]

    root = _build(tmp_path, manifest)
    try:
        assert list(root.drivers) == ["terminal_01", "meter_a", "meter_b"]
    finally:
        root.teardown()


def test_contract_tests_expand_to_each_device_under_its_own_type(tmp_path):
    """契约测试按设备类型展开:终端与它下面的两块表各按自己的类型展开,
    下游电表不需要专门用例,终端也不需要冒充电表。

    这里直接跑 tests/conftest.py 里那个 pytest_generate_tests 钩子(收集阶段做的事),
    看每个类型被参数化成了哪几台设备。
    """
    spec = importlib.util.spec_from_file_location("tests_conftest_topology", TESTS_CONFTEST)
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    class _FakeConfig:
        def getoption(self, name):
            assert name == "--device-config"
            return write(tmp_path, copy.deepcopy(BASE_MANIFEST))

    def _parametrized_for(fixture_name):
        class _FakeMetafunc:
            fixturenames = [fixture_name]
            config = _FakeConfig()
            parametrizations = []

            def parametrize(self, name, ids, indirect=False):
                self.parametrizations.append((name, list(ids), indirect))

        metafunc = _FakeMetafunc()
        conftest.pytest_generate_tests(metafunc)
        return metafunc.parametrizations

    assert _parametrized_for("terminal") == [("terminal", ["terminal_01"], True)]
    assert _parametrized_for("meter") == [("meter", ["meter_a", "meter_b"], True)]


# ===================== 一台终端下挂多块表:各自读、各自判 =====================

@pytest.mark.parametrize(
    "device_id, voltage, energy, passed",
    [
        pytest.param("meter_a", 220.0, 0.0, True, id="下游表A"),
        pytest.param("meter_b", 250.0, 0.0, False, id="下游表B超上限"),
    ],
)
def test_each_meter_under_the_terminal_reads_and_is_judged_on_its_own(
    bus_root, _connected_terminal, device_id, voltage, energy, passed
):
    """同一台终端、同一条总线:每块表有自己的从站地址、自己的读数、自己的合格结论。"""
    driver = bus_root.get(device_id)
    driver.connect()

    reading = driver.read_all()

    assert reading.readings["电压"] == pytest.approx(voltage)
    assert reading.readings["正向有功总电能"] == pytest.approx(energy)
    assert reading.passed is passed
    if passed:
        assert reading.breaches == ()
    else:
        assert [(b.quantity, round(b.exceeded_by, 6)) for b in reading.breaches] == [("电压", 8.0)]


def test_readings_of_two_meters_do_not_leak_into_each_other(bus_root, _connected_terminal):
    """共享一条总线不等于共享一份读数:每块表按自己的地址单独取数。"""
    healthy = bus_root.get("meter_a")
    faulty = bus_root.get("meter_b")
    healthy.connect()
    faulty.connect()

    healthy_voltage = healthy.read_all().readings["电压"]
    faulty_voltage = faulty.read_all().readings["电压"]

    assert (healthy_voltage, faulty_voltage) == (pytest.approx(220.0), pytest.approx(250.0))
    assert faulty.device.history[-1].passed is False
    assert healthy.device.history[-1].passed is True


def test_terminal_reads_its_own_reading_keys_not_a_meters(bus_root, _connected_terminal):
    """终端自身作为被测设备:读的是终端型号点表里的键,不是电表的读数键。"""
    terminal = bus_root.get("terminal_01")
    terminal.connect()

    reading = terminal.read_all()

    assert reading.readings == {"上电运行时间": pytest.approx(86400)}
    assert terminal.device.history[-1] is reading
    # 电表的读数键不属于终端:拿电表的键去读,当场报错而不是返回一份电表的读数。
    with pytest.raises(KeyError, match="正向有功总电能"):
        terminal.read("正向有功总电能")


def test_terminal_model_differs_from_the_meter_model(bus_root):
    """终端与电表是两个型号、两种设备类型:能力与点表各是各的。"""
    assert bus_root.capabilities_of("terminal_01") == frozenset({"上电运行时间"})
    assert bus_root.capabilities_of("meter_a") == frozenset({"电压", "正向有功总电能"})
    assert bus_root.reading_keys_of("terminal_01") == ("上电运行时间",)
    # 点表键的顺序跟着 yaml 走,这里只比物量集合。
    assert set(bus_root.reading_keys_of("meter_a")) == {"电压", "正向有功总电能"}
    assert bus_root.model_of("terminal_01") is not bus_root.model_of("meter_a")


# ========================= 下游设备不重复建立物理连接 =========================

def test_downstream_connect_does_not_open_the_bus_again(bus_root):
    """下游设备的 connect() 只确认终端连着、选中自己的从站地址,不碰物理链路:
    终端的连接计数在它连上之后不因下游设备连接而增加。"""
    bus = bus_root.transports["terminal_01"]
    connects, disconnects = [], []
    bus.connect = lambda: (connects.append(1), bus.__class__.connect(bus))
    bus.disconnect = lambda: (disconnects.append(1), bus.__class__.disconnect(bus))

    bus_root.get("terminal_01").connect()
    bus_root.get("meter_a").connect()
    bus_root.get("meter_b").connect()

    assert connects == [1]
    assert disconnects == []
    # 下游设备断开也不关总线:总线归终端。
    bus_root.get("meter_a").disconnect()
    assert disconnects == []


def test_devices_under_one_terminal_share_the_terminal_bus(bus_root):
    terminal_bus = bus_root.transports["terminal_01"]

    assert bus_root.transports["meter_a"].bus is terminal_bus
    assert bus_root.transports["meter_b"].bus is terminal_bus


def test_downstream_device_cannot_be_used_before_the_terminal_is_connected(bus_root):
    """终端没连就读表,是平台自身用错,不是电表不合格:当场报错。

    这条保护不因为泛型 fixture 会先连上游而失效:它守的是"下游设备不能在上游没连时
    使用",验的就是 direct connect 这条路径。
    """
    with pytest.raises(DeviceNotConnectedError) as excinfo:
        bus_root.get("meter_a").connect()

    assert "terminal_01" in str(excinfo.value)


# ==================== 取设备的人负责把上游连好(泛型 fixture 用它) ====================

def test_get_ready_connects_the_upstream_once_for_every_meter_under_it(bus_root):
    """一条总线下一挂多块表:取设备时上游只连一次,不重复开总线。"""
    bus = bus_root.transports["terminal_01"]
    connects = []
    bus.connect = lambda: (connects.append(1), bus.__class__.connect(bus))

    delivered = [bus_root.get_ready(device_id) for device_id in ("meter_a", "meter_b", "meter_a")]

    assert connects == [1]
    assert bus_root.get("terminal_01").device.state is DeviceState.CONNECTED
    assert [driver.device.id for driver in delivered] == ["meter_a", "meter_b", "meter_a"]


def test_upstream_connect_failure_is_a_test_invalid_not_a_device_failure(bus_root, monkeypatch):
    """终端连不上:被测的电表还没被验过,这一条用例的结论是"测试无效"。

    get_ready 负责把上游的失败换成 UpstreamFault,泛型 fixture 再换成 skip +
    固定前缀;不能落成 platform_error(平台没 bug),也不能读成电表不合格。
    """

    def _refuse():
        raise ConnectionError("串口 /dev/ttyUSB2 打不开")

    monkeypatch.setattr(bus_root.get("terminal_01"), "connect", _refuse)

    with pytest.raises(UpstreamFault) as excinfo:
        bus_root.get_ready("meter_a")

    assert excinfo.value.device_id == "terminal_01"
    # 归因层按这句话区分"没连上"与"跑到一半断链",措辞不能改。
    assert "连接失败" in str(excinfo.value)


# 规模清单里的一台终端 + 挂在它下面的 3 块表(缩小版拓扑),跑的是 tests/test_meter.py 里
# 那 4 条真实契约用例 —— 用例体内只有 meter.connect(),没有一行手写连终端。
SCALE_MANIFEST = REPO / "configs" / "devices_scale.yaml"
CONTRACT_CASE_IDS = [
    "test_meter_connects",
    "test_meter_reads_physical_value_by_reading_key",
    "test_meter_reading_is_recorded_in_history",
    "test_meter_unknown_reading_key_names_the_available_ones",
]
METERS_UNDER_ONE_TERMINAL = [f"terminal_scale_01_meter_{index:02d}" for index in (1, 2, 3)]


def test_the_contract_cases_under_a_terminal_are_green_and_stay_green_on_a_second_run():
    """挂终端下的表:泛型 fixture 交付前上游已连好,4 条契约用例 × 3 块表全绿。

    跑两次:第二次不能因为终端已经连着而变得不同(状态不能跨进程漏出去)。
    """
    cases = [
        f"tests/test_meter.py::{case_id}[{meter_id}]"
        for case_id in CONTRACT_CASE_IDS
        for meter_id in METERS_UNDER_ONE_TERMINAL
    ]

    for attempt in (1, 2):
        result = subprocess.run(
            pytest_command(*cases, "-q", "--device-config", str(SCALE_MANIFEST), "--no-evidence"),
            cwd=REPO,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"第 {attempt} 次: {result.stdout[-2000:]}"
        assert f"{len(cases)} passed" in result.stdout, result.stdout[-2000:]


# ========================= 装配前就报错的清单写法 =========================

def test_via_pointing_at_an_unknown_terminal_fails_before_assembly(tmp_path):
    """via 写错必须报错,而不是静默地一台设备都不测。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"][1]["via"] = "terminal_99"

    with pytest.raises(KeyError) as excinfo:
        _build(tmp_path, manifest)

    assert "terminal_99" in str(excinfo.value)
    assert "terminal_01" in str(excinfo.value)  # 报错里列出已声明的设备


def test_via_cycle_fails_before_assembly(tmp_path):
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"][1]["via"] = "meter_b"
    manifest["devices"][2]["via"] = "meter_a"

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "成环" in str(excinfo.value)


def test_downstream_device_cannot_declare_its_own_connection(tmp_path):
    """物理连接归终端。下游设备再写 connection 是清单写错了,不能静默忽略。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"][1]["connection"] = {"port": "/dev/ttyUSB3"}

    with pytest.raises(ValueError) as excinfo:
        _build(tmp_path, manifest)

    assert "connection" in str(excinfo.value)


def test_dlt645_meter_needs_a_12_digit_meter_address(tmp_path):
    """DL/T645 的从站地址是 12 位 BCD 表地址,缺了或写成整数都在装配前报错。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    del manifest["devices"][2]["bus_address"]
    with pytest.raises(ValueError, match="bus_address"):
        _build(tmp_path, manifest)

    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"][2]["bus_address"] = 3
    with pytest.raises(ValueError, match="12 位 BCD"):
        _build(tmp_path, manifest)
