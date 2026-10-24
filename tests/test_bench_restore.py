"""
用例失败之后台架复位:周边设备回到清单声明的 initial_setpoint,结果落成证据。

一条用例把台架设到 0V 之后失败,如果不能复位,下一条用例读到的是错值 —— 它会被判成
"设备不合格",而真正的原因是上一条用例没退出来,归因四类里查不出来。
所以复位分三步落地,都在这个文件里验:

1. 清单:initial_setpoint 只许声明在 role: support 上(被测设备的收尾只有条款知道);
2. 装配:声明了却收不下它的驱动(没有 apply_initial_setpoint)装配期就报错 ——
   否则复位只是"静默没发生",后面所有读数都被污染;
3. 运行:每条用例结束后与整轮结束前都复位一次,恢复失败 = 测试无效(不是设备不合格),
   而且复位永远不往被测设备身上写。

真跑一轮 pytest 才证明得了的两件事(崩溃路径、下一条用例读到什么)交给嵌套 pytest,
用 tests/bench_restore_cases.py 与 tests_scenario/bench_restore_cases.py 两个用例文件。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests_support.nested_pytest import pytest_command
from testplatform.composition.root import (
    CompositionRoot,
    SupportEquipmentFault,
    SupportRestoreFault,
)
from testplatform.config.manifest import parse_manifest
from testplatform.domain.grid_simulator import GridSetpoint
from testplatform.reporting.evidence import SETPOINT_RESTORE_OPERATION, EvidenceSession

REPO = Path(__file__).resolve().parent.parent
FACTORY_MANIFEST = REPO / "configs" / "devices.yaml"
GRID, INVERTER = "grid_sim_01", "inv_01"
RESTORE_FRAME = "SET_GRID 230.0 50.0\n"


def _manifest():
    """真清单的一份副本:型号、点表、判据集、设备全部照旧,只在副本上改动要验的那一处。"""
    return yaml.safe_load(FACTORY_MANIFEST.read_text(encoding="utf-8"))


def _write(tmp_path, manifest) -> str:
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return str(path)


def _support_spec(manifest, device_id=GRID) -> dict:
    return next(spec for spec in manifest["devices"] if spec["id"] == device_id)


def _records(session, device_id) -> list:
    path = session.path_of(device_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sends(session, device_id) -> list:
    return [
        bytes.fromhex(record["hex"]).decode()
        for record in _records(session, device_id)
        if record["kind"] == "frame" and record["direction"] == "send"
    ]


# ========================= 清单:只有周边设备能声明初始设定 =========================

def test_the_manifest_refuses_an_initial_setpoint_on_a_dut():
    """被测设备的收尾只有条款知道(这条用例进没进工厂模式)。平台替它猜一个"初始设定"
    就是在整轮结束时往设备身上写它自己编的值 —— 那比留在工厂模式更难查。"""
    manifest = _manifest()
    _support_spec(manifest, INVERTER)["initial_setpoint"] = {"voltage_v": 230}

    with pytest.raises(ValueError) as excinfo:
        parse_manifest(manifest)

    message = str(excinfo.value)
    assert INVERTER in message and "initial_setpoint" in message and "support" in message


@pytest.mark.parametrize(
    "setpoint",
    [{}, {"voltage_v": "230"}, {"voltage_v": True}, {"": 230}, [230, 50]],
)
def test_the_manifest_refuses_a_malformed_initial_setpoint(setpoint):
    """设定项名 → 数值。写得不成样子在装配前报错:一个拼错的名字在 teardown 时
    只会变成"恢复失败",那时已经说不清是清单写错了还是台架不回话。"""
    manifest = _manifest()
    _support_spec(manifest)["initial_setpoint"] = setpoint

    with pytest.raises(ValueError) as excinfo:
        parse_manifest(manifest)

    assert "initial_setpoint" in str(excinfo.value)


def test_the_declared_initial_setpoint_lands_on_the_composition_root():
    root = CompositionRoot.build(str(FACTORY_MANIFEST))
    try:
        assert root.initial_setpoints == {GRID: {"voltage_v": 230, "freq_hz": 50}}
        assert set(root.initial_setpoints) <= set(root.ids_of_role("support")), (
            "复位只对周边设备做,被测设备不能出现在这张表里"
        )
    finally:
        root.teardown()


# ========================= 装配:收不下初始设定的驱动当场报错 =========================

def test_a_support_device_whose_driver_cannot_restore_is_refused_at_assembly(tmp_path):
    """声明了 initial_setpoint,驱动却没有 apply_initial_setpoint:装配期报错。

    留到 teardown 才发现的话,复位只是"没发生" —— 后面所有读数都被污染,报告里看不出来。
    """
    manifest = _manifest()
    spec = _support_spec(manifest)
    # 逆变器驱动没有"恢复初始设定"这个入口:把一台逆变器当周边设备,并给它声明初始设定。
    spec["model"] = "inverter_pv_series"
    spec["rated_power_w"] = 5000
    spec["connection"] = {"simulated_response": "3000.0 230.0 50.0"}

    with pytest.raises(ValueError) as excinfo:
        CompositionRoot.build(_write(tmp_path, manifest))

    message = str(excinfo.value)
    assert GRID in message and "initial_setpoint" in message and "apply_initial_setpoint" in message


# ========================= 运行:复位真的发帧,且不碰被测设备 =========================

def test_restore_support_sets_the_bench_back_and_never_writes_to_a_dut(tmp_path):
    """复位只对周边设备发帧。被测设备的收尾由用例自己声明,平台不在整轮结束时动它:
    整轮结束时设备被人动过,比留在工厂模式更难查。"""
    session = EvidenceSession(tmp_path / "evidence")
    root = CompositionRoot.build(_write(tmp_path, _manifest()), evidence=session)
    try:
        root.connect_support()
        inverter = root.get(INVERTER)
        inverter.connect()
        inverter.read_all()
        # 用例把台架设到 0V 之后失败 —— 这一轮结束时台架必须回到清单声明的设定。
        root.get(GRID).apply_grid(0.0, 50.0)
        dut_sends_before = _sends(session, INVERTER)

        root.restore_support()

        assert _sends(session, GRID)[-1] == RESTORE_FRAME
        assert root.get(GRID).device.setpoint == GridSetpoint(voltage_v=230.0, freq_hz=50.0)
        assert _sends(session, INVERTER) == dut_sends_before, "复位期间不许往被测设备写帧"
    finally:
        root.teardown()

    # 整轮结束(teardown)也不往被测设备写。
    assert _sends(session, INVERTER) == dut_sends_before


def test_a_bench_that_cannot_be_restored_is_a_test_invalid_not_a_device_failure(
    tmp_path, monkeypatch
):
    """恢复失败 = 这一轮没有有效结论:SupportRestoreFault 是周边设备故障的一种,
    测试层按同一个前缀转成 skip,不会落到"设备不合格"那一栏。"""
    class _SilentBench:
        """台架不回话:复位命令发得出去,答不回来。"""

        def connect(self):
            pass

        def disconnect(self):
            pass

        def has_client(self):
            return True

        def send(self, data):
            raise OSError("台架没回话")

        def receive(self, timeout=2.0, expected=None):
            raise OSError("台架没回话")

    monkeypatch.setattr(
        "testplatform.composition.root.build_transport",
        lambda access, mode, connection: _SilentBench(),
    )
    session = EvidenceSession(tmp_path / "evidence")
    root = CompositionRoot.build(_write(tmp_path, _manifest()), evidence=session)
    root.connect_support()
    with pytest.raises(SupportRestoreFault) as excinfo:
        root.restore_support()

    assert excinfo.value.device_id == GRID
    assert isinstance(excinfo.value, SupportEquipmentFault), "周边设备故障的一种,不是设备不合格"
    assert "恢复初始设定失败" in str(excinfo.value)
    assert "台架没回话" in str(excinfo.value), "原因不许吞掉"

    # 整轮结束时的复位同样不静默,但也不抛:pytest 里后一个 finalizer 抛异常会盖掉
    # 用例收尾那条"测试无效"的 skip,退出码还从 0 变成 1 —— 同一件事报两次,
    # 而最该被看见的那条被 ERROR 吞掉。所以它落进本轮状态,人看到的文案是"复位失败"。
    root.teardown()
    assert [entry["operation"] for entry in session.link_lost] == [SETPOINT_RESTORE_OPERATION]
    status = json.loads(session.write_round_status().read_text(encoding="utf-8"))
    assert status["verdict"] == "needs-review"
    assert "台架复位失败" in status["note"]
    assert "断链" not in status["note"], "复位失败不是断链:排障方向会被这句话带偏"


# ========================= 真跑一轮 pytest:崩溃路径与下一条用例 =========================

def _run(tmp_path: Path, *args: str, config: str | None = None) -> tuple[int, str]:
    """在临时目录里真跑一轮:证据落在临时目录(写进仓库的 evidence/ 会和主套件互相盖),
    rootdir 仍然是仓库(传的是仓库里的用例文件),所以 tests/ 与 tests_scenario/ 的 conftest
    照旧生效 —— 收尾这件事本身就长在那些 fixture 上。"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        pytest_command(
            *args, "--device-config", config or str(REPO / "configs" / "devices.yaml")
        ),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=180,
    )
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")


def _evidence_files(tmp_path: Path, pattern: str) -> list[Path]:
    return sorted((tmp_path / "evidence").glob(f"*/{pattern}"))


def test_a_case_that_leaves_the_bench_at_zero_does_not_pollute_the_next_one(tmp_path):
    """端到端:用例把台架设到 0V 之后抛异常 → 收尾把台架发回 230V/50Hz → 下一条用例的读数不受影响。

    断言的是真发出去的帧:模拟器不管收到什么都回 230V/50Hz,聚合根上的设定值看不出台架
    到底被命令成什么。
    """
    cases = f"{REPO / 'tests' / 'bench_restore_cases.py'}"
    returncode, output = _run(
        tmp_path,
        "-q",
        f"{cases}::test_the_case_drives_the_bench_to_zero_and_fails",
        f"{cases}::test_the_next_case_sees_a_restored_bench",
    )

    assert returncode != 0, output  # 第一条用例是按设计失败的
    assert "1 failed, 1 passed" in output, output
    assert "台架被留在 0V" in output, output


def test_a_bench_that_cannot_be_restored_makes_the_round_invalid(tmp_path):
    """恢复失败要落成"测试无效":skip 里带固定前缀,退出码不变,不是设备不合格。"""
    manifest = _manifest()
    # 台架只回一个数:恢复命令(带电压与频率两项)读不回来 = 恢复不了。
    _support_spec(manifest)["connection"]["simulated_response"] = "230.0"
    config = _write(tmp_path, manifest)

    cases = f"{REPO / 'tests' / 'bench_restore_cases.py'}"
    returncode, output = _run(
        tmp_path,
        "-q",
        "-rs",
        f"{cases}::test_the_round_is_invalid_when_the_bench_cannot_be_restored",
        config=config,
    )

    assert returncode == 0, f"测试无效不改退出码,整轮跑完:{output}"
    assert "1 passed, 1 skipped" in output, output
    assert "测试无效:" in output and GRID in output, output
    assert "恢复初始设定失败" in output, output
    # 人看见的文案必须说得出"台架没恢复初始设定",而不是"断链":两者排障方向相反。
    assert "本轮存在台架复位失败" in output, output
    assert "断链" not in output, output

    status = json.loads(_evidence_files(tmp_path, "round-status.json")[0].read_text("utf-8"))
    assert status["verdict"] == "needs-review"
    assert "台架复位失败" in status["note"] and "断链" not in status["note"], status


def test_a_cleanup_runs_on_the_exception_path_and_does_not_mask_the_original_error(tmp_path):
    """用例抛异常之后:注册的收尾照样执行,结果进 scenario-*.json 的 cleanup 字段;
    收尾自己抛异常时不顶掉原异常 —— 原异常是被测设备的问题,收尾失败是流程问题,两者都进证据。"""
    cases = f"{REPO / 'tests_scenario' / 'bench_restore_cases.py'}"
    returncode, output = _run(
        tmp_path,
        "-q",
        f"{cases}::test_the_case_fails_after_declaring_cleanups",
        f"{cases}::test_the_bench_is_back_after_the_failed_case",
    )

    # 失败理由是原异常,收尾自己的异常没有顶掉它,也没有变成 teardown 错误。
    assert "1 failed, 1 passed" in output, output
    assert "原异常:用例读到 0 kWh" in output, output
    assert "ERROR" not in output, output
    assert "这段文本只能出现在 cleanup 字段里" not in output, (
        f"收尾的异常不许顶掉原异常出现在报告里:{output}"
    )

    (path,) = _evidence_files(
        tmp_path, "scenario-test_the_case_fails_after_declaring_cleanups.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["verdict"] == "invalid", "崩在写出步骤之前:这一轮没有有效结论,不是设备不合格"
    assert record["cleanup"] == [
        {"action": "写回出厂值", "passed": True},
        {
            "action": "这条收尾自己抛异常",
            "passed": False,
            "error": "RuntimeError",
            "message": "收尾自己抛异常:这段文本只能出现在 cleanup 字段里",
        },
    ]
    assert record["cleanup_failed"] is True
    assert returncode != 0, output
