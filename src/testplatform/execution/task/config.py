"""任务文件的加载与校验。运行在 runtime.py。"""
from __future__ import annotations

import hashlib
import importlib.util
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


_IMPORTED_PROJECT_CONFTESTS: set[str] = set()


def _import_project_plugins(root: Path) -> None:
    """项目根目录的 conftest.py 是驱动/协议注册点。

    任务加载在父进程里解析清单,子 pytest 还没起来;不先导入它,项目里新注册的规约
    会被判成"未知规约"。导入失败直接报 TaskFileError,不把半份清单交给装配。

    一个进程里只执行一次:``testplatform tasks`` 会连续加载多个任务,而 conftest
    可能用 ``@register_device`` 这种不幂等的装饰器,重复执行会被当成重复注册。
    """
    conftest = root / "conftest.py"
    if not conftest.is_file():
        return
    key = str(conftest.resolve())
    if key in _IMPORTED_PROJECT_CONFTESTS:
        return
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    name = "_testplatform_project_conftest_" + hashlib.sha1(key.encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, conftest)
    if spec is None or spec.loader is None:
        raise TaskFileError(f"项目的 conftest.py 读不了: {conftest}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise TaskFileError(
            f"项目的 conftest.py 导入失败(驱动/协议要在装配前注册): {conftest}: {exc}"
        ) from exc
    _IMPORTED_PROJECT_CONFTESTS.add(key)


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
    _import_project_plugins(platform.root)
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
