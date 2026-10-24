"""把平台源码落进用户项目(票 15a):wheel 只读是设计缺口,用户要能改平台。

落下去之后平台就归用户了,所以每一步都要能被追溯:
- 复制 → `<project>/<dir>/testplatform` + `_data` + `tools/`
- `pip install -e`(先卸掉 wheel 版,否则 import 的还是 site-packages 里那份)
- 写 `.testplatform/ejected.json {fromVersion, at, sha256}` —— 供排查"我这套是从哪个版本来"
- **不往 `.gitignore` 里写**:用户的改动要能提交
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from testplatform import DISTRIBUTION_NAME, PACKAGE_NAME
from testplatform.config import origin, paths

EJECTED_FILE = "ejected.json"
STATE_DIR = ".testplatform"
DEFAULT_DIR = "platform"


def eject(project_root: pathlib.Path, *, into: str = DEFAULT_DIR, install: bool = True) -> dict:
    """复制 → 可编辑安装 → 记 ejected.json。返回那份记录(同时写进证据可用的形状)。"""
    root = pathlib.Path(project_root).resolve()
    target = (root / into).resolve()
    package = origin.package_directory()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} 已经有东西了:eject 不覆盖用户的改动")
    target.mkdir(parents=True)
    shutil.copytree(package, target / "testplatform", ignore=shutil.ignore_patterns("__pycache__"))
    tools = package.parent.parent / "tools"
    if tools.is_dir():
        shutil.copytree(tools, target / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    (target / "pyproject.toml").write_text(_pyproject(), encoding="utf-8")

    if install:
        _editable_install(root, target)
    record = {
        "fromVersion": paths.platform_version(),
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        # 指纹覆盖 platform/ 里所有 .py + _data:改了哪一行,这个值就变
        "sha256": origin.digest(origin.project_files(target / "testplatform"), root=target / "testplatform"),
    }
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    (state / EJECTED_FILE).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


_PYPROJECT = """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "testplatform"
version = "{version}"
requires-python = ">=3.11"

[tool.setuptools]
packages = ["testplatform"]
include-package-data = true

[tool.setuptools.package-data]
testplatform = ["_data/**/*"]
"""


def _pyproject() -> str:
    version = paths.platform_version()
    return _PYPROJECT.format(version=version if version != "unknown" else "0.0.0")


def _venv_python(root: pathlib.Path) -> pathlib.Path:
    """项目自己的 venv;没有就用当前解释器(至少别去动别人的环境)。"""
    venv = root / STATE_DIR / "venv"
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.exists():
            return candidate
    return pathlib.Path(sys.executable)


def import_path(python: pathlib.Path) -> pathlib.Path:
    """这份解释器 import 到的是哪一份 testplatform —— 唯一可信的判据。"""
    done = subprocess.run(
        [str(python), "-c", "import testplatform; print(testplatform.__file__)"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f"{python} 里 import 不到 testplatform: {done.stderr}")
    return pathlib.Path(done.stdout.strip())


def assert_imports_from_project(root: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    """import 必须落在 `<项目>/<dir>/` 里,否则 eject 就是**假溯源**(票 15a 打回)。

    装了两个发行版时(site-packages 里的 wheel 残留 + editable),site-packages 排在前面,
    用户改的 `platform/` 永远轮不到 —— 报告却会写 `platform_source: project`。
    所以这一条不是警告,是失败。
    """
    found = import_path(_venv_python(root))
    if not found.is_relative_to(target):
        raise RuntimeError(
            f"eject 之后 import 到的还是 {found} —— 不在 {target} 里。"
            "用户的改动不会生效,报告却会说 platform_source: project(假溯源)。"
        )
    return found


def _editable_install(root: pathlib.Path, target: pathlib.Path) -> None:
    """先卸干净再 `-e`,最后**验证 import 落在项目里**。

    踩过的坑:wheel 的发行版名是 `cautonwong-testplatform`,editable 叫 `testplatform` ——
    只卸后者时 wheel 的 `testplatform/` 目录原地不动,site-packages 先被 import,
    于是"能改"是假的。所以:按**真实发行版名**卸、清残留、装、再断言。
    """
    python = _venv_python(root)
    for name in (DISTRIBUTION_NAME, PACKAGE_NAME):
        done = subprocess.run(
            [str(python), "-m", "pip", "uninstall", "-y", name], capture_output=True, text=True
        )
        if done.returncode != 0:
            raise RuntimeError(f"pip uninstall {name} 失败: {done.stdout} {done.stderr}")
    # 卸不掉的残留(比如发行版名变了):留着它,import 就到不了项目里 —— 必须有主。
    residue = _site_packages(python) / PACKAGE_NAME
    if residue.is_dir():
        shutil.rmtree(residue)
    done = subprocess.run(
        [str(python), "-m", "pip", "install", "-e", str(target)], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise RuntimeError(f"pip install -e {target} 失败: {done.stdout} {done.stderr}")
    assert_imports_from_project(root, target)


def _site_packages(python: pathlib.Path) -> pathlib.Path:
    done = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f"问不到 {python} 的 site-packages: {done.stderr}")
    return pathlib.Path(done.stdout.strip())
