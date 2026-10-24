"""
平台根解析(票 01b):显式 > TESTPLATFORM_ROOT > 探测;探测失败一次说清;`env.commit` 不许静默 None。

这一层是纯函数缝:三类路径来源、三种失败消息、commit 的四种取值,不需要起产品也不需要真设备。
真任务在临时根上跑通的那条在 tests_platform/test_task_root.py。
"""
from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

import pytest

from testplatform.config import paths
from testplatform.config.paths import (
    COMMIT_ENV,
    ROOT_ENV,
    UNKNOWN,
    PlatformRootError,
    commit,
    layout,
    resolve_root,
)

REPO = Path(__file__).resolve().parents[1]


def _root_with_markers(base: Path) -> Path:
    """造一个"看着像平台根"的目录:四个标记齐全。"""
    (base / "src").mkdir(parents=True)
    (base / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (base / "tools").mkdir()
    (base / "tools" / "mock_modbus_meter.py").write_text("", encoding="utf-8")
    (base / "configs").mkdir()
    (base / "configs" / "mock_registers.yaml").write_text("{}\n", encoding="utf-8")
    return base


# --------------------------------------------------------------- 优先级 ----

def test_detection_finds_the_source_tree_by_default(monkeypatch):
    """没有显式参数、没有环境变量:从本包位置往上探测,得到仓库根。"""
    monkeypatch.delenv(ROOT_ENV, raising=False)

    assert resolve_root() == REPO


def test_the_environment_variable_wins_over_detection(tmp_path, monkeypatch):
    """TESTPLATFORM_ROOT 指到别处时,探测让位 —— 安装形态下这才是常态。"""
    elsewhere = _root_with_markers(tmp_path / "elsewhere")
    monkeypatch.setenv(ROOT_ENV, str(elsewhere))

    assert resolve_root() == elsewhere.resolve()


def test_an_explicit_root_wins_over_the_environment_variable(tmp_path, monkeypatch):
    from_env = _root_with_markers(tmp_path / "from_env")
    explicit = _root_with_markers(tmp_path / "explicit")
    monkeypatch.setenv(ROOT_ENV, str(from_env))

    assert resolve_root(explicit=explicit) == explicit.resolve()
    assert resolve_root() == from_env.resolve()


def test_the_layout_derives_every_resource_from_the_same_root(tmp_path):
    root = _root_with_markers(tmp_path / "root")

    resolved = layout(explicit=root)

    assert resolved.root == root.resolve()
    assert resolved.slave_script == root.resolve() / "tools" / "mock_modbus_meter.py"
    assert resolved.registers == root.resolve() / "configs" / "mock_registers.yaml"
    assert resolved.pytest_ini == root.resolve() / "pyproject.toml"
    assert resolved.evidence == root.resolve() / "evidence"


def test_the_sys_path_starts_with_this_tree_and_ends_with_the_project(tmp_path):
    """sys.path / PYTHONPATH 一份规则:代码路径在前(本树的 src),项目目录在后。"""
    root = _root_with_markers(tmp_path / "root")

    entries = paths.sys_path(root=root)

    assert entries[0] == str(REPO / "src"), "先命中本树的代码,别让别的 worktree 的安装抢在前面"
    assert entries[-1] == str(root.resolve()), "项目目录在后:项目自带的辅助模块要 import 得到"
    assert paths.sys_path() == [str(REPO / "src")], "不给项目目录时只有代码路径这一项"


def test_the_sys_path_assembly_is_defined_in_exactly_one_place():
    """结构断言(不是行为断言):组装 sys.path 的只有一处 —— 资源回落与代码路径不许各写一套。"""
    definitions = [
        path.relative_to(REPO).as_posix()
        for path in sorted((REPO / "src").rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "sys_path"
    ]

    assert definitions == ["src/testplatform/config/paths.py"], definitions


# ------------------------------------------------------------- 失败消息 ----

def test_detection_failure_says_what_is_missing_and_how_to_point_at_it(tmp_path):
    """探测失败必须一处报清:缺什么、往哪找、怎么指定 —— 不是子进程 exit 4。"""
    with pytest.raises(PlatformRootError) as error:
        resolve_root(required=("no_such_marker_xyz",), start=tmp_path / "deep" / "start.py")

    message = str(error.value)
    assert "no_such_marker_xyz" in message
    assert "--root" in message and ROOT_ENV in message, f"没告诉怎么指定:{message}"


def test_a_resource_missing_on_both_sides_lists_the_paths_each_side_expects(tmp_path, monkeypatch):
    """包内少一样、项目目录也没有:报错要给出**两侧**各自的期望路径,并说清这个根从哪来。"""
    package = tmp_path / "package_data"
    shutil.copytree(paths.PACKAGE_DATA, package)
    (package / "configs" / "clauses.yaml").unlink()
    monkeypatch.setattr(paths, "PACKAGE_DATA", package)
    project = tmp_path / "project"
    (project / "configs").mkdir(parents=True)

    with pytest.raises(PlatformRootError) as error:
        layout(explicit=project)

    message = str(error.value)
    assert str(project / "configs" / "clauses.yaml") in message, f"没给项目目录侧的期望路径:{message}"
    assert str(package / "configs" / "clauses.yaml") in message, f"没给包内侧的期望路径:{message}"
    assert "显式" in message, f"没说这个根是从哪来的:{message}"
    assert "--root" in message and ROOT_ENV in message, f"没告诉怎么指定:{message}"


def test_an_installed_looking_root_is_accepted_and_says_package(tmp_path, monkeypatch):
    """只剩"装到 site-packages 的包目录"的形状也是合法根:包内资源逐样补上,来源如实记 package。"""
    installed = tmp_path / "site-packages"
    (installed / "testplatform").mkdir(parents=True)
    (installed / "testplatform" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV, str(installed))

    resolved = layout()

    assert resolved.root == installed.resolve()
    assert resolved.sources() == {
        "slave_script": "package",
        "pytest_ini": "package",
        "registers": "package",
        "clauses": "package",
        "sample_product": "package",
        "cases": "project",
    }


# ------------------------------------- sources() 原始视图 / layout() 可用视图 ----

def test_layout_never_reports_unknown(tmp_path, monkeypatch):
    """`sources()` 允许 unknown,`layout()` 不许:两边都没有就加载期报错,不把 unknown 交出去。"""
    root = _root_with_markers(tmp_path / "root")  # 四样齐,但没有 configs/clauses.yaml
    monkeypatch.chdir(root)
    monkeypatch.delenv(ROOT_ENV, raising=False)
    package = tmp_path / "package_data"
    shutil.copytree(paths.PACKAGE_DATA, package)
    (package / "configs" / "clauses.yaml").unlink()
    monkeypatch.setattr(paths, "PACKAGE_DATA", package)

    with pytest.raises(PlatformRootError) as error:
        layout()

    assert "configs/clauses.yaml" in str(error.value), str(error.value)
    # 同一份材料在原始视图里就是 unknown:两边都没有,照实说
    assert paths.PlatformLayout(root).sources()["clauses"] == UNKNOWN


# ------------------------------------------------------------ env.commit ----

def test_commit_uses_the_explicit_value_first(tmp_path, monkeypatch):
    monkeypatch.setenv(COMMIT_ENV, "envvalue")

    assert commit(root=tmp_path, explicit="abc1234") == "abc1234"


def test_commit_uses_the_environment_variable_second(tmp_path, monkeypatch):
    monkeypatch.setenv(COMMIT_ENV, "deadbee")

    assert commit(root=tmp_path) == "deadbee"


def test_commit_falls_back_to_git_in_a_real_checkout():
    """真 checkout 里 git 是可用来源 —— 这时不该出现 unknown。"""
    assert re.fullmatch(r"[0-9a-f]{7,40}", commit(root=REPO))


def test_commit_is_unknown_instead_of_none_without_git(tmp_path, monkeypatch):
    """没有 .git 的目录(安装形态 / git archive 抽出的树):`unknown`,不是 None、也不是空串。"""
    monkeypatch.delenv(COMMIT_ENV, raising=False)

    value = commit(root=tmp_path)

    assert value == UNKNOWN == "unknown"


def test_an_empty_commit_variable_is_not_a_value(tmp_path, monkeypatch):
    """空串等于没设:不许把 `commit: ""` 写进证据(报告里那会看起来像"有来源")。"""
    monkeypatch.setenv(COMMIT_ENV, "   ")
    (tmp_path / ".git").mkdir()

    assert commit(root=tmp_path) == UNKNOWN


# ------------------------------------------------------- env.platform_version ----

def test_platform_version_is_unknown_without_an_installed_distribution(monkeypatch):
    """源码布局(没装成包)取不到版本:`unknown`,不是 None、也不是空串 —— 与 commit 同口径。"""
    monkeypatch.delenv(paths.VERSION_ENV, raising=False)

    def _not_installed(_name):
        raise paths.importlib.metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(paths.importlib.metadata, "version", _not_installed)

    assert paths.platform_version() == UNKNOWN == "unknown"


def test_platform_version_prefers_explicit_then_the_environment_variable(monkeypatch):
    monkeypatch.setenv(paths.VERSION_ENV, "9.9.9")

    assert paths.platform_version(explicit="1.2.3") == "1.2.3"
    assert paths.platform_version() == "9.9.9"

    monkeypatch.setenv(paths.VERSION_ENV, "  ")
    assert paths.platform_version() == UNKNOWN, "空串等于没设"


# ---------------------------------------------- 源码树层级不许再出现(票 11c) ----

def test_no_module_derives_paths_from_the_source_tree():
    """`parents[` 在 src/ 里必须归零。

    01b 定的不变量:仓库根不许靠"本文件在源码树里的第几层"推出来 —— 装成包以后那几层
    根本不存在。11c 抓回三处(包内资源 / 子进程 PYTHONPATH / _data 里的从站脚本),
    这条 grep 就是防再漂的锁:任何一处改回 `parents[`,这里立刻红。
    """
    offenders = []
    for path in sorted((REPO / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "parents[" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")

    assert offenders == [], "源码树层级推路径又回来了(装成包就会失效):\n" + "\n".join(offenders)


def test_sys_path_is_the_package_parent_plus_the_project(tmp_path):
    """代码层 = **平台包的父目录**(问 importlib.resources,不数源码树层级);后面跟项目目录。"""
    from importlib.resources import files

    expected = str(Path(str(files("testplatform"))).parent)

    assert paths.sys_path(root=tmp_path)[0] == expected
    assert paths.sys_path(root=tmp_path)[-1] == str(tmp_path.resolve())
    assert paths.sys_path() == [expected]


def test_the_package_data_dir_is_found_through_importlib_resources():
    """包内资源用 importlib.resources 定位:_data 必须真的在包目录里(两种布局都成立)。"""
    from importlib.resources import files

    package = Path(str(files("testplatform")))

    assert paths.PACKAGE_DATA == package / "_data"
    assert paths.PACKAGE_DATA.is_dir()
