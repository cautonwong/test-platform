"""
台架复位的一组用例 —— 只在嵌套 pytest 里被点名跑(文件名不是 test_*),因为其中一条必须真的失败。

"一条用例把台架设到 0V 之后失败,下一条用例读到的是什么"只有在真跑一轮 pytest 里才证明得了:
收尾的挂点是用例的 fixture 收尾,tests/conftest.py 那几条 autouse 就是被测的东西本身。
另一条用例会在"台架恢复不了"的清单下被点名跑(见 tests/test_bench_restore.py),
证明恢复失败落成"测试无效",不是设备不合格。
"""
import json

from testplatform.domain.grid_simulator import GridSetpoint

GRID = "grid_sim_01"
INITIAL = GridSetpoint(voltage_v=230.0, freq_hz=50.0)


def test_the_case_drives_the_bench_to_zero_and_fails(composition_root):
    """用例自己失败:台架被留在 0V。收尾不跑,下一条用例读到的就是错值。"""
    grid = composition_root.get(GRID)
    grid.connect()
    grid.apply_grid(0.0, 50.0)
    raise AssertionError("用例中途失败,台架被留在 0V")


def test_the_next_case_sees_a_restored_bench(composition_root):
    """上一条用例失败之后,收尾把台架发回了 230V/50Hz:这一条用例的读数才可信。

    看真的发出去的帧,不看聚合根里的设定值:模拟器不管收到什么都回 "230.0 50.0",
    聚合根上的值因此看不出台架到底被命令成什么。
    """
    assert _grid_sends(composition_root) == ["SET_GRID 0.0 50.0\n", "SET_GRID 230.0 50.0\n"]
    assert composition_root.get(GRID).device.setpoint == INITIAL


def test_the_round_is_invalid_when_the_bench_cannot_be_restored(composition_root):
    """这条用例什么也没做错:结论不成立是因为台架恢复不了。

    收尾阶段的 skip 是"这一轮测试无效",不是"这台设备不合格" —— 退出码不变,整轮跑完。
    """
    assert composition_root.get(GRID) is not None


def _grid_sends(composition_root) -> list[str]:
    path = composition_root.evidence.path_of(GRID)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        bytes.fromhex(record["hex"]).decode()
        for record in records
        if record["kind"] == "frame" and record["direction"] == "send"
    ]
