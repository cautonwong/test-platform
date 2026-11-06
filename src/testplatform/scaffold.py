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
import re
from pathlib import Path

import yaml

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

# 驱动项目多一条 entry point:装成包以后,平台的插件加载器能发现它,
# 不依赖 conftest 的 import。源码直跑时 conftest 仍然是注册入口。
PYPROJECT_DRIVER = '''\
[project]
name = "scaffold-project"
version = "0.1.0"
requires-python = ">=3.10"

[project.entry-points."testplatform.drivers"]
scaffold_meter = "scaffold_meter_driver"

[tool.setuptools]
py-modules = ["scaffold_meter_driver"]

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
#   testplatform run {task_path} --root {root} --evidence-dir {evidence}
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
    # 电表、逆变器、电池的 read_all() 都有 passed / breaches:
    # 判据结论就在这次读数上,不用再去 device.latest_reading 取一次。
    assert reading.passed is True, reading.breaches
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


NEW_CASE = PYTHON_NOTE + '''\
"""一条新用例:连上清单里的设备,读一次。"""

from testplatform.cases import case


@case(id={case_id_literal}, clause={clause_literal}, tags=["样板"])
def test_{function}(composition_root):
    device = composition_root.get_ready({device_literal})
    device.connect()

    reading = device.read_all()

    assert reading.readings, "一次读数至少要有一个量"
    # 有判据的设备(电表 / 逆变器 / 电池)再加一行:
    # assert reading.passed, reading.breaches
'''

NEW_DRIVER_VENDOR_ASCII = PYTHON_NOTE + '''\
"""驱动骨架:注册 (设备类型, 规约) → 工厂。点表驱动的读路径已经在平台里。

要改的只有两处:
    * READ_COMMAND:你的设备用什么命令读数据
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

READ_COMMAND = b"READ\\n"


@register_device("{device_type}", "vendor_ascii")
def build_{function}_driver(
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

NEW_DRIVER_GENERIC = PYTHON_NOTE + '''\
"""驱动骨架:注册 ({device_type}, {protocol}) → 工厂。点表驱动的读路径已经在平台里。

要改的只有一处:read_raw 里发请求、收应答、返回这个点表项的原始字节。
读回来的字节怎么变成物理量,由点表决定;这里不参与。
"""

from testplatform.config.manifest import Model, Point
{protocol_import}from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import register_device
from testplatform.transport.interfaces import ByteStreamTransport

{protocol_registration}

def read_raw(transport: ByteStreamTransport):
    def _read(point: Point) -> bytes:
        raise NotImplementedError(f"还没有实现 {{point.key}} 的读法")

    return _read


@register_device("{device_type}", "{protocol}")
def build_{function}_driver(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    """把清单里的一台设备装配成驱动:领域对象 + 点表 + 传输 + 读路径。"""
    device = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(device, model, transport, read_raw(transport))
'''

NEW_PROTOCOL = PYTHON_NOTE + '''\
"""编解码骨架:纯函数,字节进、字节出;自检在文件末尾。

    python 本文件.py

把 HEADER / 长度 / 校验换成你的规约;解析失败就抛错,别返回半个值。
"""

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


if __name__ == "__main__":
    _self_check()
    print("protocol self-check passed")
'''


NEW_SIMULATOR = PYTHON_NOTE + '''\
"""模拟器骨架:注册 ({access}, "simulator") → 传输。

按你的规约改 respond:request 是最近一次 send 的字节,返回应答字节。
"""

from testplatform.transport.interfaces import (
    ByteStreamTransport,
    FrameLength,
    IncompleteFrameError,
    expected_bytes,
)
from testplatform.transport.registry import register_transport


class SimulatedProtocolTransport(ByteStreamTransport):
    supported_faults = ("timeout", "link-lost", "slow")

    def __init__(self, cfg: dict) -> None:
        self._cfg = dict(cfg)
        self._faults = dict(cfg.get("simulated_faults") or {{}})
        self._connected = False
        self._last = b""

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def has_client(self) -> bool:
        return self._connected

    def send(self, data: bytes) -> None:
        self._last = data

    def respond(self, request: bytes) -> bytes:
        raise NotImplementedError("按你的规约生成应答")

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        if self._faults.get("link-lost"):
            raise ConnectionError("注入的故障 link-lost:模拟器按清单设定断链")
        if self._faults.get("timeout"):
            raise TimeoutError("注入的故障 timeout:模拟器按清单设定不回答")
        delay = self._faults.get("slow")
        if delay:
            import time

            time.sleep(float(delay))
        answer = self.respond(self._last)
        total = expected_bytes(expected, answer)
        if total is not None and len(answer) < total:
            raise IncompleteFrameError("模拟器应答不够一帧")
        return answer


@register_transport("{access}", "simulator")
def build_{function}_simulator(cfg: dict) -> ByteStreamTransport:
    return SimulatedProtocolTransport(cfg)
'''


def _project_root(out: str | Path) -> Path:
    """已有项目必须带设备清单;不是项目时给出下一步,而不是写到一半。"""
    project = Path(out).expanduser().resolve()
    if not (project / "configs" / "devices.yaml").is_file():
        raise ValueError(
            f"{project} 不像一个项目:缺 configs/devices.yaml;"
            "先跑 testplatform scaffold case 建项目"
        )
    return project


def _module_name(name: str) -> str:
    module = re.sub(r"\W+", "_", name).strip("_")
    if not module:
        raise ValueError(f"名字 {name!r} 生成不出 Python 模块名")
    return module


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_name(value: str, field: str) -> str:
    """生成到 Python 源码里的名字只收标识符字符;引号 / 换行在生成前就拒掉。"""
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError(
            f"{field} 只能包含字母、数字、下划线、点和连字符,拿到 {value!r}"
        )
    return value


def _source_root(project: Path) -> Path:
    """setuptools 的包根;源码布局(where = ["src"])下驱动要写进 src。"""
    pyproject = project / "pyproject.toml"
    if not pyproject.is_file():
        return project
    text = pyproject.read_text(encoding="utf-8")
    section = re.search(
        r"(?ms)^\[tool\.setuptools\.packages\.find\]\s*(.*?)(?=^\[|\Z)", text
    )
    where = (
        re.search(r"where\s*=\s*\[(.*?)\]", section.group(1), re.S)
        if section
        else None
    )
    if where:
        first = where.group(1).split(",")[0].strip().strip("\"'")
        candidate = project / first
        if first and candidate.is_dir():
            return candidate
    package_dir = re.search(
        r'package-dir\s*=\s*\{[^}]*""\s*=\s*"([^"]+)"', text, re.S
    )
    if package_dir and (project / package_dir.group(1)).is_dir():
        return project / package_dir.group(1)
    return project


def _ensure_conftest_import(
    project: Path,
    module: str,
    *,
    suffix: str = "_driver",
    import_root: Path | None = None,
) -> None:
    """驱动/模拟器必须在装配前被 import,装饰器才会执行;把这一行写进项目的 conftest。"""
    conftest = project / "conftest.py"
    line = f"import {module}{suffix}  # noqa: F401"
    text = conftest.read_text(encoding="utf-8") if conftest.is_file() else ""
    additions: list[str] = []
    if import_root is not None and import_root != project:
        relative = import_root.relative_to(project).as_posix()
        path_line = f'sys.path.insert(0, str(Path(__file__).parent / "{relative}"))'
        if path_line not in text:
            additions.extend(["import sys", "from pathlib import Path", path_line])
    if f"import {module}{suffix}" not in text:
        additions.append(line)
    if not additions:
        return
    if text:
        conftest.write_text(
            text.rstrip() + "\n\n" + "\n".join(additions) + "\n", encoding="utf-8"
        )
    else:
        conftest.write_text(
            "# 让项目里的驱动在装配前注册:导入即注册(平台的注册表是模块级的)\n"
            + "\n".join(additions)
            + "\n",
            encoding="utf-8",
        )


def _ensure_py_module(text: str, module: str) -> str:
    """把根模块加进 setuptools 的 py-modules;已有列表就地追加。"""
    marker = "[tool.setuptools]"
    entry = f'"{module}"'
    if marker not in text:
        insert_at = text.find("[tool.setuptools.packages")
        block = f"{marker}\npy-modules = [{entry}]\n\n"
        if insert_at == -1:
            return text.rstrip() + "\n\n" + block.rstrip() + "\n"
        # 父表要出现在子表之前,TOML 读取器才不会把父表当成重复定义。
        return text[:insert_at] + block + text[insert_at:]

    start = text.index(marker) + len(marker)
    next_section = re.search(r"(?m)^\[", text[start:])
    end = start + next_section.start() if next_section else len(text)
    section = text[start:end]
    match = re.search(r"(?m)^py-modules\s*=\s*\[(.*?)\]", section, re.S)
    if match:
        modules = [
            item.strip().strip("\"'")
            for item in match.group(1).split(",")
            if item.strip()
        ]
        if module not in modules:
            modules.append(module)
        replacement = "py-modules = [" + ", ".join(f'"{item}"' for item in modules) + "]"
        return text[:start] + section[: match.start()] + replacement + section[match.end() :] + text[end:]
    insert_at = start + 1 if start < len(text) and text[start] == "\n" else start
    return text[:insert_at] + f"py-modules = [{entry}]\n" + text[insert_at:]


def _ensure_entry_point(
    pyproject: Path,
    module: str,
    *,
    group: str = "testplatform.drivers",
    target: str | None = None,
) -> bool:
    """给可安装项目补 entry point;没有 pyproject 就跳过(源码直跑仍然靠 conftest)。"""
    if not pyproject.is_file():
        return False
    target = target or f"{module}_driver"
    marker = f'[project.entry-points."{group}"]'
    entry = f'{module} = "{target}"'
    text = pyproject.read_text(encoding="utf-8")
    backend = re.search(r'build-backend\s*=\s*"([^"]+)"', text)
    if backend is not None and "setuptools" not in backend.group(1):
        # 非 setuptools 后端不知道 py-modules;写 entry point 只会指向 wheel 里不存在的模块。
        # 源码直跑仍由 conftest 导入,这里只拒绝安装形态的假承诺。
        return False
    if entry not in text:
        if marker in text:
            lines = text.splitlines()
            lines.insert(lines.index(marker) + 1, entry)
            text = "\n".join(lines) + "\n"
        else:
            text = text.rstrip() + f"\n\n{marker}\n{entry}\n"

    text = _ensure_py_module(text, target)
    pyproject.write_text(text, encoding="utf-8")
    return True


def _known_protocol_access(protocol: str) -> frozenset | None:
    from testplatform.config.manifest import PROTOCOL_ACCESS
    from testplatform.config.protocols import access_of

    return access_of(protocol) or PROTOCOL_ACCESS.get(protocol)


def new_driver(
    name: str,
    out: str | Path,
    *,
    device_type: str | None = None,
    protocol: str = "vendor_ascii",
    access: str = "tcp",
) -> Path:
    """往已有项目加一个驱动文件、conftest 导入与 entry point。

    vendor_ascii 生成即可跑;其它规约生成 read_raw 骨架,填完才能读。
    已注册的规约沿用平台声明的接入方式,生成器不再重复声明。
    """
    project = _project_root(out)
    module = _module_name(name)
    device_type = _safe_name(device_type or module, "device_type")
    if not protocol:
        raise ValueError("protocol 不能为空")
    protocol = _safe_name(protocol, "protocol")
    access_values = [
        _safe_name(value.strip(), "access")
        for value in access.split(",")
        if value.strip()
    ]
    if not access_values:
        raise ValueError("access 不能为空")
    known_access = _known_protocol_access(protocol)
    if known_access is not None:
        extra = set(access_values) - set(known_access)
        if extra:
            raise ValueError(
                f"协议 {protocol!r} 已注册,允许的接入方式: {sorted(known_access)};"
                f"不能新增 {sorted(extra)}"
            )
    access_set = "{" + ", ".join(f'"{value}"' for value in access_values) + "}"
    source_root = _source_root(project)
    path = source_root / f"{module}_driver.py"
    if path.exists():
        raise FileExistsError(f"{path} 已存在;换一个名字,或先删掉它")
    if protocol == "vendor_ascii":
        template = NEW_DRIVER_VENDOR_ASCII
        protocol_import = ""
        protocol_registration = ""
    else:
        template = NEW_DRIVER_GENERIC
        if known_access is None:
            protocol_import = (
                "from testplatform.config.protocols import register_protocol\n"
            )
            protocol_registration = (
                f'register_protocol("{protocol}", access={access_set})\n'
            )
        else:
            protocol_import = ""
            protocol_registration = ""
    path.write_text(
        template.format(
            device_type=device_type,
            protocol=protocol,
            function=module,
            access=access_set,
            protocol_import=protocol_import,
            protocol_registration=protocol_registration,
        ),
        encoding="utf-8",
    )
    _ensure_conftest_import(project, module, import_root=source_root)
    _ensure_entry_point(project / "pyproject.toml", module)
    return path


def new_simulator(name: str, out: str | Path, *, access: str | None = None) -> Path:
    """往已有项目加一个模拟器传输;access 缺省 <module>_sim,避免覆盖内置 tcp/serial。"""
    project = _project_root(out)
    module = _module_name(name)
    access = (access or f"{module}_sim").strip()
    if not access:
        raise ValueError("access 不能为空")
    access = _safe_name(access, "access")
    source_root = _source_root(project)
    path = source_root / f"{module}_simulator.py"
    if path.exists():
        raise FileExistsError(f"{path} 已存在;换一个名字,或先删掉它")
    path.write_text(
        NEW_SIMULATOR.format(access=access, function=module), encoding="utf-8"
    )
    _ensure_conftest_import(
        project, module, suffix="_simulator", import_root=source_root
    )
    _ensure_entry_point(
        project / "pyproject.toml",
        module,
        group="testplatform.transports",
        target=f"{module}_simulator",
    )
    return path


def new_protocol(name: str, out: str | Path) -> Path:
    """往已有项目加一个编解码模块;自检用 python <文件> 跑。"""
    project = _project_root(out)
    module = _module_name(name)
    path = _source_root(project) / f"{module}_protocol.py"
    if path.exists():
        raise FileExistsError(f"{path} 已存在;换一个名字,或先删掉它")
    path.write_text(NEW_PROTOCOL, encoding="utf-8")
    return path


def _known_device_ids(project: Path) -> list[str]:
    """项目 configs/ 下所有清单里的设备 id;读不了的 YAML 不参与本次校验。"""
    ids: list[str] = []
    for path in sorted((project / "configs").glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, list):
            continue
        for spec in devices:
            device_id = spec.get("id") if isinstance(spec, dict) else None
            if isinstance(device_id, str) and device_id not in ids:
                ids.append(device_id)
    return ids


def new_case(
    name: str,
    out: str | Path,
    *,
    device: str = "dev_01",
    clause: str = "SCAFFOLD-1",
    case_id: str | None = None,
) -> Path:
    """往已有项目加一条用例。

    条款写项目 clauses.yaml 里的 id,或 ``free: <原文>``。没有条款清单的项目
    (例如 EdgeC 的 acceptance 产品项目)仍可以用 ``free:`` 生成用例。
    """
    project = Path(out).expanduser().resolve()
    devices_path = project / "configs" / "devices.yaml"
    if not devices_path.is_file():
        raise ValueError(
            f"{project} 不像一个项目:缺 configs/devices.yaml;先跑 testplatform scaffold case"
        )
    known_devices = _known_device_ids(project)
    if device not in known_devices:
        raise ValueError(
            f"设备 {device!r} 不在 {project / 'configs'} 的清单里;可用: {sorted(known_devices)}"
        )
    clauses_path = project / "configs" / "clauses.yaml"
    clauses = (
        (yaml.safe_load(clauses_path.read_text(encoding="utf-8")) or {}).get("clauses") or {}
        if clauses_path.is_file()
        else {}
    )
    if clause.startswith("free:"):
        if not clause[len("free:"):].strip():
            raise ValueError("free: 后面的原文不能为空")
    elif not clauses_path.is_file():
        raise ValueError(
            f"{project} 没有 configs/clauses.yaml;把 clause 写成 'free: <原文>',"
            "或先跑 testplatform scaffold case"
        )
    elif clause not in clauses:
        raise ValueError(
            f"条款 {clause!r} 不在 {clauses_path};先加一行,或换一个已存在的 id。"
            f"可用: {sorted(clauses)}"
        )
    function = re.sub(r"\W+", "_", name).strip("_")
    if not function:
        raise ValueError(f"用例名 {name!r} 生成不出 Python 函数名")
    test_file = project / "tests" / f"test_{function}.py"
    if test_file.exists():
        raise FileExistsError(f"{test_file} 已存在;换一个名字,或先删掉它")
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text(
        NEW_CASE.format(
            case_id_literal=repr(case_id or function),
            clause_literal=repr(clause),
            device_literal=repr(device),
            function=function,
        ),
        encoding="utf-8",
    )
    return test_file


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
    (project / "pyproject.toml").write_text(
        PYPROJECT_DRIVER if driver_kind else PYPROJECT, encoding="utf-8"
    )
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
    print(f"  跑它:  testplatform run {project / 'tasks' / (name + '.yaml')} --root {project}"
          f" --evidence-dir {project / 'reports'}")
    return 0
