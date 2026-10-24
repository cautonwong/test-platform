"""settle 只等一段固定时长:实际时长必须量出来,这一步又不进场景结论。

这条纯规则不经过任何设备,所以放在单测层;真等 1 秒的用法在 tests_scenario 里。
"""
import pytest

from testplatform.reporting import scenario
from testplatform.reporting.scenario import SETTLE_LIMIT_S, ScenarioStep, settle, verdict_of

PARTICIPANTS = {"grid_sim_01": "support", "inv_01": "dut"}


class _FakeClock:
    """假时钟:换掉 scenario 里的 time 模块名。sleep 只记录不真等,
    还能让"时钟走的秒数"与"睡下去的秒数"不一致 —— 那正是"量出来的"与"抄配置"的分界。"""

    def __init__(self, drift: float = 0.0) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.drift = drift

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds + self.drift


def test_actual_s_is_measured_not_copied_from_the_target(monkeypatch):
    """睡 1 秒、时钟却走了 2 秒(被拖长了):actual_s 报 2,不是把 target_s 抄一遍。"""
    clock = _FakeClock(drift=1.0)
    monkeypatch.setattr(scenario, "time", clock)

    step = settle(1.0, "走字")

    assert clock.sleeps == [1.0]
    assert step.detail["target_s"] == 1.0
    assert step.detail["actual_s"] == 2.0


def test_a_short_interrupted_wait_shows_up_as_a_smaller_actual_s(monkeypatch):
    """时钟只走了 0.4 秒就"回来"了:记录里对得上,才看得出这次没等够。"""
    monkeypatch.setattr(scenario, "time", _FakeClock(drift=-0.6))

    step = settle(1.0, "走字")

    assert step.detail["actual_s"] == pytest.approx(0.4)


def test_settle_records_the_reason_and_reads_as_one_step(monkeypatch):
    monkeypatch.setattr(scenario, "time", _FakeClock())

    step = settle(0.5, "等逆变器跟上设定值")

    assert step.action == "等待 0.5s(等逆变器跟上设定值)"
    assert step.detail == {"reason": "等逆变器跟上设定值", "target_s": 0.5, "actual_s": 0.5}
    assert step.device is None, "没有设备参与:结论里不归给谁"


def test_a_wait_longer_than_the_limit_is_refused_with_the_limit_and_the_way_out():
    with pytest.raises(ValueError) as raised:
        settle(SETTLE_LIMIT_S + 1.0, "手滑写了 3600")

    message = str(raised.value)
    assert f"上限 {SETTLE_LIMIT_S}s" in message
    assert "confirm_long=True" in message


def test_confirm_long_lets_a_deliberately_long_wait_through(monkeypatch):
    """显式确认后放行:走字条款里的 60 秒本来就该能写(这里用假时钟不真等)。"""
    monkeypatch.setattr(scenario, "time", _FakeClock())

    step = settle(SETTLE_LIMIT_S + 1.0, "长时走字", confirm_long=True)

    assert step.detail["target_s"] == SETTLE_LIMIT_S + 1.0


def test_a_negative_wait_is_refused(monkeypatch):
    monkeypatch.setattr(scenario, "time", _FakeClock())

    with pytest.raises(ValueError, match="不能为负"):
        settle(-1.0, "写反了")


def test_a_settle_step_never_decides_the_verdict(monkeypatch):
    """等待不构成结论:它既不把失败的场景救回来,也不改变失败落在谁身上。"""
    monkeypatch.setattr(scenario, "time", _FakeClock())
    wait = settle(1.0, "等稳定")
    grid_ok = ScenarioStep("grid_sim_01", "设定电网", True, {})
    dut_ok = ScenarioStep("inv_01", "读读数", True, {})
    dut_bad = ScenarioStep("inv_01", "读读数", False, {"breaches": ["grid_freq_hz=60.0 越过 55"]})
    grid_bad = ScenarioStep("grid_sim_01", "设定电网", False, {"freq_hz": 60.0})

    assert verdict_of([grid_ok, wait, dut_ok], PARTICIPANTS) == "passed"
    assert verdict_of([grid_ok, wait, dut_bad], PARTICIPANTS) == "failed"
    assert verdict_of([wait, grid_bad, dut_ok], PARTICIPANTS) == "invalid", "归属不受等待影响"
