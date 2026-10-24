"""并行分组证明用的输入用例,只被 tests/parallel_topology_check.py 显式传给 pytest。

文件名不以 test_ 开头:默认套件不收集它,所以"同一 via 下的设备落在同一个 worker"
这条证明不需要往默认套件里塞用例。

它用的是 tests/conftest.py 里现成的两条缝,不新增第三条:
按设备类型展开的泛型 fixture(terminal / meter)+ 收集期的 xdist_group 分组。
每条用例跑完,分组记录由 _record_parallel_assignment 写进临时 CSV,断言在脚本那边。
"""
import pytest


@pytest.fixture(autouse=True)
def _connect_everything_in_topology_order(composition_root):
    """下游设备的 connect() 要求它借的那条总线(终端)已经连着。

    drivers 是按拓扑序装配的(终端先、下游后),所以顺序连一遍就位,
    用例里不用自己保证先后。
    """
    for driver in composition_root.all_drivers():
        if driver.device.state.name != "CONNECTED":
            driver.connect()


def test_terminal_connects(terminal):
    assert terminal.device.state.name == "CONNECTED"


def test_terminal_reads_its_first_reading_key(terminal, composition_root):
    key = composition_root.reading_keys_of(terminal.device.id)[0]
    terminal.read(key)
    assert key in terminal.device.history[-1].readings


def test_meter_connects(meter):
    assert meter.device.state.name == "CONNECTED"


def test_meter_reads_its_first_reading_key(meter, composition_root):
    key = composition_root.reading_keys_of(meter.device.id)[0]
    meter.read(key)
    assert key in meter.device.history[-1].readings
