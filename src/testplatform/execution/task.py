"""
任务执行器 —— 一份任务文件 → 一条命令跑完:分配端口、拉起模拟从站、起产品、等它就绪、
跑选中的用例、落证据、给出退出码。

任务文件的形状(票 01 定形:五个必填字段 + 一个可选的 kind):
    id       这一轮的名字,也是证据目录名的一部分
    product  只管身份与追踪,不参与任何判定
    launch   命令行。占位符是**闭集**:
               ${PORT}              平台分配的产品监听端口(必须有 —— 写死数字并行时会撞端口)
               ${SLAVE_PORT:<设备id>}  那台模拟从站的端口,<设备id> 必须在这份任务引用的清单里
               ${PYTHON}            当前解释器(任务文件不绑死某个 python 路径)
    devices  设备清单,相对**项目目录**解析(清单与用例一样只来自项目目录;launch 里的脚本路径
             多一档包内兜底,见 `_resolve_product_command`)
    select   用例筛选,与 --select 同一套写法(model: / capability: / tag: / case:)
    kind     可选,这一轮是哪种:acceptance(缺省,验收设备)/ smoke(只验通路,不验设备)
             取值是闭集:写别的在加载期报错。静默当 acceptance 会让「设备级 0/0」有两种读法,
             而冒烟任务验的本来就不是设备(见 reporting/report.py 的 0/0 分叉)。

加载期报错必须指出是哪一条:清单不存在、产品命令不可执行、占位符拼错、从站 id 不存在、
select 写法不认识、kind 不在闭集里。留到跑起来再报,现场只会看到"产品起不来" —— 而那可能是清单、命令、
端口三件事中的任何一件。

模拟从站用 `tools/mock_modbus_meter.py`(真进程、真 socket),说哪种帧由该设备型号的 protocol 推导:
`modbus_tcp` → MBAP 帧,其余(含拼错的规约名)一律 RTU 帧 —— 帧格式不写第二遍。从站地址与寄存器初值
取清单里那台设备自己声明的 `bus_address` / `connection.simulated_registers`:平台侧读同一台表、
产品也读同一台表,两边读到的东西才有可比性。

产品进程的死因按「平台自己有没有发起过终止」分类,不看退出码(票 03):
    self_exit        产品自己退出(非零码 / 收到信号)= 产品不合格;证据记下 how=self_exit 与 code / signal
    platform_killed  平台收尾、任务超时、取消、重启 —— 终止本身不产生结论,已跑完的用例保持各自结论
    起不来            产品没到就绪就没了(可执行缺失 / 参数错 / 权限)= 平台错误,不记 product_exit
被平台叫停时没跑到的用例单独记一条 unexecuted{count, reason},与产品死因分开 —— 报告要能分开陈述。

证据落在任务自己的目录里(缺省 `evidence/task-<id>-<UTC 时间戳>/`):
    task.json         这一轮的任务事实:替换后的实际命令行、分配到的端口、就绪等待时长、退出码,
                      端口来源(port_source / port_range / port_attempts)、
                      runtime(启动 / 首个读数 / 总耗时)、env(commit / 平台版本 / 产品摘要 / 模拟器版本),
                      process_events(死因记录 product_exit、重启点 product_restart、未执行 unexecuted)
    每设备 JSONL      由证据层写(任务目录就是 pytest 那一轮的 --evidence-dir)
    run-scope.json    这一轮的范围(收集 / 执行 / 跳过 / 失败)
    round-status.json 这一轮状态(有没有断链)
    product.out/.err  产品自己的 stdout / stderr
    report.md         这一轮的结论(四档 + 两栏通过率),人读;summary.json 是同一份的机器读版
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from testplatform.cases import parse_select
from testplatform.config.loader import load_device_config
from testplatform.config import origin, paths
from testplatform.config.manifest import Manifest, parse_manifest
from testplatform.execution.death import attempt_exit, exit_event, unexecuted_event
from testplatform.execution.process_control import (
    CONTROL_DIR_ENV,
    PRODUCT_CRASHED,
    RESTART_ACK_FILE,
    RESTART_REQUEST_FILE,
    TASK_CANCELLED,
    TASK_TIMEOUT,
    TASK_TIMEOUT_ENV,
    request_stop,
    stop_reason,
)
from testplatform.reporting.evidence import parse_at
from testplatform.reporting.report import (
    VERDICT_FAILED,
    VERDICT_INCOMPLETE,
    VERDICT_INVALID,
    VERDICT_QUALIFIED,
    build,
    write_report,
)

# 平台根与包内资源统一由 config/paths.py 解析(显式 > TESTPLATFORM_ROOT > 探测):
# 这里不按源码树相对层级推路径 —— 那个写法在安装形态下指向 site-packages 的上级,四处路径全不存在。
TASK_RECORD_FILE = "task.json"

# 产品慢启动多久都等:超时到了就是"没就绪",不是"用例失败"。
DEFAULT_READY_TIMEOUT = 15.0

# 端口被抢时换端口重来的次数(含第一次)。三次都不行 = 平台错误,不是产品不合格。
PORT_ATTEMPTS = 3

# 产品端口从平台自己的段里选,**不从系统的临时端口池里要**:那个池是全机器共享的,
# 别的进程的 bind(0) 会在「平台放开端口 → 产品 bind」这个窗口里刚好被分到同一个端口,
# 产品 bind 失败报 10013(Windows 上 SO_REUSEADDR 的抢占语义),于是组合闸门下偶发红(票 14)。
# 段必须与系统分配段不重叠:Windows / macOS 从 49152 起,Linux 默认从 32768 起。
PRODUCT_PORT_RANGE = (20000, 31999)
PORT_RANGE_ENV = "TESTPLATFORM_PORT_RANGE"
# 系统分配段的保守下界(Linux 默认 32768):段碰到它就算重叠 —— 可配置项不能把保护关掉。
OS_EPHEMERAL_FLOOR = 32768
# 证据里的端口来源:平台自己的段 / 测试注入的分配器(见 run_task 的 port_reserver)。
PORT_SOURCE_RANGE = "platform_range"
PORT_SOURCE_INJECTED = "injected"

# port_retry.reason:只允许写明依据的两种 —— “因为 bind 成功所以重试”这种反因果不许出现。
PORT_RETRY_PORT_TAKEN = "端口他人不可用"
PORT_RETRY_BIND_REPORTED = "产品 stderr 自报 bind 失败"
PORT_RETRY_REASONS = (PORT_RETRY_PORT_TAKEN, PORT_RETRY_BIND_REPORTED)
# 产品 bind 失败时,不同运行时的措辞不一样(Windows 中文是"以一种访问权限不允许的方式…",
# 英文是 "address already in use")。这里只认它们自报的错误码/短语,**只用来决定要不要重试**,
# 从不参与结论:结论永远由"就绪了没有 / 进程怎么死的"决定。
BIND_FAILURE_HINTS = ("10013", "10048", "address already in use", "eaddrinuse")

# 子进程与父进程说同一种编码:否则 Windows 上子进程按控制台编码写、父进程按 locale 解码,
# 一行中文启动输出就能把这一轮炸成 UnicodeDecodeError。
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

FIELDS = ("id", "product", "launch", "devices", "select")
# 任务自己是哪一种。acceptance 缺省 = 这一轮要验设备;smoke = 只验通路(minimal.yaml 那种)。
KIND_FIELD = "kind"
KIND_ACCEPTANCE = "acceptance"
KIND_SMOKE = "smoke"
KINDS = (KIND_ACCEPTANCE, KIND_SMOKE)
# 模拟从站说哪种帧:与清单里的 connection.simulated_frames 同一套取值(同一概念一个拼法)。
FRAME_MODBUS_RTU = "modbus_rtu"
FRAME_MODBUS_TCP = "modbus_tcp"
SLAVE_PLACEHOLDER = "SLAVE_PORT:"
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_SLAVE_BANNER = re.compile(r"监听 \S+:(\d+)")

PROCESS_EVENTS = "process_events"
# pytest 自己的这条退出码:pytest NO_TESTS_COLLECTED(一条都没收集到)。
PYTEST_NO_TESTS_COLLECTED = 5

# 等 pytest 时检查产品死没死、用例要不要重启产品的间隔。
CONTROL_POLL_SECONDS = 0.1


# 退出码 = 结论(票 04 补丁 2):报告判什么,进程就退什么。pytest 自己的退出码只进证据。
EXIT_QUALIFIED = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_INCOMPLETE = 3
EXIT_PLATFORM_ERROR = 4
_VERDICT_EXIT_CODES = {
    VERDICT_QUALIFIED: EXIT_QUALIFIED,
    VERDICT_FAILED: EXIT_FAILED,
    VERDICT_INVALID: EXIT_INVALID,
    VERDICT_INCOMPLETE: EXIT_INCOMPLETE,
}


class TaskFileError(ValueError):
    """任务文件本身写错了:加载期报,消息里带任务文件路径与是哪一条。"""


class TaskRunError(RuntimeError):
    """任务没跑起来(产品没就绪 / 从站起不来):平台侧问题,不是用例失败。"""


class PortRangeExhausted(TaskRunError):
    """整段产品端口都 bind 不上:平台错误。带上实际试过的端口数,证据里的 port_attempts 读它。"""

    def __init__(self, message: str, *, tried: int) -> None:
        super().__init__(message)
        self.tried = tried


@dataclass(frozen=True)
class Task:
    """一份加载并校验过的任务。`manifest` 是它引用的设备清单,从站地址与寄存器初值从这里取。"""

    id: str
    product: str
    launch: tuple[str, ...]
    devices: Path
    select: str
    kind: str
    manifest: Manifest
    slave_ids: tuple[str, ...]
    # 这份任务是在哪个项目目录上加载的:证据目录、模拟从站、子 pytest 都从它派生;
    platform: paths.PlatformLayout


# ---------------------------------------------------------------- 加载期 ----


def load_task(path: str | Path, *, root: str | Path | None = None) -> Task:
    """读任务文件并校验。任何一处不通都抛 TaskFileError,消息指出是哪个字段哪一条。

    root 是项目目录(省略时按 TESTPLATFORM_ROOT 或当前目录,见 config/paths.py);
    任务里的相对路径按它解析 —— 只有一套路径规则。平台那几样资源(从站脚本 / 寄存器表 /
    条款清单 / 子 pytest 的 ini)由这里派生出的 PlatformLayout 解析,项目目录里没有就用包内的。
    """
    platform = paths.layout(explicit=root)
    task_path = Path(path)
    if not task_path.is_file():
        raise TaskFileError(f"任务文件不存在: {task_path}")
    try:
        raw = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskFileError(f"任务文件 {task_path}: YAML 读不了: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise TaskFileError(
            f"任务文件 {task_path}: 顶层必须是映射,字段 {list(FIELDS)}"
        )

    missing = [name for name in FIELDS if name not in raw]
    if missing:
        raise TaskFileError(f"任务文件 {task_path}: 缺字段 {missing},必须有 {list(FIELDS)}")
    unknown = sorted(set(raw) - set(FIELDS) - {KIND_FIELD})
    if unknown:
        raise TaskFileError(
            f"任务文件 {task_path}: 不认识的字段 {unknown},"
            f"只允许 {list(FIELDS)} 与可选的 {KIND_FIELD}"
        )
    # 缺省 acceptance:不写 kind 的任务按「要验设备」对待,这是严的那一边。
    kind = raw.get(KIND_FIELD, KIND_ACCEPTANCE)
    if kind not in KINDS:
        raise TaskFileError(
            f"任务文件 {task_path}: kind 只允许 {list(KINDS)}(缺省 {KIND_ACCEPTANCE}),拿到 {kind!r}"
        )
    for name in ("id", "product", "select"):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise TaskFileError(
                f"任务文件 {task_path}: {name} 必须是非空字符串,拿到 {raw[name]!r}"
            )

    launch = raw["launch"]
    if not isinstance(launch, list) or not all(
        isinstance(argument, str) and argument for argument in launch
    ):
        raise TaskFileError(f"任务文件 {task_path}: launch 必须是非空字符串列表,拿到 {launch!r}")
    launch = tuple(launch)
    _check_placeholder_names(task_path, launch)
    if not any("${PORT}" in argument for argument in launch):
        raise TaskFileError(
            f"任务文件 {task_path}: launch 里没有 ${{PORT}}"
            f" —— 端口要由平台分配,写死数字在并行跑时会撞端口"
        )

    devices = _resolve_devices(task_path, raw["devices"], platform.root)
    manifest = _load_manifest(task_path, devices)
    slave_ids = _slave_ids(task_path, launch, manifest)
    launch = _resolve_product_command(task_path, launch, platform.root)
    try:
        parse_select(raw["select"])
    except ValueError as exc:
        raise TaskFileError(
            f"任务文件 {task_path}: select 写错了({raw['select']!r}): {exc}"
        ) from exc
    return Task(
        id=raw["id"].strip(),
        product=raw["product"].strip(),
        launch=launch,
        devices=devices,
        select=raw["select"],
        kind=kind,
        manifest=manifest,
        slave_ids=slave_ids,
        platform=platform,
    )


def _resolve_devices(task_path: Path, declared: Any, root: Path) -> Path:
    if not isinstance(declared, str) or not declared.strip():
        raise TaskFileError(
            f"任务文件 {task_path}: devices 必须是清单路径字符串,拿到 {declared!r}"
        )
    path = Path(declared)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise TaskFileError(f"任务文件 {task_path}: devices 指向的清单不存在: {path}")
    return path


def _load_manifest(task_path: Path, devices: Path) -> Manifest:
    try:
        return parse_manifest(load_device_config(devices))
    except (KeyError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise TaskFileError(
            f"任务文件 {task_path}: devices 清单 {devices} 读不进来: {exc}"
        ) from exc


def _check_placeholder_names(task_path: Path, launch: tuple[str, ...]) -> None:
    """占位符是闭集:名字写错必须当场报,不能留成字面量跟着命令行跑下去。"""
    for argument in launch:
        for name in _PLACEHOLDER.findall(argument):
            if name in ("PORT", "PYTHON") or name.startswith(SLAVE_PLACEHOLDER):
                continue
            raise TaskFileError(
                f"任务文件 {task_path}: 不认识的占位符 ${{{name}}},"
                f"可用值: ${{PORT}} / ${{SLAVE_PORT:<设备id>}} / ${{PYTHON}}"
            )


def _slave_ids(task_path: Path, launch: tuple[str, ...], manifest: Manifest) -> tuple[str, ...]:
    """launch 里出现的 ${SLAVE_PORT:<id>}:id 必须在这份任务引用的清单里。"""
    declared = [spec["id"] for spec in manifest.devices]
    found: list[str] = []
    for argument in launch:
        for name in _PLACEHOLDER.findall(argument):
            if not name.startswith(SLAVE_PLACEHOLDER):
                continue
            device_id = name.partition(SLAVE_PLACEHOLDER)[2]
            if device_id not in declared:
                raise TaskFileError(
                    f"任务文件 {task_path}: ${{{name}}} 里的设备 id {device_id!r} 不在清单里,"
                    f"清单里的设备: {sorted(declared)}"
                )
            if device_id not in found:
                found.append(device_id)
    return tuple(found)


def _resolve_product_command(task_path: Path, launch: tuple[str, ...], root: Path) -> tuple[str, ...]:
    """产品命令不可执行要在加载期报:'起不来'的三种原因(清单/命令/端口)必须先分开。

    两种写法都查:直接给可执行文件(`./product`)与给解释器 + 脚本(`${PYTHON} xxx.py`)。
    后者漏掉的话,拼错的脚本名只会在产品启动后表现成"退出码 2",而那是运行期的事。

    脚本路径**走资源的同一个解析入口**(`config/paths.py::resource`):项目目录优先,项目目录
    没有就用包内那份,解析到的路径写回 launch。示例产品随包走(票 09e),照项目目录拼路径会让
    装完的用户在只有 `configs/` 的目录里 exit 4,而消息还会指着他自己的项目。写回绝对路径也让
    证据里的实际命令行与真被执行的那条一致(产品 sha256 / 死因都指得回同一个文件)。
    """
    argv0 = _PLACEHOLDER.sub(
        lambda match: sys.executable if match.group(1) == "PYTHON" else "0", launch[0]
    )
    resolved = list(launch)
    if "/" in argv0 or "\\" in argv0 or argv0.endswith(".py"):
        # 占位符(${PYTHON})不写死:解释器是哪一个是运行时才定的。
        if "${" not in launch[0]:
            resolved[0] = str(_existing_script(task_path, argv0, root))
    elif shutil.which(argv0) is None:
        raise TaskFileError(f"任务文件 {task_path}: 产品命令不可执行: PATH 里找不到 {argv0!r}")
    for index, argument in enumerate(launch[1:], start=1):
        if argument.endswith(".py") and "${" not in argument:
            resolved[index] = str(_existing_script(task_path, argument, root))
    return tuple(resolved)


def _existing_script(task_path: Path, written: str, root: Path) -> Path:
    """这一条脚本参数解析到哪(项目目录优先,包内兜底);解析不到就在加载期报。

    两边都没有时分两路措辞:示例产品只随包走,缺了就是**包内缺**(安装损坏)——
    说成"你项目里没有"会让人去改自己的项目,而真正坏的是安装。
    """
    path = paths.resource(written, root=root)
    if path.is_file():
        return path
    if Path(written).as_posix() == paths.SAMPLE_PRODUCT:
        raise TaskFileError(
            f"任务文件 {task_path}: 产品命令不可执行: 包内缺 {written}(平台包装坏了,重装平台包)\n"
            f"  包内应有: {paths.PACKAGE_DATA / written}(wheel 里是 site-packages/testplatform/_data/)\n"
            f"  或在项目目录放一份: {Path(root) / written}(放同名文件也认,项目目录优先)"
        )
    project = Path(written) if Path(written).is_absolute() else Path(root) / written
    raise TaskFileError(f"任务文件 {task_path}: 产品命令不可执行: {project} 不存在")


# ---------------------------------------------------------------- 执行期 ----


@dataclass
class _Slave:
    device_id: str
    port: int
    bus_address: int
    argv: tuple[str, ...]
    process: subprocess.Popen


def run_task(
    task: Task,
    *,
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    registers: str | Path | None = None,
    evidence_dir: str | Path | None = None,
    task_timeout: float | None = None,
    port_reserver: Callable[[], tuple[socket.socket | None, int]] | None = None,
) -> int:
    """跑一份任务,返回这一轮的退出码:**报告判成哪一档就退哪个码**,不是 pytest 的退出码
    (任务跑不起来抛 TaskRunError)。pytest 自己的退出码照旧落进 task.json / summary.json 的
    `pytest_exit_code`,诊断用;它决定不了进程退出码 —— 否则「报告红、CI 绿」的假绿会一直在。

    顺序固定:先起模拟从站(端口只能由它自己报),再替换占位符起产品,再等就绪,
    最后才跑用例 —— 用例开始时产品必须已经在监听,否则"就绪等待"等于没做。

    task_timeout 是这一轮的用例时间预算(秒,从第一条用例开跑算起):到点了平台不再开新用例,
    已跑完的结论留着,没跑到的记 unexecuted{reason=task_timeout}。检查点是用例边界 ——
    在用例中间硬杀 pytest 会把 run-scope 一起弄丢,而"未执行几条"只有它说得清。

    port_reserver 是"端口从哪来、占住了没有"(缺省:在平台自己的端口段里按顺序取一个能 bind 的)。
    它返回 (占住的套接字 | None, 端口):None 表示这个端口此刻不可用。做成可注入是给测试留的
    **外部缝** —— 要验的是"端口被占 → 换一个重来",而那个竞态没法在测试里稳定复现;
    注入一个分配器比 monkeypatch 私有函数诚实(依赖由使用者给,见架构的 user-alloc)。

    端口来源进证据(task.json 的 port_source / port_range / port_attempts):下次红了,
    看证据就知道端口是从哪来的、试过几次 —— 否则"平台自选段"和"系统分配"在证据上长得一模一样。
    """
    ports = _product_port_range()
    directory = (
        Path(evidence_dir) if evidence_dir else _default_evidence_dir(task.id, task.platform)
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record: dict[str, Any] = {
        "task": task.id,
        "kind": task.kind,
        "product": task.product,
        "devices": str(task.devices),
        "select": task.select,
        "product_port": None,
        "port_source": PORT_SOURCE_INJECTED if port_reserver is not None else PORT_SOURCE_RANGE,
        "port_range": _port_range_text(ports),
        "port_attempts": 0,
        "slave_ports": {},
        "slave_addresses": {},
        "slaves": [],
        "launch": [],
        "readiness": None,
        # 每样资源从哪来(package / project):"装完能用"与"根里四样齐"在证据里必须分得开。
        "layout": task.platform.sources(),
        "pytest_exit_code": None,
        "product_exit_code": None,
        "error": None,
        "started_at": _stamp(),
        "runtime": {"startup_seconds": None, "first_reading_seconds": None, "total_seconds": None},
        "env": {},
        PROCESS_EVENTS: [],
    }
    slaves: list[_Slave] = []
    # 产品进程的当前一个。重启会换进程,所以放在列表里传给出错也要收尾的那一层。
    products: list[subprocess.Popen | None] = [None]
    # 已经记过死因的进程:监控看到一次、收尾又看到一次,不能记两遍。
    recorded: set[subprocess.Popen] = set()
    # 产品到过就绪没有:没到就绪就退出 = 起不来(平台错误),不是死因。
    ready = False
    pytest_exit_code: int | None = None
    # 试过几次(含成功那次)。整段不可用时由 PortRangeExhausted 里的实际端口数覆盖。
    attempts = 0
    exhausted = False
    reserve = port_reserver if port_reserver is not None else partial(_open_product_port, ports)
    try:
        for device_id in task.slave_ids:
            slave = _start_slave(task, device_id, directory, registers)
            slaves.append(slave)
            record["slave_ports"][device_id] = slave.port
            record["slave_addresses"][device_id] = slave.bus_address
            record["slaves"].append(
                {
                    "device": device_id,
                    "port": slave.port,
                    "bus_address": slave.bus_address,
                    "argv": list(slave.argv),
                }
            )
        values = {"PORT": "", "PYTHON": sys.executable}
        values.update(
            {f"{SLAVE_PLACEHOLDER}{slave.device_id}": str(slave.port) for slave in slaves}
        )
        tried: list[int] = []
        for attempt in range(1, PORT_ATTEMPTS + 1):
            attempts = attempt
            probe, product_port = reserve()
            tried.append(product_port)
            record["product_port"] = product_port
            values["PORT"] = str(product_port)
            record["launch"] = [_substitute(argument, values) for argument in task.launch]
            record["env"] = _environment(record["launch"], task.platform)
            # 先落一次:用例要按 task.json 里的端口去连任务起的那台从站。跑完再落一次带退出码。
            _write_record(directory, record)

            if probe is None:
                # 分配器给的端口此刻就不可用(被别人占 / 系统保留段):产品还没起,换一个重来。
                # 这条不再去复查"端口现在还可不可用" —— 刚才.bind 已经失败过一次,
                # 而 Windows 上这种失败可能是瞬时的(复查会翻成"可用",于是白丢一次重试)。
                failure = TaskRunError(
                    f"端口 {product_port} 分配到时已经不可用(被别的进程占着或被系统保留)"
                )
                retry_reason: str | None = PORT_RETRY_PORT_TAKEN
            else:
                # 占住它到最后一刻再放开:这样"产品 bind 时端口可用"是此刻验证过的,
                # 而不是"刚才探过"。窗口从 close() 到产品 bind,只有微秒级。
                probe.close()
                products[0] = _start_product(record["launch"], directory, task.platform.root)
                try:
                    record["readiness"] = _wait_ready(
                        products[0], product_port, ready_timeout, directory / "product.err"
                    )
                except TaskRunError as exc:
                    failure = exc
                    retry_reason = _port_trouble_reason(products[0], product_port, str(failure))
                else:
                    ready = True
                    break

            # 到这里就是这一次尝试失败。要不要换端口重来、依据是哪一条,全由
            # _port_trouble_reason 一处判定(判序写死:先看产品死活,再看端口能不能用)。
            if retry_reason is None:
                raise failure
            abandoned = _abandon_product(record, products, recorded)
            if attempt == PORT_ATTEMPTS:
                raise TaskRunError(
                    f"平台错误:{PORT_ATTEMPTS} 次尝试都拿不到可用端口"
                    f"({', '.join(map(str, tried))};"
                    f"port_attempts={attempt},port_source={record['port_source']})"
                    f" —— 产品没能在任何一个上监听。这不是产品不合格,而是端口被别的进程占着或被系统保留;"
                    f"重跑一次或换一段端口。(最后一次的失败原因: {failure})"
                ) from failure
            retry = {
                "kind": "port_retry",
                "attempt": attempt + 1,
                "reason": retry_reason,
                "port": product_port,
                "at": _stamp(),
            }
            if abandoned is not None:
                # 被放弃的那次尝试里产品怎么没的:退出码是事实,死因没到就绪就不成立(unverified)。
                retry["abandoned_product"] = abandoned
            _record_event(record, retry)
        _write_record(directory, record)
        record["pytest_exit_code"] = pytest_exit_code = _run_pytest(
            task,
            directory,
            products=products,
            record=record,
            ready_timeout=ready_timeout,
            task_timeout=task_timeout,
            recorded=recorded,
        )
        if pytest_exit_code == PYTEST_NO_TESTS_COLLECTED:
            # 用例只来自项目目录(不随包):一条都没收到就是项目目录里没有用例。
            # 交给 pytest 的 exit 5 一路走成"测试无效",现场只会去查产品 —— 这里说清是什么。
            raise TaskRunError(
                f"项目目录 {task.platform.root} 里没有用例:pytest 一条都没收集到。"
                f"用例不随平台包分发,要放在项目目录下(清单里的 testpaths / 目录结构照项目自己的 pytest 配置)。"
            )
    except TaskRunError as exc:
        record["error"] = str(exc)
        if isinstance(exc, PortRangeExhausted):
            attempts = exc.tried
        raise
    finally:
        record["port_attempts"] = attempts
        settled = _settle_product(record, products[0], ready=ready, recorded=recorded)
        if settled is not None:
            # 收尾时手上没有产品(端口重试用光)= 上一次尝试的退出码已经在 _abandon_product 里留下了。
            record["product_exit_code"] = settled
        for slave in slaves:
            _stop_slave(slave)
        _note_unexecuted(record, directory)
        _fill_runtime(record, directory, started)
        _write_record(directory, record)
        # 报告是消费层:证据写完才读得到 task.json 里的 runtime / env。
        write_report(directory)
    # 退出码从报告读:结论是判出来的,不是 pytest 说了算的。
    return _exit_code(build(directory).verdict, pytest_exit_code, directory)


def _exit_code(verdict: str, pytest_exit_code: int | None, directory: Path) -> int:
    """结论 → 退出码。pytest 没跑通(收集期崩 / 插件崩 / 一个用例都没收到)先报这一层:
    那时没有可判的结论,把它的码当结论读就是「红报告 + 绿 CI」。

    被平台叫停的轮次 pytest 也给 2,那不是 pytest 崩了 —— stop_reason 记着是谁叫的停,
    结论仍由报告说。与「测试无效」同码 2:两者都是"这一轮拿不出可用结论"。
    """
    if pytest_exit_code not in (0, 1) and stop_reason(directory) is None:
        return EXIT_INVALID
    return _VERDICT_EXIT_CODES[verdict]


def _default_evidence_dir(task_id: str, platform: paths.PlatformLayout) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return platform.evidence / f"task-{task_id}-{stamp}"


def _product_port_range() -> range:
    """这一轮的产品端口段:TESTPLATFORM_PORT_RANGE("起始-结束")覆盖,缺省 PRODUCT_PORT_RANGE。

    写错、或段与系统分配的临时端口段重叠,都在这里报(开跑前:从站与产品都还没起)。
    可配置项如果能把「端口不由系统随机分配」这个保护关掉,那保护就只是默认值了。
    """
    written = os.environ.get(PORT_RANGE_ENV, "").strip()
    if not written:
        start, end = PRODUCT_PORT_RANGE
    else:
        left, _, right = written.partition("-")
        try:
            start, end = int(left), int(right)
        except ValueError:
            raise TaskRunError(
                f"{PORT_RANGE_ENV}={written!r} 读不了:要写成 \"起始-结束\""
                f"(例: {PRODUCT_PORT_RANGE[0]}-{PRODUCT_PORT_RANGE[1]})"
            ) from None
        if not 0 < start <= end <= 65535:
            raise TaskRunError(
                f"{PORT_RANGE_ENV}={written!r} 不成立:要 0 < 起始 <= 结束 <= 65535"
            )
    if end >= OS_EPHEMERAL_FLOOR:
        raise TaskRunError(
            f"{PORT_RANGE_ENV} 的端口段 {start}-{end} 与系统分配的临时端口段重叠"
            f"(Windows / macOS 从 49152 起,Linux 默认从 {OS_EPHEMERAL_FLOOR} 起,这里按严的判)。"
            f"系统那段是全机器共享的:别的进程的 bind(0) 会把平台刚放开的端口拿走,产品 bind 报 10013 —— "
            f"和系统抢端口这件事又回来了,等于把「端口由平台自选」这个修复关掉。"
            f"换成 {PRODUCT_PORT_RANGE[0]}-{PRODUCT_PORT_RANGE[1]} 那样的段。"
        )
    return range(start, end + 1)


def _port_range_text(ports: range) -> str:
    return f"{ports.start}-{ports.stop - 1}"


# 每个进程从一个不同的位置开始,在段内按顺序扫:
#   同一个进程里游标只往前走,不会把刚放开给产品的端口再捡回来;
#   同一台机器上并发的几个进程(多个 xdist worker / 两个 worktree)不会沿着同一条序列同步走 ——
#   同步走时对方刚放开给产品的端口正好是你的下一个,旧的偶发红就原封不动回来了(实测过两次:
#   pid 相邻的 worker 起点只差几个端口,而 pytest-randomly 给每个 worker 种同一个种子,
#   用 random 取起点是四个 worker 拿到同一个数)。所以起点用进程熵,不用 random。
# 取 8 字节再取模:2 字节只有 65536 种、对 12000 取模会明显偏小(低段被选中的概率高 6 倍),
# 两个 run 于是都从低段起步、更容易撞在一起(实测到一次:27135/27136/27137 连丢)。
_PORT_CURSOR = itertools.count(int.from_bytes(os.urandom(8), "big"))


def _open_product_port(ports: range) -> tuple[socket.socket, int]:
    """在平台自己的段里按顺序拿一个能 bind 的端口,并把它拿住(拿住才算,不猜)。

    shortcut: 产品要自己 bind,所以到头来还是要放开它(见 _port_unavailable 的说明);
    要根治得把监听套接字传给产品(fd 继承)—— 产品是外部的,等它支持再换。
    现在:端口不从系统临时端口池里要(那个池是全机器共享的,别的进程的 bind(0) 会在
    「平台放开端口 → 产品 bind」这个窗口里抢走它),改从平台自己的段里按顺序取 ——
    这一段没有别人会从系统拿到,剩下的窗口只靠段内顺序 + 失败换端口重来(PORT_ATTEMPTS)兜底。

    整段都 bind 不上 = 平台错误(**不**静默回落到 bind(0)):那等于把上面这个保护关掉,
    而且修好了的偶发红会重新出现。
    """
    start = next(_PORT_CURSOR) % len(ports)
    for step in range(len(ports)):
        port = ports[(start + step) % len(ports)]
        probe = socket.socket()
        # 探针也要 SO_REUSEADDR:不带它去 bind 再 close,Windows 会把这条端口留成
        # "别的进程用 SO_REUSEADDR 就回 10013"的状态 —— 产品正好就是这种绑定方,
        # 于是平台自己的"验证"成了丢端口的原因(实测 A 不带 5/60 失败、C 带 0/60)。
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            probe.close()
            continue
        return probe, port
    raise PortRangeExhausted(
        f"平台错误:端口段 {_port_range_text(ports)} 的 {len(ports)} 个端口都不可用"
        f"(逐个 bind 过;port_attempts={len(ports)},port_source={PORT_SOURCE_RANGE}) —— "
        f"这不是产品不合格,而是这一段被别的进程占着(或被系统保留);"
        f"换一段({PORT_RANGE_ENV}=<起始>-<结束>)或腾出端口再跑。",
        tried=len(ports),
    )


def _port_trouble_reason(
    product: subprocess.Popen | None, port: int, failure_message: str
) -> str | None:
    """这一次失败算不算"端口问题"(该换端口重来),算的话**依据**是哪一条;不算返回 None。

    判序写死,不许倒过来:
      ① 产品自己报 bind 失败(stderr 里有 10013 / 10048 / …)→ 重试,依据是**产品的话**。
         这是产品自己给的因,不是我们拿端口探出来的旁证 —— 它也解释得了"产品已死"。
      ② 产品已经退出、且自己没说 bind 失败 → 那是它自己的死因,**不重试**。
         关键:端口 probe 成功过、或者此刻查着"不可用",都盖不过产品自己退出这件事 ——
         把旁证排在事实前面,就会把"产品崩了(退出码 3)"读成"端口被抢",白重试一次。
      ③ 产品还没起来(活着但没监听)且端口此刻别人也 bind 不上 → 重试,依据是端口。
      ④ 剩下的都是产品自己的死因 → 不重试。
    """
    if _looks_like_bind_failure(failure_message):
        return PORT_RETRY_BIND_REPORTED
    if product is not None and product.poll() is not None:
        return None
    if _port_unavailable(port):
        return PORT_RETRY_PORT_TAKEN
    return None


def _looks_like_bind_failure(message: str) -> bool:
    """失败消息里有没有"绑定端口失败"的痕迹(产品自己的 stderr 摘要在里面)。"""
    lowered = message.lower()
    return any(hint in lowered for hint in BIND_FAILURE_HINTS)


def _port_unavailable(port: int) -> bool:
    """这个端口现在还能被 bind 吗?返回 True = 不可用(被别人占 / 系统保留段)。

    判断"产品起不来是不是因为端口"只认这一个依据:不匹配 stderr 文案(不同运行时措辞不同,
    中文 Windows 是"以一种访问权限不允许的方式…"),而是问操作系统"现在这个地址能不能用"。
    竞态窗口仍在(检查完到下次 bind 之间),所以它只用来**决定要不要重试**,不用来下结论。
    """
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _abandon_product(
    record: dict[str, Any],
    products: list[subprocess.Popen | None],
    recorded: set[subprocess.Popen],
) -> dict | None:
    """放弃这一次尝试的产品进程:停掉它、**不记死因**(它没到过就绪,起不来是平台侧的事)。

    但退出码不许丢:丢了它,task.json 里只剩 product_exit_code=null,平台就说不清自己为什么停
    (票 14)。返回这条尝试的进程事实(how / code),由调用方写进 port_retry —— how 用
    unverified:没到过就绪就不下死因结论,那不等于"产品没崩",而是"平台没拿到死因"。
    """
    product = products[0]
    products[0] = None
    if product is None:
        return None
    was_running = product.poll() is None
    if was_running:
        _terminate(product)
    recorded.add(product)
    facts = attempt_exit(product, platform_stopped=was_running)
    record["product_exit_code"] = facts["code"]
    return facts


def _substitute(argument: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise TaskFileError(
                f"占位符 ${{{name}}} 没有值;可用: {sorted(values)}(加载期已校验过一次,"
                f"走到这里说明执行期漏了一种)"
            )
        return values[name]

    return _PLACEHOLDER.sub(replace, argument)


def _slave_frame(manifest: Manifest, spec: Mapping[str, Any]) -> str:
    """这台模拟从站该说哪种帧:由该设备型号的 protocol 推导,任务文件里不重复写第二遍。

    只有 modbus_tcp 是 MBAP;其余(含写错的规约名)一律 RTU —— 未知规约按 MBAP 起从站,
    对端收到的是一帧解不了的字节,而现场只会看到"读数超时"。
    """
    if manifest.models[spec["model"]].protocol == FRAME_MODBUS_TCP:
        return FRAME_MODBUS_TCP
    return FRAME_MODBUS_RTU


def _start_slave(
    task: Task, device_id: str, directory: Path, registers: str | Path | None
) -> _Slave:
    """一台设备一条模拟从站进程(一个 mock = 一条物理总线 = 一个监听端口)。

    端口交给它自己报(`--port 0` + 读启动行),平台不猜:先分配再 bind 的窗口在并行时会撞。
    """
    spec = next(spec for spec in task.manifest.devices if spec["id"] == device_id)
    bus_address = int(spec.get("bus_address", 1))
    argv = (
        sys.executable,
        str(task.platform.slave_script),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--slave",
        str(bus_address),
        "--registers",
        str(_registers_for(spec, device_id, directory, registers, task.platform)),
        "--frame",
        _slave_frame(task.manifest, spec),
        "--stop-on-stdin-close",
    )
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_child_env(task.platform),
    )
    banner = process.stdout.readline()
    match = _SLAVE_BANNER.search(banner or "")
    if match is None:
        cause = process.stderr.read() if process.stderr else ""
        _terminate(process)
        raise TaskRunError(f"模拟从站 {device_id!r} 没起来: {banner!r} {cause!r}")
    return _Slave(device_id, int(match.group(1)), bus_address, argv, process)


def _registers_for(
    spec: Mapping[str, Any],
    device_id: str,
    directory: Path,
    registers: str | Path | None,
    platform: paths.PlatformLayout,
) -> Path:
    """寄存器初值取清单里这台设备自己声明的那份:`$(SLAVE_PORT:<id>)` 指向的表必须和平台读的是同一张表。"""
    if registers is not None:
        return Path(registers)
    declared = spec.get("connection", {}).get("simulated_registers")
    if not declared:
        return platform.registers
    path = directory / f"slave-{device_id}-registers.json"
    path.write_text(
        json.dumps({int(key): int(value) for key, value in declared.items()}),
        encoding="utf-8",
    )
    return path


def _wait_ready(
    product: subprocess.Popen, port: int, timeout: float, stderr_path: Path
) -> dict[str, Any]:
    """等产品监听端口。等待时长进证据 —— 没有它,'就绪等待生效'这件事只能靠猜。"""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if product.poll() is not None:
            raise TaskRunError(
                f"产品进程在就绪前退出(退出码 {product.returncode}):"
                f"{_tail(stderr_path)}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            time.sleep(0.05)
            continue
        return {"seconds": round(time.monotonic() - started, 3), "timeout": timeout}
    raise TaskRunError(f"产品在 {timeout}s 内没有监听端口 {port}(没起来或起得比超时更慢)")


def _tail(path: Path, lines: int = 3) -> str:
    """产品自己说的话是死因的第一手材料:一行都不给,现场只能去猜端口 / 参数 / 代码。"""
    try:
        written = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return "产品没有留下 stderr"
    return " | ".join(written[-lines:]) if written else "产品没有留下 stderr"


def _start_product(
    launch: list[str], directory: Path, root: Path, *, mode: str = "w"
) -> subprocess.Popen:
    """起产品进程,stdout / stderr 落进证据目录;重启时追加,不覆盖上一段的输出。"""
    with (
        open(directory / "product.out", mode, encoding="utf-8") as stdout,
        open(directory / "product.err", mode, encoding="utf-8") as stderr,
    ):
        return subprocess.Popen(
            launch,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            env=CHILD_ENV,
        )


def _child_env(platform: paths.PlatformLayout) -> dict[str, str]:
    """子进程(模拟从站 / pytest)都要能 import 到平台代码。

    插件在 ini 的 pythonpath 生效之前就被 import,`-p testplatform.plugin` 直接 ImportError 的话
    整轮起不来;从站脚本(wheel 里在包内 `_data/tools/` 下)也要 import `testplatform.protocol`。
    项目目录也在 PYTHONPATH 上:项目自己带的辅助模块能被子进程看到。
    **顺序与项都由 `paths.sys_path` 一条规则给**:父进程与子进程看到的代码层、项目层是同一份,
    否则父进程按 entry point 判"插件已装"、子进程却 import 不到(证据里只剩 exit 4)。
    """
    return {
        **CHILD_ENV,
        "PYTHONPATH": os.pathsep.join(
            [*paths.sys_path(root=platform.root), CHILD_ENV.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }


def _plugin_arguments() -> list[str]:
    """显式 `-p testplatform.plugin`,除非 entry point 已经会把它装上。

    装成包以后 pytest 自己按 pyproject 的 pytest11 entry point 加载插件;这时再传 `-p`
    就是同一个模块两个名字,pytest 报 `Plugin already registered` 中止整轮。
    判据是 entry point **能不能 resolve 到插件模块**(见 config/paths.py),不是"包装没装"。
    """
    if paths.plugin_from_entry_point():
        return []
    return ["-p", paths.PLUGIN_MODULE]


def _run_pytest(
    task: Task,
    directory: Path,
    *,
    products: list[subprocess.Popen | None],
    record: dict[str, Any],
    ready_timeout: float,
    task_timeout: float | None,
    recorded: set[subprocess.Popen],
) -> int:
    """跑任务选中的用例。证据目录就是任务目录:每设备 JSONL、范围、轮次状态都落在同一处。

    等 pytest 的这段时间里还要盯着两件事:产品自己死了没有(死了就记死因、叫停本轮剩余用例),
    用例要求重启产品没有。两者都只写控制面文件,pytest 在用例边界上读(见 process_control)。
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:randomly",
        # 显式加载插件:否则 --device-config 这类选项在"插件还没加载"的早期解析里
        # 会被当成未知参数,而 pytest 认定"路径"的方式又依赖这些参数值 —— 设备清单位于
        # 仓库之外(tmp/CI 证据目录)时,整轮直接退出码 4(实测)。
        *_plugin_arguments(),
        # 平台叫停本轮 / 用例要求重启产品的控制面:只在任务跑起来时有事做。
        "-p",
        "testplatform.execution.process_control",
        # 显式指定 ini:嵌套跑不靠"从 cwd 猜 pyproject.toml"(设备清单在仓库之外时它会猜丢),
        # 否则 markers / testpaths / timeout 全不生效,整轮变成"另一个 pytest 配置"。
        "-c",
        str(task.platform.pytest_ini),
        "--device-config",
        str(task.devices),
        "--select",
        task.select,
        "--evidence-dir",
        str(directory),
    ]
    env = _child_env(task.platform)
    env[CONTROL_DIR_ENV] = str(directory)
    if task_timeout is not None:
        env[TASK_TIMEOUT_ENV] = str(task_timeout)
    process = subprocess.Popen(command, cwd=task.platform.root, env=env)
    try:
        while process.poll() is None:
            product = products[0]
            if (
                product is not None
                and product.poll() is not None
                and stop_reason(directory) is None
            ):
                # 产品自己退出:这是死因,不是超时。剩下没跑到的用例记无效,已跑完的结论不动。
                _record_exit(
                    record, product, ready=True, platform_stopped=False, recorded=recorded
                )
                request_stop(directory, PRODUCT_CRASHED)
            if (directory / RESTART_REQUEST_FILE).is_file():
                products[0] = _restart_product(
                    products, record, directory, ready_timeout, recorded, task.platform.root
                )
            time.sleep(CONTROL_POLL_SECONDS)
    except KeyboardInterrupt:
        # 被取消:本轮不再开新用例,让 pytest 把 run-scope 写完再走。
        request_stop(directory, TASK_CANCELLED)
        _await_exit(process)
        raise
    return int(process.returncode or 0)


def _await_exit(process: subprocess.Popen, grace: float = 10.0) -> None:
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _terminate(process)


def _restart_product(
    products: list[subprocess.Popen | None],
    record: dict[str, Any],
    directory: Path,
    ready_timeout: float,
    recorded: set[subprocess.Popen],
    root: Path,
) -> subprocess.Popen:
    """用例要求的冷启动:先停掉当前产品(平台发起的终止,不算死因),再起一个新的、等它就绪。

    重启点写进证据,回执给用例 —— 用例只知道"重启好了",不知道进程是谁起的。
    """
    request_path = directory / RESTART_REQUEST_FILE
    try:
        asked = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        asked = {}
    request_path.unlink(missing_ok=True)
    _settle_product(record, products[0], ready=True, recorded=recorded)
    product = _start_product(record["launch"], directory, root, mode="a")
    record["readiness"] = _wait_ready(
        product, record["product_port"], ready_timeout, directory / "product.err"
    )
    _record_event(
        record,
        {"kind": "product_restart", "at": _stamp(), "reason": asked.get("reason")},
    )
    _write_record(directory, record)
    (directory / RESTART_ACK_FILE).write_text(
        json.dumps(
            {"at": _stamp(), "pid": product.pid, "port": record["product_port"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return product


def _stop_product(product: subprocess.Popen | None) -> int | None:
    if product is None:
        return None
    if product.poll() is None:
        _terminate(product)
    return product.returncode


def _stop_slave(slave: _Slave) -> None:
    """关 stdin 让它走 shutdown()(退出码 0),而不是被硬杀 —— 端口要真的被它自己关掉。"""
    try:
        if slave.process.stdin is not None and not slave.process.stdin.closed:
            slave.process.stdin.close()
        slave.process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        _terminate(slave.process)


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _settle_product(
    record: dict[str, Any],
    product: subprocess.Popen | None,
    *,
    ready: bool,
    recorded: set[subprocess.Popen],
) -> int | None:
    """停掉产品并记下它的死因。

    平台自己发的终止(这里就是收尾)记 how=platform_killed:终止本身不产生结论。
    产品在收尾前就自己跑掉了,记 self_exit —— 那才是产品不合格的那一类。
    产品没到过就绪就没了(起不来),一律不记死因:那是平台错误(命令 / 参数 / 权限)。
    """
    if product is None:
        return None
    was_running = product.poll() is None
    code = _stop_product(product)
    _record_exit(record, product, ready=ready, platform_stopped=was_running, recorded=recorded)
    return code


def _record_exit(
    record: dict[str, Any],
    product: subprocess.Popen,
    *,
    ready: bool,
    platform_stopped: bool,
    recorded: set[subprocess.Popen],
) -> None:
    """记一条死因。同一个进程只记一次(监控看到一次、收尾又看到一次)。

    起不来的产品没有死因(平台错误):exit_event 返回 None,这里就不记。
    """
    if product in recorded:
        return
    recorded.add(product)
    event = exit_event(
        product, ready=ready, platform_stopped=platform_stopped, at=_stamp()
    )
    if event is not None:
        _record_event(record, event)


def _record_event(record: dict[str, Any], event: Mapping[str, Any]) -> None:
    record.setdefault(PROCESS_EVENTS, []).append(dict(event))


def _note_unexecuted(record: dict[str, Any], directory: Path) -> None:
    """被平台叫停后没跑到的用例:数出来,单独一条,与产品死因不合并。

    只有 run-scope.json 说得清"哪些没跑":collected 里有、executed / skipped / deselected 里都没有。
    没被叫停就不记 —— count=0 的一条只会让读的人以为出过事。
    """
    reason = stop_reason(directory)
    if reason is None:
        return
    try:
        scope = json.loads((directory / "run-scope.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # 会话没跑到写范围就没了:未执行几条无从得知,不拿缺文件冒充 0
    event = unexecuted_event(scope, reason)
    if event is not None:
        _record_event(record, event)


def _fill_runtime(record: dict[str, Any], directory: Path, started: float) -> None:
    """启动耗时 / 首个读数耗时 / 总耗时:报告要能回答"这一轮的时间花在哪"。

    首个读数从证据的那一刻算起(每设备 JSONL 里最早的 reading) —— 平台自己读的,
    产品读的不算:那是产品的速度,不是平台的。
    """
    readiness = record.get("readiness") or {}
    record["runtime"]["startup_seconds"] = readiness.get("seconds")
    record["runtime"]["total_seconds"] = round(time.monotonic() - started, 3)
    first = _first_reading_at(directory)
    if first is not None:
        began = datetime.fromisoformat(record["started_at"])
        record["runtime"]["first_reading_seconds"] = round((first - began).total_seconds(), 3)


def _first_reading_at(directory: Path) -> datetime | None:
    stamps = []
    for path in directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == "reading" and entry.get("at"):
                stamps.append(parse_at(entry["at"]))
    return min(stamps) if stamps else None


def _environment(launch: list[str], platform: paths.PlatformLayout) -> dict[str, Any]:
    """这一轮的复现四件套:平台代码的 commit、平台版本、产品二进制摘要、模拟器版本。

    模拟器没有版本号,内容摘要就是它的版本 —— 改了模拟器而没改版本号时,这里能看出来。

    commit 的来源是显式的(见 config/paths.py::commit):TESTPLATFORM_COMMIT > git > `unknown`。
    没有 `.git` 的目录里(安装形态 / git archive 抽出的树)不会静默变 None —— 报告里会看到
    `unknown` 四个字母,那本身就是"这个值没有来源"这条事实。
    platform_version 同口径(见 config/paths.py::platform_version):
    TESTPLATFORM_VERSION > 已安装发行版 > `unknown`,同样不靠 git。

    通则:**任何"来源可选"的证据字段都不许 null** —— 能算就给值,不能算给 `unknown`,
    字段本身永不缺席。`null` 只许表示"平台没这个字段",不许兼作"平台不知道"。
    """
    return {
        "commit": paths.commit(root=platform.root),
        "platform_version": paths.platform_version(),
        # 票 15a:eject 之后平台可改,结论的可靠性取决于改了什么 —— 来源与指纹一起记
        "platform_source": origin.source_of(platform.root),
        "platform_sha256": origin.sha256_of(platform.root),
        "product_sha256": _product_digest(_product_file(launch, platform.root)),
        "simulator_version": _sha256(platform.slave_script),
    }


def _product_digest(script: Path | None) -> str:
    """产品摘要三态:文件型产品 → sha256;命令行里没有文件 → `unknown`;永不 null。

    没有文件的是 `python -c ...` 与 PATH 上的命令型产品:那是"平台不知道这个产品的内容
    是哪一份",不是"没这个字段"。两者在证据里必须分得开 —— 否则同一个字段会因为
    "产品怎么写"忽有忽无,读报告的人只会当成装坏了。
    """
    return _sha256(script) or paths.UNKNOWN


def _product_file(launch: list[str], root: Path) -> Path | None:
    """产品二进制 / 脚本:命令行里第一个真的存在的文件(解释器本身不算)。"""
    interpreter = Path(sys.executable)
    for argument in launch:
        path = Path(argument)
        if not path.is_absolute():
            path = root / path
        if path.is_file() and path != interpreter:
            return path
    return None


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp() -> str:
    """与证据层同一个时间口径(UTC、毫秒、带偏移):两份记录并排时比得出先后。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_record(directory: Path, record: Mapping[str, Any]) -> Path:
    path = directory / TASK_RECORD_FILE
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
