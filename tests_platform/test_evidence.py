"""
证据落盘:跑完一轮,每台被测设备在按会话隔离的目录下留一份 JSONL,
能据它复现结论 —— 原始报文(hex)、解码读数、实际生效的判据集、求值结论、
领域事件、用例身份。

用例只经已有的两条缝:CompositionRoot.build(清单路径) 装配,然后读证据文件断言。
证据怎么采(包一层传输)不在这里测,这里测的是"落盘的东西够不够复现"。
"""
import copy
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests_support.manifest_factory import (
    INVERTER_MODEL,
    METER_220V,
    MODBUS_METER_MODEL,
    build,
    dlt645_bench,
    manifest,
    meter_device,
)
from tests_support.nested_pytest import pytest_command

from testplatform.application.event_relay import drain_device_events
from testplatform.cases import case, case_of
from testplatform.composition.root import CompositionRoot, SupportLinkLost
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.device import HISTORY_WINDOW
from testplatform.domain.events import HighPowerWarning
from testplatform.domain.meter import Meter
from testplatform.protocol import modbus_rtu
from testplatform.reporting.event_sinks import InMemoryEventSink
from testplatform.reporting.evidence import EvidenceSession, RecordingTransport, parse_at
from testplatform.transport.interfaces import ByteStreamTransport
from testplatform.transport.simulators import SimulatedByteStreamTransport

REPO = Path(__file__).resolve().parent.parent

GRID_50HZ = [
    {"quantity": "grid_freq_hz", "min": 45, "max": 55},
    {"quantity": "grid_voltage_v", "gt": 0},
    {"quantity": "output_power_w", "min": 0, "max": {"rated_power_w": 1.1}},
    {"quantity": "output_power_w", "max": {"rated_power_w": 0.9}, "severity": "warn"},
]


# 下游表只声明「电压」一个点:这条台架的用例靠"一帧一条记录"断言,多一个点就多一帧。
TERMINAL_BUS_MANIFEST = dlt645_bench(
    {
        "000000000001": {"02800085": "80510100"},
        "000000000002": {"02010100": "0022"},
    },
    energy=False,
)


def _manifest(devices, profiles=None, models=None):
    return {
        "criteria_profiles": profiles or {"meter_220v": copy.deepcopy(METER_220V)},
        "models": models or {"dtsd1352_modbus": copy.deepcopy(MODBUS_METER_MODEL)},
        "devices": devices,
    }


def _build(tmp_path, manifest, evidence):
    return build(tmp_path, manifest, evidence=evidence)


def _round(root, identity=None) -> None:
    """一轮的收尾动作:取走事件并写下证据(与 tests/conftest.py 那个收尾 fixture 同一件事)。"""
    events = drain_device_events(root.all_drivers(), InMemoryEventSink())
    root.record_evidence(identity=identity, events=events)


def _records(session, device_id):
    path = session.path_of(device_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _of_kind(session, device_id, kind):
    return [record for record in _records(session, device_id) if record["kind"] == kind]


def _readings_taken_at_once(session, device_id, count):
    """不走传输直接记读数,再一次性取走证据:测的是取增量那一侧,不需要 count 个往返。"""
    meter = Meter(device_id, device_id, CriteriaProfile(name="none"))
    meter.mark_connected()
    for _ in range(count):
        meter.record_reading({"有功电量": 1234.56})
    session.record_round(
        drivers={device_id: SimpleNamespace(device=meter)}, criteria_overrides={}, modes={}
    )
    return meter


def _by_at(row):
    return parse_at(row["at"])


@pytest.fixture()
def session(tmp_path):
    return EvidenceSession(tmp_path / "evidence")


def test_each_device_leaves_its_own_file_in_a_session_directory(tmp_path, session):
    """两台设备、两条结论:一台合格,一台超上限不合格,各自一份文件。"""
    root = _build(
        tmp_path,
        _manifest([meter_device("meter_a", 1, 2300), meter_device("meter_b", 2, 2500)]),
        session,
    )
    try:
        for device_id in ("meter_a", "meter_b"):
            root.get(device_id).connect()
            root.get(device_id).read_all()
        _round(root)
    finally:
        root.teardown()

    assert sorted(path.name for path in session.directory.iterdir()) == [
        "meter_a.jsonl",
        "meter_b.jsonl",
    ]
    (healthy,) = _of_kind(session, "meter_a", "reading")
    (faulty,) = _of_kind(session, "meter_b", "reading")
    assert healthy["readings"]["电压"] == pytest.approx(230.0)
    assert healthy["evaluation"] == {"passed": True, "breaches": []}
    assert faulty["readings"]["电压"] == pytest.approx(250.0)
    assert faulty["evaluation"]["passed"] is False
    assert [(b["quantity"], b["exceeded_by"]) for b in faulty["evaluation"]["breaches"]] == [
        ("电压", 8.0)
    ]


def test_raw_frames_and_decoded_readings_are_in_the_evidence(tmp_path, session):
    """报文是原始 hex(发送与应答各一条),读数是解码后的物理量,两者都带时间戳。

    两个点地址相邻(0 起共 3 个寄存器),所以这次读数只发一帧:证据里因此是
    send/receive 两条,而不是合并前的四条。
    """
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root)
    finally:
        root.teardown()

    frames = _of_kind(session, "meter_a", "frame")
    assert [(f["direction"], f["hex"]) for f in frames] == [
        ("send", modbus_rtu.build_read_request(1, 0, 3).hex()),
        ("receive", modbus_rtu.build_read_response(1, bytes.fromhex("0001e24008fc"), 3).hex()),
    ]
    # 证据里的报文就是总线上跑的那一帧:按它重新解析能解回同一段寄存器字节。
    assert modbus_rtu.parse_read_response(
        bytes.fromhex(frames[1]["hex"]), 1
    ) == bytes.fromhex("0001e24008fc")

    (reading,) = _of_kind(session, "meter_a", "reading")
    assert reading["readings"] == {"有功电量": pytest.approx(1234.56), "电压": pytest.approx(230.0)}
    for record in frames + [reading]:
        assert datetime.fromisoformat(record["at"])  # 时间戳可直接读回


def test_evidence_answers_which_case_and_which_clause(tmp_path, session):
    """用例身份取自测试函数上的 @case(和筛选用的是同一份,没有第二份清单)。"""
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root, identity=case_of(_meter_reads_by_reading_key))
    finally:
        root.teardown()

    (reading,) = _of_kind(session, "meter_a", "reading")
    assert reading["case"] == {
        "id": "M_00_00_01_000002_09",
        "clause": "DLT645-4.5.2",
        "tags": ["一致性", "读数"],
    }


@case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2", tags=["一致性", "读数"])
def _meter_reads_by_reading_key():
    """这条函数不会被 pytest 收集(名字不是 test_*),它就是身份的来源。"""


def test_a_single_read_leaves_frames_but_no_reading_record(tmp_path, session):
    """read(key) 是取值不是测量:总线上有一帧,但证据里没有新的 kind: reading。

    读数证据只由 read_all() 产出(它同时是唯一求判据的入口),所以这条用例锁的是
    "单读不会悄悄变成一条可判读数" —— 否则下一次有人把"顺带记录"加回来就看不出来。
    """
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        meter = root.get("meter_a")
        meter.connect()
        assert meter.read("电压") == pytest.approx(230.0)
        _round(root)
    finally:
        root.teardown()

    assert [f["direction"] for f in _of_kind(session, "meter_a", "frame")] == [
        "send",
        "receive",
    ]
    assert _of_kind(session, "meter_a", "reading") == [], "单读不落读数证据"
    assert meter.device.history == (), "单读不进 history"


def test_two_runs_do_not_overwrite_each_other(tmp_path):
    """两次运行各自一个会话目录:后一次不动前一次的证据。"""
    first = EvidenceSession.open(tmp_path / "evidence")
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), first)
    try:
        root.get("meter_a").connect()
        root.get("meter_a").read_all()
        _round(root)
    finally:
        root.teardown()
    first_lines = first.path_of("meter_a").read_text(encoding="utf-8")

    second = EvidenceSession.open(tmp_path / "evidence")
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2500)]), second)
    try:
        root.get("meter_a").connect()
        root.get("meter_a").read_all()
        _round(root)
    finally:
        root.teardown()

    assert first.directory != second.directory
    assert first.path_of("meter_a").read_text(encoding="utf-8") == first_lines
    assert _of_kind(second, "meter_a", "reading")[0]["readings"]["电压"] == pytest.approx(250.0)


def test_open_boundary_breach_is_written_as_equal_not_exceeded_by_zero(tmp_path, session):
    """gt/lt 的开区间边界:落在边界上也算越界,证据里读成"等于边界(不允许等于)",
    而不是"超出 0" —— 后者会让人以为设备其实合格。"""
    root = _build(
        tmp_path,
        _manifest(
            [meter_device("meter_a", 1, 0)],
            profiles={"meter_220v": [{"quantity": "电压", "gt": 0}]},
        ),
        session,
    )
    try:
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root)
    finally:
        root.teardown()

    (breach,) = _of_kind(session, "meter_a", "reading")[0]["evaluation"]["breaches"]
    assert breach["exceeded_by"] == pytest.approx(0.0)
    assert breach["text"] == "电压=0.0 等于边界 0.0(不允许等于)"
    assert "超出 0" not in breach["text"]


def test_equals_breach_carries_expected_instead_of_an_empty_range(tmp_path, session):
    """等值判据违约时证据里要读得出「期望多少、实测多少」,而且不许留下
    limit: null + exceeded_by: 0.0 —— 那个组合机器会读成「没有边界、没有越界」,
    而结论是 passed: false。"""
    root = _build(
        tmp_path,
        _manifest(
            [meter_device("meter_a", 1, 2500)],
            profiles={"meter_220v": [{"quantity": "电压", "equals": 230}]},
        ),
        session,
    )
    try:
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root)
    finally:
        root.teardown()

    reading = _of_kind(session, "meter_a", "reading")[0]
    assert reading["evaluation"]["passed"] is False
    (breach,) = reading["evaluation"]["breaches"]
    assert (breach["kind"], breach["expected"], breach["value"]) == ("equals", 230, 250.0)
    assert breach["text"] == "电压 期望 230,实测 250.0"
    # 机器可读的守卫:区间字段不适用于等值,旧形态(同时是 null / 0.0)不许再出现。
    assert "limit" not in breach
    assert "exceeded_by" not in breach


def test_range_breach_evidence_is_unchanged_by_the_equals_shape(tmp_path, session):
    """区间违约的记录逐字节不变:既有消费方按 limit / exceeded_by 读,字段名、含义、
    连键的顺序都不动。这里钉住整条 breach 的 JSON 序列化。"""
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2500)]), session)
    try:
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root)
    finally:
        root.teardown()

    (breach,) = _of_kind(session, "meter_a", "reading")[0]["evaluation"]["breaches"]
    serialized = json.dumps(breach, ensure_ascii=False)
    assert serialized == (
        '{"quantity": "电压", "value": 250.0, "limit": 242.0, "exceeded_by": 8.0, '
        '"severity": "reject", "text": "电压=250.0 越过 242.0,超出 8.0"}'
    )
    assert serialized in session.path_of("meter_a").read_text(encoding="utf-8")


def test_evidence_shows_the_base_profile_and_which_criteria_the_device_overrode(tmp_path, session):
    """行内覆盖后 CriteriaProfile.name 仍是基础集名,证据里必须看得出用了覆盖:
    基础集 grid_50hz + 被覆盖的 grid_freq_hz + 实际生效的 55–65。"""
    root = _build(
        tmp_path,
        _manifest(
            [
                {
                    "id": "inv_60",
                    "model": "inverter_pv_series",
                    "access": "tcp",
                    "mode": "simulator",
                    "rated_power_w": 5000,
                    "criteria_override": [{"quantity": "grid_freq_hz", "min": 55, "max": 65}],
                    "connection": {"simulated_response": "4600.0 230.0 60.0"},
                }
            ],
            profiles={"grid_50hz": copy.deepcopy(GRID_50HZ)},
            models={"inverter_pv_series": copy.deepcopy(INVERTER_MODEL)},
        ),
        session,
    )
    try:
        inverter = root.get("inv_60")
        inverter.connect()
        inverter.read_all()
        _round(root)
    finally:
        root.teardown()

    (reading,) = _of_kind(session, "inv_60", "reading")
    assert reading["criteria"]["profile"] == "grid_50hz"
    assert reading["criteria"]["overridden"] == ["grid_freq_hz"]
    frequency = [
        criterion
        for criterion in reading["criteria"]["criteria"]
        if criterion["quantity"] == "grid_freq_hz"
    ]
    assert [(c["low"], c["high"]) for c in frequency] == [(55.0, 65.0)]
    assert reading["readings"]["grid_freq_hz"] == pytest.approx(60.0)
    assert reading["evaluation"]["passed"] is True


def test_soft_signal_events_are_written_next_to_the_readings(tmp_path, session):
    """软信号(功率逼近额定)不只进内存与终端,它和那次读数写在同一份证据里。"""
    root = _build(
        tmp_path,
        _manifest(
            [
                {
                    "id": "inv_01",
                    "model": "inverter_pv_series",
                    "access": "tcp",
                    "mode": "simulator",
                    "rated_power_w": 5000,
                    "connection": {"simulated_response": "4900.0 230.0 50.0"},
                }
            ],
            profiles={"grid_50hz": copy.deepcopy(GRID_50HZ)},
            models={"inverter_pv_series": copy.deepcopy(INVERTER_MODEL)},
        ),
        session,
    )
    try:
        inverter = root.get("inv_01")
        inverter.connect()
        inverter.read_all()
        _round(root)
    finally:
        root.teardown()

    (reading,) = _of_kind(session, "inv_01", "reading")
    assert reading["evaluation"]["passed"] is True  # 软信号不判不合格
    assert [b["severity"] for b in reading["evaluation"]["breaches"]] == ["warn"]
    (event,) = reading["events"]
    assert event["type"] == HighPowerWarning.__name__
    assert event["output_power_w"] == pytest.approx(4900.0)
    assert reading["readings"]["output_power_w"] == pytest.approx(4900.0)


def test_a_case_that_dies_before_its_round_is_recorded_still_leaves_evidence(tmp_path, session):
    """失败必存:报文在 send/receive 那一刻就落盘;用例崩在收尾记录之前时,
    组合根 teardown 还有一次兜底,已读到的读数与结论照样进证据。"""
    root = _build(tmp_path, _manifest([meter_device("meter_b", 2, 2500)]), session)
    meter = root.get("meter_b")
    meter.connect()
    meter.read_all()

    # 用例在这一步之后崩了:这一轮的收尾记录(_round)没有机会执行。
    assert _of_kind(session, "meter_b", "frame"), "报文必须先于结论落盘"

    root.teardown()

    (reading,) = _of_kind(session, "meter_b", "reading")
    assert reading["evaluation"]["passed"] is False
    assert reading["case"] is None  # 兜底这一次拿不到当前用例,不伪造身份
    # 兜底不会把已经记过的读数再记一遍。
    assert len(_of_kind(session, "meter_b", "reading")) == 1


def test_device_under_a_terminal_records_only_its_own_frames(tmp_path, session):
    """下游设备借终端那条总线,但报文只记在发起方那份文件里:终端那份不会被下游的收发污染。"""
    root = _build(tmp_path, copy.deepcopy(TERMINAL_BUS_MANIFEST), session)
    try:
        terminal = root.get("terminal_01")
        terminal.connect()
        terminal.read_all()
        meter = root.get("meter_a")
        meter.connect()
        meter.read_all()
        _round(root)
    finally:
        root.teardown()

    terminal_frames = _of_kind(session, "terminal_01", "frame")
    meter_frames = _of_kind(session, "meter_a", "frame")
    assert [frame["direction"] for frame in terminal_frames] == ["send", "receive"]
    assert [frame["direction"] for frame in meter_frames] == ["send", "receive"]
    # 两个地址域不同,所以报文集合不相交:下游的帧没有被记到终端那份里。
    assert {frame["hex"] for frame in terminal_frames}.isdisjoint(
        {frame["hex"] for frame in meter_frames}
    )

    (meter_reading,) = _of_kind(session, "meter_a", "reading")
    assert meter_reading["readings"] == {"电压": pytest.approx(220.0)}
    # 终端没有上游验收判据:读数只记录,不带判据集也不判合格。
    (terminal_reading,) = _of_kind(session, "terminal_01", "reading")
    assert terminal_reading["criteria"] is None
    assert terminal_reading["evaluation"] is None


def test_evidence_is_not_duplicated_when_the_round_is_recorded_twice(tmp_path, session):
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        root.get("meter_a").connect()
        root.get("meter_a").read_all()
        _round(root)
        _round(root)
    finally:
        root.teardown()

    assert len(_of_kind(session, "meter_a", "reading")) == 1


@pytest.mark.parametrize("reads", [10, HISTORY_WINDOW + 5])
def test_one_reading_record_per_read_whether_or_not_the_window_trimmed(tmp_path, session, reads):
    """history 是固定窗口,游标是读数计数器。窗口开始丢最旧条目之后(1205 条只留最后 1000 条),
    证据仍然一条读数一条记录,和未裁剪时(10 条)一样 —— 拿 len(history) 当游标会在
    第 1000 条起静默封顶,证据少一百多条却看不出任何异常。"""
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        driver = root.get("meter_a")
        driver.connect()
        for _ in range(reads):
            driver.device.record_reading({"有功电量": 1234.56, "电压": 230.0})
            _round(root)  # 一条用例结束就取走证据:裁剪只能丢"已经取走"的
    finally:
        root.teardown()

    assert len(driver.device.history) == min(reads, HISTORY_WINDOW)
    assert driver.device.readings_recorded == reads
    assert len(_of_kind(session, "meter_a", "reading")) == reads


def test_a_round_over_the_window_leaves_a_gap_record_instead_of_silence(session):
    """两轮之间记的条数超过 history 窗口:最旧的那几条已被裁掉、取不回来。

    这里以前一声不响 —— 证据看着完整而实际少条,“失败必留证据”就无声地破了。
    现在留一条 evidence_gap,缺几条写在 missing 里,归因把它归进证据损坏。
    """
    meter = _readings_taken_at_once(session, "meter_a", HISTORY_WINDOW + 5)

    (gap,) = _of_kind(session, "meter_a", "evidence_gap")
    assert gap["missing"] == 5
    assert gap["window"] == HISTORY_WINDOW
    assert "5 条" in gap["note"]
    # 缺的那几条写在读数之前:现场一眼先看到缺口,再看到还能救回来的那些读数。
    assert _records(session, "meter_a")[0]["kind"] == "evidence_gap"
    assert meter.readings_recorded == HISTORY_WINDOW + 5
    assert len(_of_kind(session, "meter_a", "reading")) == HISTORY_WINDOW


def test_a_round_that_fits_the_window_writes_no_gap_record(session):
    """恰好装满窗口(没超出)不是缺口:不写 gap 记录,否则每轮多一条噪声,
    真缺口反而浸在里面。"""
    _readings_taken_at_once(session, "meter_a", HISTORY_WINDOW)

    assert _of_kind(session, "meter_a", "evidence_gap") == []
    assert len(_of_kind(session, "meter_a", "reading")) == HISTORY_WINDOW


def test_timestamps_are_written_with_an_offset(session):
    """每条记录的 at 与领域读数的 timestamp 都带时区:两台台架各跑一轮、
    把证据并到一起时才排得出先后。"""
    meter = _readings_taken_at_once(session, "meter_a", 1)

    assert _records(session, "meter_a")[0]["at"].endswith("+00:00")
    assert meter.latest_reading.timestamp.utcoffset() is not None
    assert meter.connected_at.utcoffset() is not None


def test_two_benches_sort_by_real_time_and_old_records_without_an_offset_still_join():
    """跨台架合并:两份证据的 at 要按绝对时刻排,不能按字符串排;
    旧证据的 at 没有偏移 —— 按本地时间理解,照样能参与排序。

    改格式不改读法:否则跨台架的问题只是换成了跨版本的问题。
    """
    shanghai = {"bench": "上海台架", "at": "2024-01-01T09:00:00.000+08:00"}  # 01:00Z
    utc = {"bench": "UTC 台架", "at": "2024-01-01T02:00:00.000+00:00"}  # 02:00Z

    assert [row["bench"] for row in sorted([utc, shanghai], key=_by_at)] == ["上海台架", "UTC 台架"]
    # 字符串排序会把两个台架排反:偏移不参与字典序。
    assert [row["bench"] for row in sorted([utc, shanghai], key=lambda row: row["at"])] == [
        "UTC 台架",
        "上海台架",
    ]

    old = {"bench": "旧证据(无偏移)", "at": "2020-01-01T00:00:00.000"}
    assert parse_at(old["at"]) == datetime.fromisoformat(old["at"]).astimezone()
    assert [row["bench"] for row in sorted([utc, shanghai, old], key=_by_at)][0] == (
        "旧证据(无偏移)"
    )


def test_turning_recording_off_leaves_assembly_and_readings_unchanged(tmp_path):
    """关掉记录:不建目录、不多包一层传输,读数与开着记录时完全一样。"""
    manifest = _manifest([meter_device("meter_a", 1, 2300)])
    off = _build(tmp_path, manifest, evidence=None)
    try:
        assert off.evidence is None
        assert not isinstance(off.transports["meter_a"], RecordingTransport)
        assert not (tmp_path / "evidence").exists()
        meter = off.get("meter_a")
        meter.connect()
        reading = meter.read_all()
        off.record_evidence()  # 关掉时是空操作,不抛错
    finally:
        off.teardown()

    on_session = EvidenceSession(tmp_path / "evidence")
    on = _build(tmp_path, manifest, on_session)
    try:
        on.get("meter_a").connect()
        assert on.get("meter_a").read_all().readings == reading.readings
    finally:
        on.teardown()


def test_recording_transport_keeps_the_transport_shape(session):
    """记录层是装饰:对外还是同一个 ByteStreamTransport,驱动层看不到它。"""
    inner = SimulatedByteStreamTransport(lambda request: b"reply:" + request)
    transport = session.wrap("meter_a", inner)

    assert isinstance(transport, ByteStreamTransport)
    transport.connect()
    transport.send(b"hello")

    assert transport.receive() == b"reply:hello"
    assert transport.has_client() is True
    assert [record["hex"] for record in _of_kind(session, "meter_a", "frame")] == [
        b"hello".hex(),
        b"reply:hello".hex(),
    ]


class _SilentMeter:
    """一个永远不回话的假传输:测的是"收不到应答"也留下证据。"""

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def has_client(self) -> bool:
        return True

    def send(self, data: bytes) -> None:
        pass

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        time.sleep(0.2)  # 真的等了一会儿才超时:waited 必须是从等待里量出来的
        raise TimeoutError("等不到应答")


class _BrokenLink(_SilentMeter):
    """链路断在发这一侧:设备拔了、对端把 socket 关了(ConnectionResetError 是 ConnectionError)。"""

    def send(self, data: bytes) -> None:
        raise ConnectionResetError("对端关闭了连接")


class _UnreachableLink(_SilentMeter):
    """不是对端关闭,而是网络层不可达(路由没了、EHOSTUNREACH):记 disconnect。"""

    def send(self, data: bytes) -> None:
        raise OSError("网络不可达")


@pytest.mark.parametrize(
    ("link", "kind"),
    [(_BrokenLink(), "closed"), (_UnreachableLink(), "disconnect")],
    ids=["closed", "disconnect"],
)
def test_a_link_that_breaks_while_sending_is_recorded_by_its_kind(session, link, kind):
    transport = session.wrap("meter_a", link)
    transport.connect()

    with pytest.raises(OSError):
        transport.send(b"\x01\x03")

    (send, failed) = _of_kind(session, "meter_a", "frame")
    assert send["hex"] == b"\x01\x03".hex()  # 发出去的那一帧照旧先落盘
    assert failed["direction"] == "send"
    assert failed["error"] == kind


def test_a_reply_that_never_arrives_is_recorded_as_a_timeout(session):
    """收不到应答正是最需要证据的时候:只留一个 send 的话,分不清"没回"和"回了但被拒"。
    异常照常往上抛,行为不变,只是多落一条带失败种类的帧记录。"""
    transport = session.wrap("meter_a", _SilentMeter())
    transport.connect()
    transport.send(b"\x01\x03\x00\x00\x00\x01")

    with pytest.raises(TimeoutError):
        transport.receive(timeout=0.5)

    (send, failed) = _of_kind(session, "meter_a", "frame")
    assert send["direction"] == "send"
    assert failed["direction"] == "receive"
    assert failed["error"] == "timeout"
    assert failed["timeout"] == 0.5
    assert failed["waited"] >= 0.1, f"等了 {failed['waited']} 秒,记录里对不上"
    assert "hex" not in failed, "没收到任何字节,不能编一个 hex 出来"


class _ClosedLink(_SilentMeter):
    """链路断在收这一侧:对端把连接关了(TCP 传输此时抛 ConnectionError)。"""

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        raise ConnectionError("TCP 10.0.0.5:502 对端已关闭连接")


def test_a_link_that_closes_while_receiving_is_recorded_as_closed_not_a_timeout(session):
    """断链与设备不回话是两种现场处置:断链是台架问题(测试无效),超时才可能是设备问题。
    两者在证据里必须分得开 —— 只靠 waited 接近 0 这种隐晦线索,现场读不出来。"""
    transport = session.wrap("meter_a", _ClosedLink())
    transport.connect()
    transport.send(b"\x01\x03\x00\x00\x00\x01")

    with pytest.raises(ConnectionError):
        transport.receive(timeout=0.5)

    (send, failed) = _of_kind(session, "meter_a", "frame")
    assert send["direction"] == "send"
    assert failed["direction"] == "receive"
    assert failed["error"] == "closed"
    assert "hex" not in failed


def _real_links(manifest_dict):
    """两台设备都落在真实 socket 上:断链与不回话的区别只能靠证据里的失败种类分。"""
    manifest_dict["devices"][0].update(
        access="tcp", mode="real", connection={"host": "10.0.0.5", "port": 502}
    )
    manifest_dict["devices"][1].update(mode="real", connection={"host": "10.0.0.8", "port": 502})


_SUPPORT_LINK_MANIFEST = manifest(meters=["meter_a"], support=True, mutate=_real_links)


def test_a_lost_support_link_and_a_silent_device_are_told_apart_in_the_same_round(
    tmp_path, session, monkeypatch
):
    """同一轮里两种失败必须能归到不同的结论归属上:周边设备断链记 closed 并换成 SupportLinkLost
    (台架问题,测试无效),被测设备不回话记 timeout 且原样上抛(可能是设备问题)。
    证据里两台设备各一份文件、各一种失败种类 —— "谁的问题"因此可以从证据推出来,
    不再靠 waited 接近 0 去猜。"""
    links = {"10.0.0.8": _ClosedLink(), "10.0.0.5": _SilentMeter()}
    monkeypatch.setattr(
        "testplatform.composition.root.build_transport",
        lambda access, mode, connection: links[connection["host"]],
    )
    root = _build(tmp_path, _SUPPORT_LINK_MANIFEST, session)
    try:
        root.connect_support()
        with pytest.raises(SupportLinkLost):
            root.get("grid_sim_01").apply_grid(230.0, 50.0)
        root.get("meter_a").connect()
        with pytest.raises(TimeoutError):
            root.get("meter_a").read_all()
        _round(root)
    finally:
        root.teardown()

    def kinds(device_id):
        return [
            frame["error"] for frame in _of_kind(session, device_id, "frame") if "error" in frame
        ]

    assert kinds("grid_sim_01") == ["closed"]
    assert kinds("meter_a") == ["timeout"]


class _HttpShapedTransport:
    """HttpTransport 的形状:只有 request,没有 connect / send / receive。"""

    def request(self, method: str, path: str, json=None, timeout: float = 5.0):
        raise AssertionError("这个假传输只用来证明装配期就拒掉,不该被真的调用")


def test_recording_a_non_byte_stream_device_is_rejected_at_assembly(tmp_path, session, monkeypatch):
    """证据层只实现了字节流的五个方法。非字节流设备(这里是 HTTP 形状)套进来,
    今天会在第一次连接时炸成 AttributeError('驱动写对了却连不上');应该在装配期就说清原因。"""
    monkeypatch.setattr(
        "testplatform.composition.root.build_transport",
        lambda *args, **kwargs: _HttpShapedTransport(),
    )

    with pytest.raises(TypeError) as excinfo:
        _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)

    message = str(excinfo.value)
    assert "meter_a" in message
    assert "_HttpShapedTransport" in message
    assert "字节流" in message, f"报错没说明只支持字节流规约: {message}"


def test_reading_records_which_mode_produced_it(tmp_path, session):
    """一份证据必须自证结论是模拟器还是真机跑出来的:mode 直接取组合根算好的 actual_modes。"""
    root = _build(tmp_path, copy.deepcopy(TERMINAL_BUS_MANIFEST), session)
    try:
        for device_id in ("terminal_01", "meter_a"):
            root.get(device_id).connect()
            root.get(device_id).read_all()
        _round(root)
    finally:
        root.teardown()

    (terminal_reading,) = _of_kind(session, "terminal_01", "reading")
    (meter_reading,) = _of_kind(session, "meter_a", "reading")
    assert terminal_reading["mode"] == "simulator"
    # 清单里没写 mode 时组合根按 real 算(meter_a 借终端那条总线,自己不开物理连接)。
    assert meter_reading["mode"] == root.actual_modes["meter_a"] == "real"


# ---------------------------------------------------------------- 句柄生命周期

class _Unwritable:
    """写不进去的一条记录:json 序列化走到 str() 时抛错,模拟写入路径中途炸掉。"""

    def __str__(self):
        raise OSError("写不进去")


def test_one_handle_per_device_not_one_open_per_record(tmp_path, session, monkeypatch):
    """open 次数跟着设备数走,不跟着记录条数走。

    这是本轮唯一随规模线性放大的开销:一轮 5940 条记录时逐条 open/close 要 8 秒。
    断 open 次数而不是断言耗时 —— 耗时在 CI 上会抖,次数是确定的。
    """
    opened = []
    real_open = Path.open

    def counted(self, *args, **kwargs):
        if Path(self).parent == session.directory:
            opened.append(Path(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    for _ in range(200):
        session.record("meter_a", {"kind": "frame", "direction": "send", "hex": "01"})
        session.record("meter_b", {"kind": "frame", "direction": "send", "hex": "02"})
    session.close()

    assert len(opened) == 2, f"400 条记录该只开 2 次文件(每台设备一个句柄),实际 {len(opened)}"
    assert sorted(path.name for path in opened) == ["meter_a.jsonl", "meter_b.jsonl"]
    assert len(_of_kind(session, "meter_a", "frame")) == 200
    assert len(_of_kind(session, "meter_b", "frame")) == 200


def test_records_already_written_survive_a_failing_write(tmp_path, session):
    """每条记录写完就 flush:第三条写不进去时,前两条必须已经在文件里。

    攒在内存里批量写会让崩溃丢掉最后几条,那正是证据层最不能接受的 ——
    这个用例读文件时不关会话,flush 没做事就会读到空文件。
    """
    session.record("meter_a", {"kind": "frame", "direction": "send", "hex": "01"})
    session.record("meter_a", {"kind": "frame", "direction": "receive", "hex": "02"})

    with pytest.raises(OSError):
        session.record("meter_a", {"kind": "frame", "direction": "send", "hex": _Unwritable()})

    assert [frame["hex"] for frame in _of_kind(session, "meter_a", "frame")] == ["01", "02"]


def test_teardown_closes_the_evidence_handles(tmp_path, session):
    """收尾把句柄关掉:Windows 上句柄没关就删不掉文件,这一步就是"已关闭"的证据。"""
    root = _build(tmp_path, _manifest([meter_device("meter_a", 1, 2300)]), session)
    try:
        root.get("meter_a").connect()
        root.get("meter_a").read_all()
        _round(root)
    finally:
        root.teardown()

    path = session.path_of("meter_a")
    path.unlink()
    assert not path.exists()


PARALLEL_MANIFEST = manifest(inverters=4)

PARALLEL_CONFTEST = '''
import pytest

from testplatform.cases import case_of

pytest_plugins = ["testplatform.plugin"]


@pytest.fixture(autouse=True)
def _record_each_case(composition_root, request):
    yield
    composition_root.record_evidence(identity=case_of(request.node))
'''

PARALLEL_CASES = '''
import pytest


@pytest.mark.parametrize("device_id", ["inv_01", "inv_02", "inv_03", "inv_04"])
def test_each_device_reads_its_power(composition_root, device_id):
    driver = composition_root.get(device_id)
    driver.connect()
    driver.read("有功功率")
'''


def test_parallel_workers_keep_their_own_evidence(tmp_path):
    """-n 4 时每个 worker 各开自己的会话与句柄:一轮跑完证据齐全、不串文件。

    "不串文件"指两件事:同一台设备的记录只出现在一个 worker 的目录里;
    每份文件里的 device 字段与文件名一致(两个 worker 抢写同一份会立刻看得到)。
    """
    (tmp_path / "conftest.py").write_text(PARALLEL_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(PARALLEL_MANIFEST, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_parallel_evidence.py").write_text(PARALLEL_CASES, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }

    result = subprocess.run(
        pytest_command(
            "-q",
            "--device-config",
            "devices.yaml",
            "test_parallel_evidence.py",
            "-n",
            "4",
            "--dist",
            "loadgroup",
        ),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    assert result.returncode == 0, output
    assert "4 passed" in output, output

    directories = sorted((tmp_path / "evidence").iterdir())
    assert len({path.name.rsplit("-", 1)[-1] for path in directories}) > 1, "没跑成并行"
    written = {}
    for directory in directories:
        for path in sorted(directory.glob("*.jsonl")):
            records = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            ]
            if not records:
                continue  # 装配了但没读到的设备留空文件(占位),保持现状
            assert {record["device"] for record in records} == {path.stem}, (
                f"{directory.name}/{path.name} 里混进了别的设备的记录"
            )
            assert path.stem not in written, (
                f"{path.stem} 同时写进了 {written[path.stem]} 和 {directory.name}"
            )
            written[path.stem] = directory.name
    assert sorted(written) == [spec["id"] for spec in PARALLEL_MANIFEST["devices"]]
