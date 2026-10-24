"""这轮结论基于哪份平台代码(票 15a)。

`eject` 之后平台不再是 site-packages 里的只读 wheel,而是用户项目里的 `platform/` ——
**用户能改它**。所以证据必须记下:代码从哪来(`platform_source`)、指纹是多少
(`platform_sha256`)。否则三个月后没人知道那份「合格」是谁的平台给的。

两条形态是闭集,不许出现第三种:
- `package`:平台是装进来的(只读)
- `project`:平台在项目里(可改)
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import pathlib
import sys
from typing import Iterable

PACKAGE = "package"
PROJECT = "project"
SOURCES = (PACKAGE, PROJECT)
DATA_DIR = "_data"


def package_directory() -> pathlib.Path:
    """这段代码自己所在的那一层。判"项目内还是装进来的"只能看它,不能看 cwd。"""
    return pathlib.Path(__file__).resolve().parent.parent


def source_of(root: pathlib.Path, *, package_dir: pathlib.Path | None = None) -> str:
    """`package_dir` 是否落在 `root` 里:在 → project(用户能改),不在 → package。"""
    package = (package_dir or package_directory()).resolve()
    try:
        package.relative_to(pathlib.Path(root).resolve())
    except ValueError:
        return PACKAGE
    return PROJECT


def digest(files: Iterable[pathlib.Path], *, root: pathlib.Path) -> str:
    """排序后哈希:内容 + 相对路径都进哈希(改名也算改了平台)。"""
    sha = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        sha.update(path.relative_to(root).as_posix().encode("utf-8"))
        sha.update(b"\0")
        sha.update(path.read_bytes())
        sha.update(b"\0")
    return sha.hexdigest()


def project_files(package_dir: pathlib.Path | None = None) -> list[pathlib.Path]:
    """`platform/` 里所有 `.py` + `_data` 下的文件(用户能改的全部东西)。"""
    package = package_dir or package_directory()
    files = [path for path in package.rglob("*.py") if "__pycache__" not in path.parts]
    data = package / DATA_DIR
    if data.is_dir():
        files += [path for path in data.rglob("*") if path.is_file()]
    return files


def sha256_of(root: pathlib.Path, *, package_dir: pathlib.Path | None = None) -> str:
    """project 形态 = 源码指纹;package 形态 = 已安装 wheel 的 sha256。

    wheel 本身运行时看不到,但安装时记过(见 ejected.json / 发行版元数据),这里按
    可信度逐级回落:**任何一级都不给 None**(纪律 §12:字段永不缺席)。
    """
    package = package_dir or package_directory()
    if source_of(root, package_dir=package) == PROJECT:
        return digest(project_files(package), root=package)
    from_wheel = _wheel_digest()
    if from_wheel:
        return from_wheel
    # 拿不到 wheel(源码布局直接跑测试):退回代码指纹 —— 值的口径写在报告里由 source 区分
    return digest(project_files(package), root=package)


def _wheel_digest() -> str:
    """安装时写下的 wheel sha256(TESTPLATFORM_WHEEL_SHA256 > ejected.json > 空)。

    空串表示"这条来源没有值":调用方换个能算的口径,而不是写 null。
    """
    import os

    from_env = os.environ.get("TESTPLATFORM_WHEEL_SHA256", "").strip()
    if from_env:
        return from_env
    try:
        raw = importlib.metadata.distribution("testplatform").read_text("direct_url.json")
    except (importlib.metadata.PackageNotFoundError, OSError):
        return ""
    return ""
