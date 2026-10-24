"""
场景测试层的第一条真实用例:电网模拟器把频率设定到 50Hz → 读逆变器 → 判据集判合格 → 场景结论成立。

参与方按 id 显式点名(不遍历、不用默认设备集),role 取自设备清单;每个场景跑完,证据会话目录下
多一份 scenario-<用例名>.json,场景结论由各步骤的实际求值结果算出,不是用例里手写的 True。

下面是正例(场景通过)、反例(单台设备不合格 → 场景不通过),以及"设定 → 等一小段 → 读 → 判"
(等待落成记录里的一步,带计划时长与真量出来的实际时长,但它本身不判通过)。
形状与"不支持什么"见 docs/adr/0005-scenario-layer-shape.md。
"""
import json
from pathlib import Path

import pytest
import yaml

from testplatform.cases import case, case_of
from testplatform.composition.root import CompositionRoot
from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.scenario import ScenarioStep, record_scenario, settle

from tests_support.named_device import device_or_skip

ROOT = Path(__file__).resolve().parents[1]

GRID, INVERTER = "grid_sim_01", "inv_01"
SETPOINT_V, SETPOINT_HZ = 230.0, 50.0
SETTLE_S = 0.2


@case(id="S_00_00_01_000001_01", clause="GBT37408-5.2.1", tags=["场景", "验收"])
def test_inverter_holds_the_grid_frequency_setpoint(composition_root, request):
    """电网模拟器按设定输出 50Hz,逆变器的读数经判据集求值合格 —— 场景通过。"""
    if composition_root.evidence is None:
        pytest.skip("--no-evidence 下没有证据目录,这条场景的结论无从落盘")

    grid = device_or_skip(composition_root, GRID)
    grid.connect()
    setpoint = grid.apply_grid(SETPOINT_V, SETPOINT_HZ)

    inverter = device_or_skip(composition_root, INVERTER)
    inverter.connect()
    measured = inverter.read_all()  # 读数键 → 物理量
    evaluation = inverter.device.latest_reading  # 聚合根记下的那条读数,带判据结论

    steps = [
        ScenarioStep(
            GRID,
            f"设定电网 {SETPOINT_V}V / {SETPOINT_HZ}Hz",
            abs(setpoint.freq_hz - SETPOINT_HZ) < 0.1,
            {"voltage_v": setpoint.voltage_v, "freq_hz": setpoint.freq_hz},
        ),
        ScenarioStep(
            INVERTER,
            "按点表读全部读数键并由判据集求值",
            evaluation.passed,
            {
                "readings": dict(measured.readings),
                "breaches": [breach.describe() for breach in evaluation.breaches],
            },
        ),
    ]
    composition_root.record_evidence(identity=case_of(request.node))
    path = record_scenario(
        composition_root.evidence,
        name=request.node.originalname,
        identity=case_of(request.node),
        participants={
            GRID: composition_root.role_of(GRID),
            INVERTER: composition_root.role_of(INVERTER),
        },
        reading_keys={INVERTER: tuple(measured.readings)},
        steps=steps,
    )

    assert path is not None, "默认套件开着证据记录,这条场景必须能落盘"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["participants"] == [
        {"id": GRID, "role": "support"},
        {"id": INVERTER, "role": "dut"},
    ]
    # 场景本身要成立的那条跨设备事实:逆变器报的频率跟着电网设定值。
    assert evaluation.passed is True
    assert abs(evaluation.grid_freq_hz - setpoint.freq_hz) <= 0.5


@case(id="S_00_00_01_000002_01", clause="GBT37408-5.2.1", tags=["场景", "反例"])
def test_scenario_fails_when_one_dut_reading_breaks_the_criteria(tmp_path, request):
    """反例:周边设备照样按设定输出 50Hz,逆变器却报 60Hz —— 单台设备不合格,场景必须不通过。

    结论来自实际求值(判据集在聚合根上判出的 passed=False),不是把 verdict 写死。
    用的是真清单的一份副本,只把那台逆变器的应答改成 60Hz。
    """
    manifest = yaml.safe_load((ROOT / "configs" / "devices.yaml").read_text(encoding="utf-8"))
    for device in manifest["devices"]:
        if device["id"] == INVERTER:
            device["connection"]["simulated_response"] = "3000.0 230.0 60.0"
    config = tmp_path / "devices.yaml"
    config.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")

    evidence = EvidenceSession(tmp_path / "evidence")
    root = CompositionRoot.build(str(config), evidence=evidence)
    try:
        grid = root.get(GRID)
        grid.connect()
        setpoint = grid.apply_grid(SETPOINT_V, SETPOINT_HZ)

        inverter = root.get(INVERTER)
        inverter.connect()
        measured = inverter.read_all()
        evaluation = inverter.device.latest_reading

        grid_step = ScenarioStep(
            GRID,
            f"设定电网 {SETPOINT_V}V / {SETPOINT_HZ}Hz",
            abs(setpoint.freq_hz - SETPOINT_HZ) < 0.1,
            {"voltage_v": setpoint.voltage_v, "freq_hz": setpoint.freq_hz},
        )
        dut_step = ScenarioStep(
            INVERTER,
            "按点表读全部读数键并由判据集求值",
            evaluation.passed,
            {
                "readings": dict(measured.readings),
                "breaches": [breach.describe() for breach in evaluation.breaches],
            },
        )
        path = record_scenario(
            evidence,
            name=request.node.originalname,
            identity=case_of(request.node),
            participants={
                GRID: root.role_of(GRID),
                INVERTER: root.role_of(INVERTER),
            },
            reading_keys={INVERTER: tuple(measured.readings)},
            steps=[grid_step, dut_step],
        )
        record = json.loads(path.read_text(encoding="utf-8"))

        assert grid_step.passed is True, "周边设备这一步照常成立"
        assert evaluation.passed is False, "单台设备的判据结论:不合格"
        assert record["verdict"] == "failed", "场景结论:不通过"
        assert [step["device"] for step in record["steps"] if not step["passed"]] == [INVERTER]
        assert "grid_freq_hz=60.0 越过 55" in record["steps"][1]["detail"]["breaches"][0]
    finally:
        root.teardown()


@case(id="S_00_00_01_000003_01", clause="GBT37408-5.2.1", tags=["场景"])
def test_the_wait_between_setpoint_and_reading_is_recorded_with_its_measured_duration(
    composition_root, request
):
    """设定频率 → settle 0.2 秒 → 读 → 判:等多久进了记录,但等待本身不判通过。

    时长取 0.2 秒,默认套件不为它多花看得出来的时间;真条款里的 60 秒走字用同一个 settle。
    """
    if composition_root.evidence is None:
        pytest.skip("--no-evidence 下没有证据目录,这条场景的结论无从落盘")

    grid = device_or_skip(composition_root, GRID)
    grid.connect()
    setpoint = grid.apply_grid(SETPOINT_V, SETPOINT_HZ)

    inverter = device_or_skip(composition_root, INVERTER)
    inverter.connect()
    wait = settle(SETTLE_S, "等逆变器跟上设定值")  # 台架保持设定,这 0.2 秒里不动被测表
    measured = inverter.read_all()
    evaluation = inverter.device.latest_reading

    steps = [
        ScenarioStep(
            GRID,
            f"设定电网 {SETPOINT_V}V / {SETPOINT_HZ}Hz",
            abs(setpoint.freq_hz - SETPOINT_HZ) < 0.1,
            {"voltage_v": setpoint.voltage_v, "freq_hz": setpoint.freq_hz},
        ),
        wait,
        ScenarioStep(
            INVERTER,
            "按点表读全部读数键并由判据集求值",
            evaluation.passed,
            {
                "readings": dict(measured.readings),
                "breaches": [breach.describe() for breach in evaluation.breaches],
            },
        ),
    ]
    composition_root.record_evidence(identity=case_of(request.node))
    path = record_scenario(
        composition_root.evidence,
        name=request.node.originalname,
        identity=case_of(request.node),
        participants={
            GRID: composition_root.role_of(GRID),
            INVERTER: composition_root.role_of(INVERTER),
        },
        reading_keys={INVERTER: tuple(measured.readings)},
        steps=steps,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    waited = record["steps"][1]
    assert record["verdict"] == "passed"
    assert waited["device"] is None, "等待没有设备参与,不归给任何一台"
    assert waited["action"] == "等待 0.2s(等逆变器跟上设定值)"
    assert waited["detail"]["target_s"] == SETTLE_S
    # 量出来的:真的等够了,又不是把配置值抄了一遍或者卡住很久。
    # 下界留 25% 余量,是给时钟粒度不是给实现:Windows 上 monotonic 走 GetTickCount64,
    # 步长恒为 15.6ms,前后取两个时刻各被截断一次,0.2s 的 sleep 能量成 0.1875(12 个 tick)
    # —— 抓它当失败,是拿时钟的粒度判 settle,不是 settle 有问题。
    # 上界不跟着放松:被拖长/被打断才是这条断言要抓的回归。
    assert SETTLE_S * 0.75 <= waited["detail"]["actual_s"] < 2.0
