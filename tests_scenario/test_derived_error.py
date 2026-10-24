"""
跨设备派生量端到端:两台电表各走一段字 → 算增量与误差 → 由清单里的区间判 → 写进场景证据。

上游 29 条走字条款的核心判据是误差 —— `(被测表增量 − 标准表增量) / 标准表增量`。
这个量横跨两台表,判据集的 `quantity`(单台设备的读数键)表达不了,所以:

    清单  只声明"这个派生判据叫什么、谁参与、允许落在哪个区间"
    用例  用普通 Python 算增量与误差(公式不进 yaml:那是 ADR 0002 / 0005 拒绝的 DSL)
    组合根 judge_derived() 拿清单里的区间判那个算好的值

模拟从站没有"走字"的物理过程(真台架靠 settle 等表自己走),所以这里用写原语把表的读数
推到下一时刻 —— 走字这一步的 passed 是**回读结果**与目标值的比较,不是"我发过了"。
清单是测试内的临时清单(tmp_path),不碰 configs/devices.yaml。
"""
import copy
import json

import pytest
import yaml

from testplatform.cases import case, case_of
from testplatform.composition.root import CompositionRoot
from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.scenario import ScenarioStep, record_scenario

DUT, REFERENCE = "meter_dut", "meter_reference"
ENERGY = "有功电量"
DERIVED = "有功误差"

# 一台模拟表:有功电量 = 寄存器 0/1 拼出来的 uint32 × 0.01 kWh。
# 起始 100.00 kWh,两个寄存器都要在表里 —— 4 字节的点写下去是 0x10 写两个寄存器。
BASE_MANIFEST = {
    "criteria_profiles": {
        "meter_220v": [{"quantity": ENERGY, "min": 0}],
    },
    "derived_criteria": {
        DERIVED: {
            "participants": [DUT, REFERENCE],
            "limits": {"min": -1.0, "max": 1.0},
        }
    },
    "models": {
        "dtsd1352_modbus": {
            "type": "meter",
            "protocol": "modbus_rtu",
            "criteria": "meter_220v",
            "capabilities": [ENERGY],
            "point_table": {
                # writable 是模拟台架的让步:模拟从站不会自己走字,由用例把读数推到下一时刻。
                ENERGY: {
                    "address": 0,
                    "length": 4,
                    "encoding": "uint",
                    "scale": 0.01,
                    "unit": "kWh",
                    "writable": True,
                },
            },
        },
    },
    "devices": [
        {
            "id": DUT,
            "model": "dtsd1352_modbus",
            "access": "serial",
            "mode": "simulator",
            "bus_address": 1,
            "connection": {"port": "/dev/ttyUSB9", "simulated_registers": {0: 0, 1: 10000}},
        },
        {
            "id": REFERENCE,
            "model": "dtsd1352_modbus",
            "role": "support",
            "access": "serial",
            "mode": "simulator",
            "bus_address": 2,
            "connection": {"port": "/dev/ttyUSB10", "simulated_registers": {0: 0, 1: 10000}},
        },
    ],
}


def _error_percent(delta_dut: float, delta_ref: float) -> float:
    """上游条款的误差定义:被测表增量减标准表增量,除以**标准表**增量(单位 %)。

    顺序写反了符号就翻过来,而符号错的结果看起来像"超差"而不是"算错了" ——
    test_swapping_dut_and_reference_turns_the_error_negative 专门锁这一条。
    """
    return (delta_dut - delta_ref) / delta_ref * 100


def _walk_step(meter, device_id: str, delta_kwh: float) -> ScenarioStep:
    """让一块表走一段字:读一次当前值 → 用写原语推到 当前值 + 增量 → 回读确认。"""
    before = meter.read(ENERGY)
    read_back = meter.write(ENERGY, before + delta_kwh)
    return ScenarioStep(
        device_id,
        f"走字 {delta_kwh:g} kWh({device_id})",
        # 写成功 ≠ 值生效:这一步成立与否看回读到的数,不看"我把帧发出去了"。
        read_back == pytest.approx(before + delta_kwh),
        {"before_kwh": before, "after_kwh": read_back, "delta_kwh": read_back - before},
    )


def _record_walk(tmp_path, request, *, dut_kwh: float, reference_kwh: float, limits: dict):
    """跑一遍"两台表各走字 → 算误差 → 判派生判据 → 记场景",返回 (场景记录, 关键数字)。"""
    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["derived_criteria"][DERIVED]["limits"] = limits
    config = tmp_path / "devices.yaml"
    config.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")

    evidence = EvidenceSession(tmp_path / "evidence")
    root = CompositionRoot.build(str(config), evidence=evidence)
    try:
        dut, reference = root.get(DUT), root.get(REFERENCE)
        dut.connect()
        reference.connect()

        walk_dut = _walk_step(dut, DUT, dut_kwh)
        walk_ref = _walk_step(reference, REFERENCE, reference_kwh)
        delta_dut = walk_dut.detail["delta_kwh"]
        delta_ref = walk_ref.detail["delta_kwh"]
        error_pct = _error_percent(delta_dut, delta_ref)
        criterion = root.derived_criterion(DERIVED)

        judge_step = ScenarioStep(
            DUT,
            f"派生判据 {DERIVED} 在区间内",
            # 结论来自区间求值,不是写死的字面量。
            root.judge_derived(DERIVED, error_pct),
            {
                # 证据要能复现结论:判的是哪个派生量、区间多少、实测值多少、两台表各走了多少。
                "derived": DERIVED,
                "participants": list(criterion.participants),
                "limits": {"min": criterion.limits.low, "max": criterion.limits.high},
                "error_pct": error_pct,
                "delta_dut": delta_dut,
                "delta_ref": delta_ref,
            },
        )
        path = record_scenario(
            evidence,
            name=request.node.originalname,
            identity=case_of(request.node),
            participants={
                DUT: root.role_of(DUT),
                REFERENCE: root.role_of(REFERENCE),
            },
            reading_keys={DUT: (ENERGY,), REFERENCE: (ENERGY,)},
            steps=[walk_dut, walk_ref, judge_step],
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        # 方向锁:公式写反会落在区间另一侧(区间故意不对称 —— 对称区间看不出符号)。
        reversed_pct = _error_percent(delta_ref, delta_dut)
        return record, {
            "error_pct": error_pct,
            "reversed_pct": reversed_pct,
            "reversed_passed": root.judge_derived(DERIVED, reversed_pct),
        }
    finally:
        root.teardown()


@case(id="S_00_00_03_000001_01", clause="GBT17215.321-走字误差", tags=["场景", "派生量"])
def test_derived_error_within_the_limits_passes_the_scenario(tmp_path, request):
    """两块表走得一样多 → 误差 0.0% → 清单里的区间 [-1.0, 1.0] 判合格 → 场景通过。

    证据里能看到判据名、区间与实测值:光有绿色看不出判的是哪条、标准是多少。
    """
    record, facts = _record_walk(
        tmp_path, request, dut_kwh=100.0, reference_kwh=100.0, limits={"min": -1.0, "max": 1.0}
    )

    assert facts["error_pct"] == pytest.approx(0.0)
    assert record["verdict"] == "passed"
    judged = record["steps"][2]
    assert judged["device"] == DUT, "误差是被测表的误差,归给它"
    assert judged["passed"] is True
    assert judged["detail"]["derived"] == DERIVED
    assert judged["detail"]["participants"] == [DUT, REFERENCE]
    assert judged["detail"]["limits"] == {"min": -1.0, "max": 1.0}
    assert judged["detail"]["error_pct"] == pytest.approx(0.0)
    assert judged["detail"]["delta_dut"] == pytest.approx(100.0)
    assert judged["detail"]["delta_ref"] == pytest.approx(100.0)


@case(id="S_00_00_03_000002_01", clause="GBT17215.321-走字误差", tags=["场景", "派生量", "反例"])
def test_derived_error_beyond_the_limits_fails_the_scenario(tmp_path, request):
    """被测表走得比标准表快 2%(标准表只走了 100 kWh)→ 超出 max: 1.0 → 场景不通过。

    结论来自 judge_derived 的实际求值,记录里同一个值能复算出同一个结论。
    """
    record, facts = _record_walk(
        tmp_path, request, dut_kwh=102.0, reference_kwh=100.0, limits={"min": -1.0, "max": 1.0}
    )

    assert facts["error_pct"] == pytest.approx(2.0)
    assert record["verdict"] == "failed"
    judged = record["steps"][2]
    assert judged["passed"] is False
    assert judged["detail"]["limits"] == {"min": -1.0, "max": 1.0}
    assert judged["detail"]["error_pct"] == pytest.approx(2.0)
    # 超差就是超差,不是"测试无效":那一步落在被测表上。
    assert [step["device"] for step in record["steps"] if not step["passed"]] == [DUT]
    assert record["note"].startswith("有被测设备的步骤不成立")


@case(id="S_00_00_03_000003_01", clause="GBT17215.321-走字误差", tags=["场景", "派生量", "方向"])
def test_swapping_dut_and_reference_turns_the_error_negative(tmp_path, request):
    """方向锁:被测表快 0.8%,反向算得到 -0.79% —— 符号相反,而区间不对称(-0.5 / 1.0),
    所以写法一错结论就翻:正向合格、反向不合格。符号错的结果看起来像"超差"而不是"算错了",
    这条用例就是让那种错在跑起来的时候被抓到,而不是在报告里被当设备不合格。
    """
    record, facts = _record_walk(
        tmp_path, request, dut_kwh=100.8, reference_kwh=100.0, limits={"min": -0.5, "max": 1.0}
    )

    assert facts["error_pct"] == pytest.approx(0.8)
    assert facts["error_pct"] > 0 > facts["reversed_pct"]
    assert record["verdict"] == "passed", "正向算:0.8% 在 [-0.5, 1.0] 之内"
    assert facts["reversed_passed"] is False, "分子分母写反:-0.79% 掉到下限之外"
