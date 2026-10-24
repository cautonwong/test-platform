"""
断链的归属:收帧阶段遇到 ConnectionError(对端关闭)时,结论按设备清单里的 role 分流。

- 周边设备(role: support)断链 → 这一条用例没有有效结论:转成 skip("测试无效: …"),
  整轮照常跑完(退出码不变),同时留下三处可见记录:证据里的 link_lost、
  终端汇总里的小节、会话目录下的 round-status.json。
- 被测设备(role: dut)断链 → 不变。链路就是它自身的一部分,异常原样上抛,仍按设备不合格处理。

两件事都不许混:归属只看 role,不看"哪个异常类型看起来像"。
上半部分直接装配组合根测记录内容,下半部分真的跑一遍 pytest 证明结论与汇总输出。
"""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests_support.manifest_factory import GRID_50HZ, GRID_SIMULATOR_MODEL, INVERTER_MODEL, build
from tests_support.nested_pytest import pytest_command
from testplatform.composition.root import CompositionRoot, SupportLinkLost
from testplatform.reporting.evidence import EvidenceSession

REPO = Path(__file__).resolve().parent.parent

SUPPORT = {
    "id": "grid_sim_01",
    "model": "grid_simulator_3ph",
    "role": "support",
    "access": "tcp",
}

DUT = {
    "id": "inv_01",
    "model": "inverter_pv_series",
    "access": "tcp",
    "rated_power_w": 5000,
}


class _ClosedOnReceive:
    """收帧阶段断链的假传输:对端把连接关了(TCP 传输此时抛 ConnectionError)。"""

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def has_client(self) -> bool:
        return True

    def send(self, data: bytes) -> None:
        pass

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        raise ConnectionError("10.0.0.8:502 对端已关闭连接")


def _manifest(*devices: dict) -> dict:
    """型号来自工厂,devices 由各用例自己点名 —— 它们的 mode / connection 就是被测的那一处。"""
    return {
        "criteria_profiles": {"grid_50hz": GRID_50HZ},
        "models": {
            "inverter_pv_series": copy.deepcopy(INVERTER_MODEL),
            "grid_simulator_3ph": copy.deepcopy(GRID_SIMULATOR_MODEL),
        },
        "devices": [copy.deepcopy(device) for device in devices],
    }


def _build(tmp_path, manifest, evidence):
    return build(tmp_path, manifest, evidence=evidence)


def _records(session, device_id):
    path = session.path_of(device_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture()
def session(tmp_path):
    return EvidenceSession(tmp_path / "evidence")


@pytest.fixture()
def closed_link(monkeypatch):
    monkeypatch.setattr(
        "testplatform.composition.root.build_transport",
        lambda access, mode, connection: _ClosedOnReceive(),
    )


# ========================= 记录:证据里有,round-status 里有 =========================

def test_a_support_link_lost_on_receiving_is_a_test_invalid_not_a_device_failure(
    tmp_path, session, closed_link
):
    """周边设备收帧断链:抛 SupportLinkLost(测试无效),不是让用例按设备不合格收场;
    证据里留一条 link_lost —— 设备 id、时间、失败种类、当时在干什么都在,
    且单独一个 kind,不混进 reading。"""
    support = {**SUPPORT, "mode": "real", "connection": {"host": "10.0.0.8", "port": 502}}
    dut = {**DUT, "mode": "simulator", "connection": {"simulated_response": "3000.0 230.0 50.0"}}
    root = _build(tmp_path, _manifest(support, dut), session)
    try:
        root.connect_support()
        with pytest.raises(SupportLinkLost) as excinfo:
            root.get("grid_sim_01").apply_grid(230.0, 50.0)
    finally:
        root.teardown()

    assert excinfo.value.device_id == "grid_sim_01"
    assert "测试无效" not in str(excinfo.value)  # 前缀由测试层那一处转换加上,组合根不造文案

    (entry,) = session.link_lost
    assert entry["device"] == "grid_sim_01"
    assert entry["operation"] == "receive"
    assert entry["failure"] == "closed"
    assert entry["at"], "断链记录必须带时间,否则事后无从对齐现场操作"

    (record,) = [r for r in _records(session, "grid_sim_01") if r["kind"] == "link_lost"]
    assert record["device"] == "grid_sim_01"
    assert (record["operation"], record["failure"]) == ("receive", "closed")
    assert not [r for r in _records(session, "grid_sim_01") if r["kind"] == "reading"]


def test_a_dut_link_lost_is_left_alone(tmp_path, session, closed_link):
    """被测设备断链不变:链路是它自身的一部分,异常原样上抛(不是 SupportLinkLost),
    证据里只有一条 closed 帧记录,本轮状态仍是干净的 —— 断链标记只针对周边设备。"""
    support = {**SUPPORT, "mode": "simulator", "connection": {"simulated_response": "230.0"}}
    dut = {**DUT, "mode": "real", "connection": {"host": "10.0.0.5", "port": 502}}
    root = _build(tmp_path, _manifest(support, dut), session)
    try:
        root.get("inv_01").connect()
        with pytest.raises(ConnectionError) as excinfo:
            root.get("inv_01").read("有功功率")
    finally:
        root.teardown()

    assert not isinstance(excinfo.value, SupportLinkLost)
    assert session.link_lost == []
    kinds = [record["kind"] for record in _records(session, "inv_01")]
    assert "link_lost" not in kinds
    assert [r["error"] for r in _records(session, "inv_01") if "error" in r] == ["closed"]


def test_round_status_carries_the_mark_and_stays_clean_when_nothing_broke(tmp_path, closed_link):
    """round-status.json 是给 CI / 报告层用的:有断链就写明次数、设备与结论提示;
    没有断链时文件照样写、link_lost 为空 —— "文件不存在"和"没跑完"不能混为一谈。"""
    clean = EvidenceSession(tmp_path / "clean")
    clean.write_round_status()
    assert json.loads((clean.directory / "round-status.json").read_text(encoding="utf-8")) == {
        "verdict": "clean",
        "note": "本轮没有断链",
        "link_lost": [],
        # 票 13a2:事件没送到 sink 也记在这份状态里(没有就是空列表)
        "event_emit_failed": [],
    }

    support = {**SUPPORT, "mode": "real", "connection": {"host": "10.0.0.8", "port": 502}}
    broken = EvidenceSession(tmp_path / "broken")
    root = _build(tmp_path, _manifest(support), broken)
    try:
        root.connect_support()
        with pytest.raises(SupportLinkLost):
            root.get("grid_sim_01").apply_grid(230.0, 50.0)
    finally:
        root.teardown()
        broken.write_round_status()

    status = json.loads((broken.directory / "round-status.json").read_text(encoding="utf-8"))
    assert status["verdict"] == "needs-review"
    assert "需复核" in status["note"]
    assert [entry["device"] for entry in status["link_lost"]] == ["grid_sim_01"]
    assert status["link_lost"][0]["failure"] == "closed"


# ========================= 真的跑一遍 pytest:结论 + 汇总输出 =========================

# 子进程里跑的 conftest:与仓库同一个插件(结论转换与汇总都在插件里),
# 再把指定 host 的传输换成"对端已关闭",其余设备照常走模拟器。
SUITE_CONFTEST = '''
import os

from testplatform.composition import root as composition

pytest_plugins = ["testplatform.plugin"]

_original_build_transport = composition.build_transport


class _ClosedOnReceive:
    def connect(self):
        pass

    def disconnect(self):
        pass

    def has_client(self):
        return True

    def send(self, data):
        pass

    def receive(self, timeout=2.0, expected=None):
        raise ConnectionError("对端已关闭连接")


def pytest_configure(config):
    closed_host = os.environ["CLOSED_HOST"]

    def _build(access, mode, connection):
        if connection.get("host") == closed_host:
            return _ClosedOnReceive()
        return _original_build_transport(access, mode, connection)

    composition.build_transport = _build
'''

SUITE_CASES = '''
def test_reads_through_the_grid_simulator(composition_root):
    composition_root.get("grid_sim_01").apply_grid(230.0, 50.0)


def test_the_round_keeps_going(composition_root):
    inverter = composition_root.get("inv_01")
    inverter.connect()
    assert inverter.read("有功功率") == 3000.0
'''

DUT_BREAK_CASES = '''
def test_the_device_that_owes_the_readings(composition_root):
    inverter = composition_root.get("inv_01")
    inverter.connect()
    inverter.read("有功功率")
'''


def _suite(tmp_path, devices, cases: str) -> None:
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(_manifest(*devices), allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_round.py").write_text(cases, encoding="utf-8")


def _run_suite(cwd: Path, closed_host: str):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
        "CLOSED_HOST": closed_host,
    }
    result = subprocess.run(
        pytest_command("-q", "-rs", "--device-config", "devices.yaml", "test_round.py"),
        cwd=cwd,
        capture_output=True,
        env=env,
        timeout=120,
    )
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")


def _round_status(tmp_path: Path) -> dict:
    (path,) = (tmp_path / "evidence").glob("*/round-status.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _kinds_of(tmp_path: Path, device_id: str) -> list:
    (path,) = (tmp_path / "evidence").glob(f"*/{device_id}.jsonl")
    return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_round_with_a_support_link_lost_keeps_going_and_says_so(tmp_path):
    """C 的核心:周边设备断链不中止整轮、不改退出码,但那一条用例是 skip(测试无效),
    并且终端汇总与 round-status.json 里都看得见。"""
    _suite(
        tmp_path,
        [
            {**SUPPORT, "mode": "real", "connection": {"host": "10.0.0.8", "port": 502}},
            {**DUT, "mode": "simulator", "connection": {"simulated_response": "3000.0 230.0 50.0"}},
        ],
        SUITE_CASES,
    )
    returncode, output = _run_suite(tmp_path, closed_host="10.0.0.8")

    assert returncode == 0, output  # 断链不改退出码:整轮跑完
    assert "1 passed" in output and "1 skipped" in output, output
    assert "测试无效:" in output and "grid_sim_01" in output, output
    # 汇总小节在,且点得出设备与次数
    assert "本轮存在断链" in output and "共 1 次" in output, output

    status = _round_status(tmp_path)
    assert status["verdict"] == "needs-review"
    assert [entry["device"] for entry in status["link_lost"]] == ["grid_sim_01"]
    assert "link_lost" in _kinds_of(tmp_path, "grid_sim_01")


def test_a_round_with_a_dut_link_lost_still_fails_the_device(tmp_path):
    """被测设备断链不改判:它仍然是被测设备不合格(exit code 非零),
    也没有"本轮存在断链"的台架提示 —— 两种归属不能混。"""
    _suite(
        tmp_path,
        [
            {**SUPPORT, "mode": "simulator", "connection": {"simulated_response": "230.0"}},
            {**DUT, "mode": "real", "connection": {"host": "10.0.0.5", "port": 502}},
        ],
        DUT_BREAK_CASES,
    )
    returncode, output = _run_suite(tmp_path, closed_host="10.0.0.5")

    assert returncode != 0, output
    assert "1 failed" in output and "ConnectionError" in output, output
    assert "本轮存在断链" not in output, output
    assert _round_status(tmp_path) == {
        "verdict": "clean",
        "note": "本轮没有断链",
        "link_lost": [],
        # 票 13a2:事件没送到 sink 也记在这份状态里(没有就是空列表)
        "event_emit_failed": [],
    }
