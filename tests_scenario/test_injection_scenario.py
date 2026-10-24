"""
场景里的数据注入(票 06 的正例):把电表报的电量设成 4321,下一步读到的就是 4321,场景结论成立。

注入经 `inject` fixture 落一条证据,带这条用例的身份 —— 事后能看出"这个值是测试摆的"。
读完把电量设回原值:会话级的模拟器整轮共用,不还原会把后面每一条读这台表的用例一起改掉。
还原也只能靠再注入一次 —— 注入不是写路径,它没改过设备,也就没有"写回"这回事。
"""
from __future__ import annotations

import json

import pytest

from testplatform.cases import case, case_of
from testplatform.reporting.scenario import ScenarioStep, record_scenario

from tests_support.named_device import device_or_skip

METER = "meter_01"
INJECTED_ENERGY = 4321.0
ORIGINAL_ENERGY = 1234.56
CASE_ID = "S_00_00_05_000001_01"


@case(id=CASE_ID, clause="free: 平台:数据注入", tags=["场景", "注入"])
def test_injected_energy_is_what_the_meter_reports_next(
    composition_root, inject, scenario, request
):
    if composition_root.evidence is None:
        pytest.skip("--no-evidence 下没有证据目录,注入留痕无从落盘")

    meter = device_or_skip(composition_root, METER)
    meter.connect()
    scenario.on_cleanup(
        lambda: inject(METER, "有功电量", ORIGINAL_ENERGY),
        label=f"把电量设回 {ORIGINAL_ENERGY}",
    )

    entry = inject(METER, "有功电量", INJECTED_ENERGY)
    assert entry["kind"] == "injection"
    assert entry["case"]["id"] == CASE_ID, "注入留痕必须带设置它的那条用例的身份"

    measured = meter.read_all()
    reported = measured.readings["有功电量"]
    step = ScenarioStep(
        METER,
        f"读取被注入的电量 {INJECTED_ENERGY}",
        abs(reported - INJECTED_ENERGY) < 0.001,
        {"readings": dict(measured.readings)},
    )

    path = record_scenario(
        composition_root.evidence,
        name=request.node.originalname,
        identity=case_of(request.node),
        participants={METER: composition_root.role_of(METER)},
        reading_keys={METER: ("有功电量",)},
        steps=[step],
        cleanups=scenario,
    )
    assert path is not None

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "passed"
    assert record["cleanup"] and record["cleanup"][0]["passed"] is True
