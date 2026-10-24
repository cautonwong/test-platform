"""
数据注入(票 06):把一个模拟设备的读数设成指定值,下一步读到的就是它,并且这件事留痕。

三条边界各有守卫:
    注入只对 mode: simulator 生效(真设备上的读数只能由设备自己变)
    注入点必须是点表里已声明的量(拼错当场报错,并列出可用键)
    注入不是写路径 —— 它不产生 frame / write 记录,证据里只有一条 injection

"不产生 write" 这条不是洁癖:一条绿用例如果分不清"产品会写设备"和"测试替它写了",
那这条用例证明不了任何关于产品的事。
"""
from __future__ import annotations

import json

import pytest

from testplatform.cases import Case
from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.injection import inject
from testplatform.reporting.scenario import ScenarioStep, record_scenario

from tests_support.manifest_factory import build, manifest

METER = "meter_01"
IDENTITY = Case(id="T_00_00_05_000000_01", clause="free: 平台:数据注入", tags=("注入",))
ENERGY = 4321.0


def _records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _root_with_meter(tmp_path, session=None):
    session = session or EvidenceSession(tmp_path / "evidence")
    root = build(tmp_path, manifest(meters=[METER]), evidence=session)
    return root, session


# ------------------------------------------------------- 注入生效 + 留痕 ----

def test_injected_value_is_what_the_next_read_returns(tmp_path):
    """设成 4321 之后读到的就是 4321;证据里只有一条 injection,没有 frame / write。"""
    root, session = _root_with_meter(tmp_path)
    try:
        meter = root.get(METER)
        meter.connect()

        entry = inject(root, METER, "有功电量", ENERGY, identity=IDENTITY)

        # 注入这一步不经协议:证据里此刻只该有它自己这一条
        records = _records(session.path_of(METER))
        assert [record["kind"] for record in records] == ["injection"], records
        assert (entry["value"], entry["key"], entry["mode"]) == (ENERGY, "有功电量", "simulator")
        assert entry["case"]["id"] == IDENTITY.id

        # 产品视角:下一次读拿到的就是刚设的值(读本身当然会发帧)
        measured = meter.read_all()
        assert measured.readings["有功电量"] == pytest.approx(ENERGY)
    finally:
        root.teardown()


def test_injection_never_produces_a_write_record(tmp_path):
    """守卫:一次注入 + 一次读数,证据里出现 write 就说明有人把注入走了写路径。"""
    root, session = _root_with_meter(tmp_path)
    try:
        meter = root.get(METER)
        meter.connect()
        inject(root, METER, "有功电量", ENERGY, identity=IDENTITY)
        meter.read_all()
    finally:
        root.teardown()

    kinds = [record["kind"] for record in _records(session.path_of(METER))]
    assert "injection" in kinds
    assert "write" not in kinds, f"注入不该产生写记录,实际 kinds: {kinds}"


def test_injecting_a_reading_key_that_the_model_does_not_declare_fails(tmp_path):
    """注入点写错当场报错,并列出这台型号的读数键 —— 静默不生效会让用例以为设过了。"""
    root, _session = _root_with_meter(tmp_path)
    try:
        with pytest.raises(KeyError) as error:
            inject(root, METER, "有功电亮", ENERGY, identity=IDENTITY)
    finally:
        root.teardown()

    message = str(error.value)
    assert "有功电亮" in message
    assert "有功电量" in message, f"报错没列可用读数键:{message}"


# ------------------------------------------------- 只对模拟设备生效 ----

def test_a_real_device_cannot_be_injected(tmp_path):
    """真设备上的读数只能由设备自己改变:能"设"就说明设的不是设备。"""
    def _real(manifest_dict):
        manifest_dict["devices"][0].update(
            access="tcp", mode="real", connection={"host": "127.0.0.1", "port": 1}
        )

    session = EvidenceSession(tmp_path / "evidence")
    root = build(tmp_path, manifest(meters=[METER], mutate=_real), evidence=session)
    try:
        with pytest.raises(ValueError) as error:
            inject(root, METER, "有功电量", ENERGY, identity=IDENTITY)
    finally:
        root.teardown()

    assert "mode: real" in str(error.value)


def test_a_simulator_without_point_injection_says_so_and_lists_what_supports_it(tmp_path):
    """文本模拟器接不了按点注入:报错要说清,并指出哪个模拟器支持(不是一句"失败了")。"""
    root = build(tmp_path, manifest(inverters=["inv_01"]))
    try:
        with pytest.raises(ValueError) as error:
            inject(root, "inv_01", "有功功率", 3000.0, identity=IDENTITY)
    finally:
        root.teardown()

    message = str(error.value)
    assert "不支持按点注入" in message
    assert "data_injection" in message, f"报错要指出能力声明在哪儿:{message}"


# ------------------------------------------------- 注入值必须读得回来 ----

def test_an_unrepresentable_value_fails_at_injection_time_not_at_the_assertion(tmp_path):
    """scale 0.01 上注 1234.567,读回来是 1234.57 —— 注入另一个数必须当场拒绝。"""
    root, _session = _root_with_meter(tmp_path)
    try:
        with pytest.raises(ValueError) as error:
            inject(root, METER, "有功电量", 1234.567, identity=IDENTITY)
    finally:
        root.teardown()

    message = str(error.value)
    assert "读不回来" in message
    assert "1234.567" in message and "1234.57" in message, message
    assert "scale=0.01" in message and "length=4" in message, (
        f"报错要给出这个点的 scale 与 length,人才能判断该换成什么值:{message}"
    )


def test_injection_and_the_scenario_record_say_the_same_thing(tmp_path):
    """场景里改读数这条路的证据对得上:per-device 一条 injection,scenario-*.json 一步 passed。

    场景层拿到的就是这个 API(identity 由用例给):注入不改设备,只改"设备现在报什么",
    所以场景结论验的是"读到被设的值之后成立",不是"设备会自己变成这个值"。
    """
    root, session = _root_with_meter(tmp_path)
    try:
        meter = root.get(METER)
        meter.connect()
        inject(root, METER, "有功电量", ENERGY, identity=IDENTITY)
        measured = meter.read_all()
        step = ScenarioStep(
            METER,
            f"读取被注入的电量 {ENERGY}",
            measured.readings["有功电量"] == pytest.approx(ENERGY),
            {"readings": dict(measured.readings)},
        )
        path = record_scenario(
            session,
            name="test_injection_and_the_scenario_record_say_the_same_thing",
            identity=IDENTITY,
            participants={METER: root.role_of(METER)},
            reading_keys={METER: ("有功电量",)},
            steps=[step],
        )
    finally:
        root.teardown()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["case"]["id"] == IDENTITY.id
    kinds = [entry["kind"] for entry in _records(session.path_of(METER))]
    assert kinds.count("injection") == 1
    assert "write" not in kinds


def test_injection_feeds_the_criteria_so_a_bad_value_is_judged(tmp_path):
    """注入不是把结论改成通过:设一个越界的电压,判据集照样判不合格。"""
    root, _session = _root_with_meter(tmp_path)
    try:
        meter = root.get(METER)
        meter.connect()
        inject(root, METER, "电压", 100.0, identity=IDENTITY)
        measured = meter.read_all()
    finally:
        root.teardown()

    assert measured.readings["电压"] == pytest.approx(100.0)
    assert measured.passed is False
    assert [breach.quantity for breach in measured.breaches] == ["电压"]
