"""
四个脚手架:`scaffold driver|protocol|case|simulator`。

**生成的产物全部是 Python。** `npx` / `testplatform` 只是投递与运行的壳 —— 平台里没有自制的
描述语言,用例、驱动、编解码、模拟器都是 `.py`(ADR 0002)。

每个子命令都不是"生成几个文件就完事",而是**生成一个当场能跑的项目**:
    <out>/
        pyproject.toml          pytest 的收集范围与 import 路径(项目自己的)
        product.py              假产品:监听 ${PORT}(不依赖仓库里的 tests_support)
        configs/devices.yaml    一台模拟设备 + 一个判据集
        configs/clauses.yaml    这个项目自己的条款(脚手架样例挂一条)
        tasks/<name>.yaml       kind: smoke 的任务,launch 用 ${PYTHON} / ${PORT}
        tests/test_<kind>.py    该 kind 的骨架 + 一条真跑通的样板用例
        <kind> 专属的模块      driver 生成驱动文件 + conftest 导入它(注册要发生在装配前)

生成后立刻:
    testplatform-run-task <out>/tasks/<name>.yaml --root <out> --evidence-dir <out>/reports
必须 exit 0 —— 这条由 tests_platform/test_scaffolds.py 逐个验。

为什么用 smoke:样板验的是"这条生成的路是通的",不是某台设备的合格与否(设备级 0/0 在
acceptance 下会判未完成,那对新用户是误导)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping

KINDS = ("driver", "protocol", "case", "simulator")

# 生成文件头部都要有这句:用户看到的是 Python,不是某种配置 DSL
PYTHON_NOTE = "# 这个文件是 Python:平台没有自制的描述语言;`npx` / `testplatform` 只是投递与运行的壳。\n"

CASE_ID = "T_00_00_90_000001_01"
SAMPLE_ID = "T_00_00_90_000001_02"
CLAUSE_ID = "SCAFFOLD-1"

PRODUCT = '''\
"""假产品:监听平台分配的端口,什么都不做(脚手架项目用它把任务跑通)。"""
import argparse
import os
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
arguments = parser.parse_args()

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", arguments.port))
listener.listen(8)
print(f"scaffold product listening on {arguments.port}", flush=True)
while True:
    connection, _ = listener.accept()
    connection.close()
'''

PYPROJECT = '''\
[project]
name = "scaffold-project"
version = "0.1.0"
requires-python = ">=3.10"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-p no:randomly"
'''

CLAUSES = f"""\
# 这个项目自己的条款清单。条款 id 是受控集合:用例的 clause 只能写这里面的 id(或 free: 原文)。
clauses:
  {CLAUSE_ID}: {{standard: "脚手架", number: "", summary: "脚手架生成的样板用例"}}
"""

DEVICES = """\
# 一台模拟设备 + 一个判据集:脚手架项目用它验"这条生成的路是通的"。
criteria_profiles:
  scaffold_220v:
    - {quantity: __QUANTITY__, gt: 0}

models:
  scaffold_model:
    type: __MODEL_TYPE__
    protocol: vendor_ascii
    criteria: scaffold_220v
    capabilities: __CAPABILITIES__
    __DEVICE_PARAMS__point_table:
__POINTS__

devices:
  - id: dev_01
    model: scaffold_model
    access: tcp
    mode: simulator
    name: "脚手架样板设备"
    __DEVICE_PARAM_LINE__connection:
      simulated_response: "__RESPONSE__"
"""

TASK = """\
# 这份任务是脚手架生成的样板:kind: smoke(验通路,不验设备)。
#   testplatform-run-task {task_path} --root {root} --evidence-dir {evidence}
id: {task_id}
kind: smoke
product: scaffold-product
launch:
  - ${{PYTHON}}
  - product.py
  - --port
  - ${{PORT}}
devices: configs/devices.yaml
select: tag:样板
"""

SAMPLE_CASE = PYTHON_NOTE + '''\
"""样板用例:连上模拟设备、读一次、判据集给结论。改它,或者照它抄一条。"""

from testplatform.cases import case


@case(id="{case_id}", clause="{clause_id}", tags=["样板"])
def test_the_sample_device_reads_the_simulated_value(composition_root):
    device = composition_root.get("dev_01")
    device.connect()

    reading = device.read_all()

    assert reading.readings, "一次读数至少要有一个量"
    assert all(value > 0 for value in reading.readings.values()), reading.readings
    # 判据结论在领域读数上(聚合根记下的那一条),不在 RecordedReadings 上
    assert device.device.latest_reading.passed is True, device.device.latest_reading.evaluation
'''

DRIVER = PYTHON_NOTE + '''\
"""驱动骨架:注册 (设备类型, 规约) → 工厂,点表驱动的读路径已经在平台里。

要改的只有两处:
    * `READ_COMMAND`:你的设备用什么命令读数据
    * 型号点表(configs/devices.yaml)里的 address / length / encoding / scale

读回来的字节怎么变成物理量,由点表决定;这里不参与。
"""

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import register_device
from testplatform.drivers.vendor_ascii import vendor_ascii_reader
from testplatform.transport.interfaces import ByteStreamTransport

# 读一条数据的命令(按你的设备改;这里用一行文本协议)
READ_COMMAND = b"READ\\n"


@register_device("{device_type}", "vendor_ascii")
def build_scaffold_driver(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    """把清单里的一台设备装配成驱动:领域对象 + 点表 + 传输 + 读路径。"""
    device = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(
        device,
        model,
        transport,
        vendor_ascii_reader(transport, READ_COMMAND),
    )
'''

PROTOCOL = PYTHON_NOTE + '''\
"""编解码骨架 + 一条往返自检。

规矩:编解码是**纯函数**(字节进、字节出,不碰 socket、不碰设备状态),自检写在同文件里。
"""

from testplatform.cases import case

HEADER = b"\\xaa"


def build_request(address: int, value: int) -> bytes:
    """请求帧:header | address(2) | value(2) —— 按你的规约改。"""
    return HEADER + address.to_bytes(2, "big") + value.to_bytes(2, "big")


def parse_request(frame: bytes) -> tuple:
    """解析请求帧 → (address, value)。长度/校验不对就抛错,别返回半个值。"""
    if not frame.startswith(HEADER) or len(frame) != 5:
        raise ValueError(f"请求帧不合法: {frame.hex()}")
    return int.from_bytes(frame[1:3], "big"), int.from_bytes(frame[3:5], "big")


def build_response(address: int, value: int) -> bytes:
    return HEADER + address.to_bytes(2, "big") + value.to_bytes(2, "big")


def parse_response(frame: bytes, address: int) -> int:
    got, value = parse_request(frame)
    if got != address:
        raise ValueError(f"应答地址是 {got},不是 {address}")
    return value


def _self_check() -> None:
    """往返自检:拼出来再解回来必须一模一样。"""
    frame = build_request(7, 1234)
    assert parse_request(frame) == (7, 1234)
    assert parse_response(build_response(7, 4321), 7) == 4321


_self_check()


@case(id="{case_id}", clause="{clause_id}", tags=["样板"])
def test_round_trip():
    """样板用例:纯函数缝,不需要设备。"""
    _self_check()
'''

SIMULATOR = PYTHON_NOTE + '''\
"""模拟器骨架:点表可读写 + 故障注入钩子。

平台对模拟器的保真度上限就是这三件事(点表读写 / 故障注入 / 数据注入),
**不做设备业务行为仿真** —— 那会变成一套"假电表的行为规范",没人维护得起。
"""

from testplatform.cases import case

FAULTS = ("timeout", "slow", "link-lost")


class ScaffoldSimulator:
    """一个最小模拟器:内存里一张 {地址: 值} 表,读就回,写就改。"""

    supported_faults = FAULTS

    def __init__(self, registers=None, faults=None):
        self._registers = dict(registers or {0: 2300})
        self._faults = dict(faults or {})
        self._connected = False
        self._last = b""

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def has_client(self):
        return self._connected

    def send(self, data):
        self._last = data

    def receive(self, timeout=2.0, expected=None):
        if self._faults.get("link-lost"):
            raise ConnectionError("注入的故障:断链")
        if self._faults.get("timeout"):
            raise TimeoutError("注入的故障:不回答")
        if self._faults.get("slow"):
            import time

            time.sleep(float(self._faults["slow"]))
        address = int.from_bytes(self._last[:1], "big")
        return self._registers[address].to_bytes(2, "big")

    def inject_point(self, point, raw):
        """数据注入:直接用字节写进这张表,返回读回来的那份(调用方据此验往返)。"""
        self._registers[point.address] = int.from_bytes(raw, "big")
        return self._registers[point.address].to_bytes(2, "big")


@case(id="{case_id}", clause="{clause_id}", tags=["样板"])
def test_the_simulator_reads_and_injects():
    """样板用例:模拟器自己就能验(点表读、故障注入、数据注入)。"""
    simulator = ScaffoldSimulator({0: 2300})
    simulator.connect()

    simulator.send(b"\\x00")
    assert simulator.receive() == (2300).to_bytes(2, "big")

    class _Point:
        address = 0
        key = "电压"

    assert simulator.inject_point(_Point(), (2315).to_bytes(2, "big")) == (2315).to_bytes(2, "big")
    simulator.send(b"\\x00")
    assert simulator.receive() == (2315).to_bytes(2, "big")

    broken = ScaffoldSimulator(faults={"timeout": True})
    broken.connect()
    broken.send(b"\\x00")
    try:
        broken.receive()
    except TimeoutError:
        pass
    else:  # pragma: no cover - 守卫:声明了故障就必须真的故障
        raise AssertionError("声明了 timeout 却没有超时")
'''


def scaffold(kind: str, out: str | Path, *, name: str | None = None) -> Path:
    """生成一个能当场跑起来的项目,返回项目目录。"""
    if kind not in KINDS:
        raise ValueError(f"不认识的脚手架 {kind!r};可选: {list(KINDS)}")
    project = Path(out).expanduser().resolve()
    name = name or f"{kind}_sample"
    project.mkdir(parents=True, exist_ok=True)
    (project / "configs").mkdir(exist_ok=True)
    (project / "tasks").mkdir(exist_ok=True)
    (project / "tests").mkdir(exist_ok=True)

    driver_kind = kind == "driver"
    device_type = "scaffold_meter" if driver_kind else "inverter"
    (project / "product.py").write_text(PRODUCT, encoding="utf-8")
    (project / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (project / "configs" / "clauses.yaml").write_text(CLAUSES, encoding="utf-8")
    (project / "configs" / "devices.yaml").write_text(
        DEVICES.replace("__MODEL_TYPE__", device_type)
        .replace("__QUANTITY__", "电压" if driver_kind else "grid_voltage_v")
        .replace(
            "__CAPABILITIES__",
            "[电压]" if driver_kind else "[有功功率, 电压, 频率]",
        )
        .replace(
            "__POINTS__",
            "      电压: {address: 0, length: 7, encoding: ascii, unit: V}"
            if driver_kind
            else (
                "      有功功率: {address: 0, length: 8, encoding: ascii, unit: W}\n"
                "      电压: {address: 1, length: 7, encoding: ascii, unit: V}\n"
                "      频率: {address: 2, length: 7, encoding: ascii, unit: Hz}"
            ),
        )
        .replace("__RESPONSE__", "230.0" if driver_kind else "3000.0 230.0 50.0")
        .replace(
            "__DEVICE_PARAMS__",
            "" if driver_kind else "device_params: [rated_power_w]\n    ",
        )
        .replace(
            "__DEVICE_PARAM_LINE__",
            "" if driver_kind else "rated_power_w: 1000\n    ",
        ),
        encoding="utf-8",
    )
    (project / "tasks" / f"{name}.yaml").write_text(
        TASK.format(
            task_id=name,
            task_path=f"tasks/{name}.yaml",
            root=str(project),
            evidence=str(project / "reports"),
            case_id=CASE_ID,
        ),
        encoding="utf-8",
    )

    sample = SAMPLE_CASE.format(case_id=CASE_ID, clause_id=CLAUSE_ID)
    device_sample = SAMPLE_CASE.format(case_id=SAMPLE_ID, clause_id=CLAUSE_ID)
    if kind == "driver":
        (project / "scaffold_meter_driver.py").write_text(
            DRIVER.format(device_type=device_type), encoding="utf-8"
        )
        (project / "conftest.py").write_text(
            "# 让项目里的驱动在装配前注册:导入即注册(平台的注册表是模块级的)\n"
            "import scaffold_meter_driver  # noqa: F401\n",
            encoding="utf-8",
        )
        body = sample
    elif kind == "protocol":
        # 纯函数自检 + 一条真读设备的样板:只跑纯函数的话这一轮没有任何证据,
        # 报告会判"未完成"(没有 run-scope / round-status)—— 脚手架不能生成这种"跑起来就红"的东西
        body = (
            PROTOCOL.replace("{case_id}", CASE_ID).replace("{clause_id}", CLAUSE_ID)
            + "\n\n"
            + device_sample
        )
    elif kind == "simulator":
        body = (
            SIMULATOR.replace("{case_id}", CASE_ID).replace("{clause_id}", CLAUSE_ID)
            + "\n\n"
            + device_sample
        )
    else:  # case
        body = sample
    (project / "tests" / f"test_{kind}.py").write_text(body, encoding="utf-8")
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="testplatform scaffold",
        description="生成一个当场能跑的 Python 项目:驱动 / 编解码 / 用例 / 模拟器骨架",
    )
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("--out", default=None, help="生成到哪个目录(缺省 ./<kind>_project)")
    parser.add_argument("--name", default=None, help="任务与项目的名字(缺省 <kind>_sample)")
    arguments = parser.parse_args(argv)

    project = scaffold(
        arguments.kind,
        arguments.out or f"{arguments.kind}_project",
        name=arguments.name,
    )
    name = arguments.name or f"{arguments.kind}_sample"
    print(f"已生成 {project}")
    print(f"  profile: {project / 'configs' / 'devices.yaml'}")
    print(f"  跑它:  testplatform-run-task {project / 'tasks' / (name + '.yaml')} --root {project}"
          f" --evidence-dir {project / 'reports'}")
    return 0
