"""并行抢占证明用的输入用例,只被 tests/parallel_bus_check.py 显式传给 pytest。

文件名不以 test_ 开头:默认套件不收集它 —— 证明"分组生效时一条总线上只有一个请求者"
不需要往默认套件里塞用例。

用的是 tests/conftest.py 里现成的两条缝:按设备类型展开的泛型 fixture(meter)+ 收集期的
xdist_group 分组。每条用例跑完,分组记录由 _record_parallel_assignment 写进临时 CSV。

只连自己这台设备和它借的那条总线(via: 链),绝不一次把整份清单全连上:
并行时"分组生效"的真正判据是"别的 worker 不会去开这条总线",
把所有设备一次连一遍正好把这条判据毁掉。
"""
import pytest

from testplatform.config.loader import load_device_config
from testplatform.config.manifest import parse_manifest


@pytest.fixture(scope="session")
def topology(request):
    """设备 id → 清单里的那一条,用来沿 via: 链找到这条总线的主站。"""
    manifest = parse_manifest(load_device_config(request.config.getoption("--device-config")))
    return {spec["id"]: spec for spec in manifest.devices}


def _connect_bus_chain(composition_root, topology, device_id):
    """下游设备的 connect() 要求它借的那条总线已经连着,所以从主站往下依次连。"""
    chain = []
    spec = topology[device_id]
    while spec is not None:
        chain.append(spec["id"])
        upstream = spec.get("via")
        spec = topology[upstream] if upstream is not None else None
    for device_id in reversed(chain):
        driver = composition_root.get(device_id)
        if driver.device.state.name != "CONNECTED":
            driver.connect()


def test_meter_connects(meter, composition_root, topology):
    _connect_bus_chain(composition_root, topology, meter.device.id)
    assert meter.device.state.name == "CONNECTED"


def test_meter_reads_its_first_reading_key(meter, composition_root, topology):
    _connect_bus_chain(composition_root, topology, meter.device.id)
    key = composition_root.reading_keys_of(meter.device.id)[0]
    meter.read(key)
    assert key in meter.device.history[-1].readings
