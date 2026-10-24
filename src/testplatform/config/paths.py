"""
平台根与包内资源的唯一解析点。

为什么要有这个模块:执行器(`execution/task.py`)与条款清单(`clauses.py`)都需要知道
"根在哪、模拟从站脚本在哪、pyproject.toml 在哪"。以前两处各写一遍
"按源码树相对层级数几级"(从本文件往上数 N 层),于是同一件事有两个来源,而且**安装形态下一半失效**:
按包分发后,`src/` 布局不在了,数出来的那一层是 site-packages 的上级,四处路径全不存在。

资源有两种来源:**项目目录**(跑任务的人放在一起的用例、清单、配置)与**包内 `_data/`**
(模拟从站脚本、寄存器表、条款清单、子 pytest 的 ini —— 它们是执行器的一部分,不是仓库示例)。
每一样都**项目目录优先**,包内兜底;谁提供哪一样必须进证据(`PlatformLayout.sources`),
否则"装完能用"和"根里四样齐"在 task.json 里长得一模一样。

一条规则,三个优先级(冲突时显式赢):

    显式参数  >  环境变量 TESTPLATFORM_ROOT  >  当前目录

三支**同一条判据**,区别只在"起点从哪来"(报错时指认用):某一样资源项目目录有就用项目的,
项目目录没有但包内有就用包内的,两边都没有才报错。别只在"当前目录"那一支回落 ——
CI 每个 job 都传 `--root ${{ github.workspace }}`,那样一个没有 `src/` / `pyproject.toml` 的项目
会因为换了一种指定方式而突然起不来。
资源真的两边都没有时**一次说清**:缺哪几样、两侧期望路径、怎么补 —— 而不是让执行器在子进程里
以 `exit 4` 或 ImportError 收场。
用例不在这条链上:它只来自项目目录,不随包,也不回落到包里找(见 `PlatformLayout.sources`)。

`env.commit` / `env.platform_version` 的来源同理不能靠"目录里有没有 .git"决定:
显式 > 环境变量 > 真来源(git / 已安装发行版)> 字面量 `unknown`。`unknown` 必须出现在证据里 ——
缺值可以说,静默少一个字段不行。
"""
from __future__ import annotations

from importlib.resources import files as importlib_resources_files

import importlib.metadata
import os
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from testplatform import DISTRIBUTION_NAME

ROOT_ENV = "TESTPLATFORM_ROOT"
COMMIT_ENV = "TESTPLATFORM_COMMIT"
VERSION_ENV = "TESTPLATFORM_VERSION"
UNKNOWN = "unknown"

# 插件:包内的子模块(`src/testplatform/plugin.py`),随包安装。pytest 认它两处 ——
# `-p testplatform.plugin` 与 pyproject 里 pytest11 那条 entry point(名字与模块名不必相同,
# 认的是模块对象:`entry.load()` 出来的是不是这个模块)。
PLUGIN_MODULE = "testplatform.plugin"
PLUGIN_ENTRY_POINT = "testplatform"
PLUGIN_ENTRY_POINT_GROUP = "pytest11"

# 执行器真正要用到的四样东西。三支来源拿它们**逐样**校验:项目目录有就用项目的,
# 没有就用包内的(`PACKAGE_REQUIRED` / 包自身),两边都没有才报错。
EXECUTOR_REQUIRES = (
    "pyproject.toml",
    "src",
    "tools/mock_modbus_meter.py",
    "configs/mock_registers.yaml",
)

# 随包走的那份资源目录:wheel 里是 `testplatform/_data/`。
# 用 `importlib.resources` 定位 —— 它是"包内资源"的标准入口,不依赖本文件在源码树里的层级
# (装成包以后源码树的层级根本不存在)。
PACKAGE_DATA = Path(str(importlib_resources_files("testplatform"))) / "_data"

# 示例产品(`configs/tasks/` 里那几条示例任务的产品):装完的用户目录里没有 `tests_support/`,
# 所以它必须随包走,任务里写的相对路径由 `resource()` 回落到包内那份(票 09e)。
# 只有引用它的任务才要这一样 —— 所以它记来源,但不进 `PACKAGE_REQUIRED`:
# 真产品任务不该因为这一样装坏了起不来;引用它的任务会在加载期报「包内缺」。
SAMPLE_PRODUCT = "tests_support/fake_product.py"

# 包里提供、且要记来源的资源 → task.json 里 `layout` 的键。
PACKAGE_RESOURCES = {
    "pytest_ini": "pyproject.toml",
    "slave_script": "tools/mock_modbus_meter.py",
    "registers": "configs/mock_registers.yaml",
    "clauses": "configs/clauses.yaml",
    "sample_product": SAMPLE_PRODUCT,
}

# 包里必须齐的东西:**每个任务都要的**那些 + 台架量名表(装配设置类条款时才用得上,所以不进 `layout`)。
PACKAGE_REQUIRED = (
    *(relative for relative in PACKAGE_RESOURCES.values() if relative != SAMPLE_PRODUCT),
    "configs/import/params.yaml",
)

FROM_PROJECT = "project"
FROM_PACKAGE = "package"
# 两边都没有:这一样说不出来源。`layout()` 在此之前已经把它报成"平台资源不全",
# 但来源字段不许拿 package 冒充 —— 装坏了与装好了在证据里必须分得开。
FROM_UNKNOWN = "unknown"


class PlatformRootError(ValueError):
    """根目录找不到 / 不完整。消息里必须有:缺什么、期望在哪、怎么指定。"""


@dataclass(frozen=True)
class PlatformLayout:
    """一个可用的项目根,以及每样资源的实际位置与来源。构造完用 `missing()` 校验一遍。"""

    root: Path

    @property
    def slave_script(self) -> Path:
        return self._resource("slave_script")

    @property
    def registers(self) -> Path:
        return self._resource("registers")

    @property
    def pytest_ini(self) -> Path:
        return self._resource("pytest_ini")

    @property
    def clauses(self) -> Path:
        return self._resource("clauses")

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    def sources(self) -> dict[str, str]:
        """**原始视图**:每一样资源各从哪来,取值 `package` / `project` / `unknown`。

        `unknown` 只出现在这里:它说的是"解析不出来",不是谎 —— 两边都没有就照实说,
        不许拿 `package` 冒充。能拿来跑任务的那一版是 `layout()`,不是这个视图。

        用例固定是 `project`:用例不随包,挪走了就是没有(见 `execution/task.py` 的
        "项目目录没有用例"),不许回落到包里找 —— 所以它不可能是 `package`。

        未来若有第三来源(如用户覆盖目录),在 `_source` 里加值并补用例。
        """
        return {key: self._source(key) for key in PACKAGE_RESOURCES} | {"cases": FROM_PROJECT}

    def missing(self) -> list[str]:
        """项目目录与包内都没有的资源(相对路径)。空 = 这个布局能用。

        `sources()` 把这类记成 `unknown`;`layout()` 拿它当加载期错误 —— 同一份事实,
        原始视图允许说"解析不出来",可用视图不许把它交出去。
        """
        return [relative for relative in PACKAGE_REQUIRED if not resource(relative, root=self.root).is_file()]

    def _resource(self, key: str) -> Path:
        return resource(PACKAGE_RESOURCES[key], root=self.root)

    def _source(self, key: str) -> str:
        relative = PACKAGE_RESOURCES[key]
        if (self.root / relative).is_file():
            return FROM_PROJECT
        if (PACKAGE_DATA / relative).is_file():
            return FROM_PACKAGE
        return FROM_UNKNOWN


def resolve_root(
    *,
    explicit: str | Path | None = None,
    required: Iterable[str] = EXECUTOR_REQUIRES,
    start: str | Path | None = None,
) -> Path:
    """项目根:显式 > 环境变量 > 当前目录。**三支同一条判据**:每一样资源逐样回落。

    项目目录有 → 项目的;项目目录没有但包内有 → 包内的;两边都没有 → `PlatformRootError`。
    三支的差别只在起点从哪来 —— 报错里说的是这件事,不是"要不要回落"。
    `start` 只是"调用发生在哪儿",用来在报错里指认。
    """
    required = tuple(required)
    if explicit is not None:
        root = Path(explicit)
    else:
        from_env = os.environ.get(ROOT_ENV)
        root = Path(from_env) if from_env else Path.cwd()
    root = root.expanduser().resolve()
    missing = [relative for relative in required if not _available(relative, root)]
    if missing:
        raise PlatformRootError(
            _missing_resources_message(missing, root, start, _origin(explicit))
        )
    return root


def layout(*, explicit: str | Path | None = None) -> PlatformLayout:
    """**可用视图**:执行器用的布局 —— 项目根 + 每样资源的位置与来源,四样齐才返回。

    与 `PlatformLayout.sources()`(原始视图)是两个职责:`sources()` 允许 `unknown`,
    这里**永不产出 `unknown`** —— 缺任何一样就抛 `PlatformRootError`(缺哪几样、包内路径、
    怎么补),把问题留在加载期,而不是交给执行器在子进程里 exit 4。

    未来若有第三来源(如用户覆盖目录),在 `_source` 里加值并补用例。
    """
    resolved = PlatformLayout(resolve_root(explicit=explicit))
    missing = resolved.missing()
    if missing:
        raise PlatformRootError(
            _missing_resources_message(missing, resolved.root, where=_origin(explicit))
        )
    return resolved


def sys_path(*, root: str | Path | None = None) -> list[str]:
    """sys.path / PYTHONPATH 的**唯一**组装点:代码路径在前,项目目录在后。

    代码路径 = 本包所在的那一层(`src/` 或 site-packages):先命中这一棵树的代码,免得别的
    worktree 的可编辑安装抢在前面。项目目录 = 资源回落的同一个起点(`resource`)—— 项目自带的
    辅助模块与用例都在它下面,子 pytest 也得看得到。两件事从同一个根出发,不许各写一套。
    """
    entries: list[str] = [_code_path()]
    if root is not None:
        entries.append(str(Path(root).resolve()))
    return entries


def _code_path() -> str:
    """平台包所在的那一层(源码布局里的 `src/`,装成包后的 site-packages)。

    用 `importlib.resources` 问"包在哪",不数本文件的层级:同一个值在两种布局下都对。
    **只加这一层**(包的父目录),不额外塞别的东西(票 11c)。
    """
    return str(Path(str(importlib_resources_files("testplatform"))).parent)


def resource(relative: str, *, root: str | Path | None = None) -> Path:
    """一件资源:项目目录里有就用项目目录的,没有就用包内的(项目目录优先)。"""
    base = Path(root).resolve() if root is not None else Path.cwd()
    in_project = base / relative
    return in_project if in_project.is_file() else PACKAGE_DATA / relative


def clauses_path(*, explicit: str | Path | None = None) -> Path:
    """条款清单的缺省位置 —— **只有这一处**知道它在哪。

    显式 > 项目目录里的 `configs/clauses.yaml` > 包内那份。两条路都没有就一次报清。
    """
    if explicit is not None:
        return Path(explicit)
    found = resource("configs/clauses.yaml")
    if not found.is_file():
        raise PlatformRootError(_missing_resources_message(["configs/clauses.yaml"], Path.cwd()))
    return found


def commit(*, root: str | Path, explicit: str | Path | None = None) -> str:
    """这一轮的代码版本:显式 > TESTPLATFORM_COMMIT > git rev-parse > `unknown`。

    **不许返回 None**:证据字段缺值可以说(`unknown`),静默少一个字段不行 ——
    报告里 `unknown` 四个字母本身就是"这个值没有来源"这条事实。
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    from_env = os.environ.get(COMMIT_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        finished = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN
    if finished.returncode != 0:
        return UNKNOWN
    return finished.stdout.strip() or UNKNOWN


def platform_version(*, explicit: str | Path | None = None) -> str:
    """这一轮的平台版本:显式 > TESTPLATFORM_VERSION > 已安装发行版的版本 > `unknown`。

    与 `commit` 同口径,**不许 None、不许空串**:源码布局(没装成包)写 `unknown`,
    安装形态读发行版元数据 —— 两条路都不碰 `.git`,安装目录没有 git 也不影响。
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    from_env = os.environ.get(VERSION_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN


def plugin_from_entry_point() -> bool:
    """pytest 会不会自己把平台插件装上:entry point 存在**且能 resolve 到插件模块**。

    判据是"能不能 resolve",不是"这个发行版装没装" —— 装了一半、entry point 指到别的
    模块的环境里,`-p testplatform.plugin` 仍然必须传,否则整轮没有插件;
    反过来,entry point 真能 resolve 时再传一次就是同一个模块两个名字,
    pytest 直接 `Plugin already registered` 中止。两处调用点(conftest / `_run_pytest`)都按它分叉。
    """
    try:
        entries = importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except Exception:  # 元数据层坏掉不该让整轮起不来:当作没有 entry point,照旧传 -p
        return False
    for entry in entries:
        if entry.name != PLUGIN_ENTRY_POINT:
            continue
        try:
            module = entry.load()
        except Exception:
            continue
        if getattr(module, "__name__", None) == PLUGIN_MODULE:
            return True
    return False


# ---------------------------------------------------------------- 内部 ----

def _package_provides(relative: str) -> bool:
    """包提供这一样吗:`src` 由包自身提供(装上了就 import 得到 testplatform),其余看 `_data/`。"""
    return relative == "src" or (PACKAGE_DATA / relative).is_file()


def _available(relative: str, root: Path) -> bool:
    """这一样拿得到吗:项目目录里有,或者包里提供。"""
    return (root / relative).exists() or _package_provides(relative)


def _origin(explicit: str | Path | None) -> str | None:
    """根从哪来(报错时指认用):显式 > 环境变量 > 当前目录 —— 与真正取根是同一条顺序。"""
    if explicit is not None:
        return "显式参数 --root / explicit"
    if os.environ.get(ROOT_ENV):
        return f"环境变量 {ROOT_ENV}"
    return None


def _missing_resources_message(
    missing: Iterable[str],
    project: Path,
    start: str | Path | None = None,
    where: str | None = None,
) -> str:
    """缺项一次说清:两侧各自的期望路径 + 怎么指定根。`where` 是根从哪来的(显式 / 环境变量)。"""
    missing = list(missing)
    listed = "".join(
        f"\n  - {relative}\n"
        f"      项目目录: {project / relative}\n"
        f"      包内:     {PACKAGE_DATA / relative}"
        for relative in missing
    )
    origin = "" if where is None else f",根来自 {where}"
    at = "" if start is None else f"(从 {start} 调的)"
    return (
        f"平台资源不全,缺 {len(missing)} 样{origin}{at}:{listed}\n"
        f"  项目目录: {project}(在项目目录里放同名文件也行,项目目录优先)\n"
        f"  包内资源目录: {PACKAGE_DATA}(wheel 里是 site-packages/testplatform/_data/);"
        f"两边都没有就是装坏了或缺配置,重装平台包\n"
        f"  也可以 --root <目录> / {ROOT_ENV}=<目录> 指定一个含这些文件的项目目录。"
    )
