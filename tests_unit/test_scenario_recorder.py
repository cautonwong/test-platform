"""ScenarioRecorder:空步骤不能算 passed;step 只从实际求值结果取结论。"""
import json
from pathlib import Path

import pytest

from testplatform.reporting.evidence import EvidenceSession
from testplatform.reporting.scenario import ScenarioRecorder


class _Root:
    def __init__(self, evidence=None) -> None:
        self.evidence = evidence

    def role_of(self, device_id: str) -> str:
        return "dut"

    def record_evidence(self, **kwargs) -> None:
        pass


class _Device:
    id = "d1"


class _Reading:
    passed = False
    readings = {"电压": 230.0}
    breaches = ()


def _recorder() -> ScenarioRecorder:
    return ScenarioRecorder(
        _Root(), identity=None, name="s", resolve=lambda device_id: _Device()
    )


def test_record_without_steps_is_rejected():
    with pytest.raises(ValueError, match="没有任何步骤"):
        _recorder().record()


def test_step_requires_a_real_verdict():
    scenario = _recorder()
    with pytest.raises(ValueError, match="passed"):
        scenario.step(_Device(), "做一件事")


def test_step_accepts_a_bool_or_a_reading():
    scenario = _recorder()

    assert scenario.step(_Device(), "设定值回显", True).passed is True
    assert scenario.step(_Device(), "读数求值", _Reading()).passed is False


def test_measure_records_the_reading_verdict(tmp_path: Path):
    root = _Root(EvidenceSession(str(tmp_path)))
    scenario = ScenarioRecorder(root, identity=None, name="s", resolve=lambda _: _Device())
    device = _Device()
    device.read_all = lambda: _Reading()

    measured = scenario.measure(device)
    path = scenario.record()

    assert measured.passed is False
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["reading_keys"] == {"d1": ["电压"]}
    assert record["steps"][0]["passed"] is False
