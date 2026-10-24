"""
票 09d:根的三种来源(显式 `--root` / `TESTPLATFORM_ROOT` / 当前目录)**同一条判据**,逐样回落。

老口径只有"当前目录"那一支有包内回落,显式根必须四样齐全 —— 于是同一个目录不传 `--root` 跑得通、
传了就 exit 4,而 CI 每个 job 都传 `--root`。这里守三件事:

1. 混排来源如实记账(哪一样来自项目、哪一样来自包),不是笼统"能用";
2. 用例永不参与回落(它是 `project`,不可能是 `package`);
3. 两边都没有才报错,且报出**两侧**各自的期望路径。

真任务在临时根上按混排形状跑通的那条在 tests_platform/test_installed_layout.py。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from testplatform.config import paths
from testplatform.config.paths import ROOT_ENV, PlatformRootError, layout, resolve_root

REPO = Path(__file__).resolve().parents[1]


def _project(base: Path, *items: str) -> Path:
    """一个只带 `items` 这几样的项目目录,其余资源靠包内。"""
    root = base / "project"
    root.mkdir(parents=True)
    for item in items:
        target = root / item
        if item.endswith(".py"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / item, target)
        else:
            target.mkdir(parents=True)
    return root


# --------------------------------------------------------- 逐样回落 ----

def test_each_resource_comes_from_where_it_really_is(tmp_path):
    """混排:项目目录只带模拟从站脚本 —— 这一样记 project,其余几样记 package。"""
    project = _project(tmp_path, "tools/mock_modbus_meter.py")

    resolved = layout(explicit=project)

    assert resolved.sources() == {
        "slave_script": "project",
        "pytest_ini": "package",
        "registers": "package",
        "clauses": "package",
        "sample_product": "package",
        "cases": "project",
    }
    assert resolved.slave_script == project / "tools" / "mock_modbus_meter.py"
    assert resolved.pytest_ini == paths.PACKAGE_DATA / "pyproject.toml"


def test_the_environment_variable_judges_exactly_like_the_explicit_root(tmp_path, monkeypatch):
    """同一份目录,显式参数与 `TESTPLATFORM_ROOT` 得到同一个布局 —— 差别只在起点从哪来。"""
    project = _project(tmp_path, "tools/mock_modbus_meter.py")

    explicit = layout(explicit=project)
    monkeypatch.setenv(ROOT_ENV, str(project))
    from_environment = layout()

    assert from_environment.root == explicit.root == project
    assert from_environment.sources() == explicit.sources()


def test_a_project_with_only_configs_loads_instead_of_demanding_pyproject(tmp_path):
    """缺陷形状:/tmp/proj09 只有 configs、没有 src/tools/pyproject。这里必须加载成功,不是"缺 pyproject.toml"。"""
    project = _project(tmp_path, "configs")
    (project / "configs" / "devices_probe.yaml").write_text("devices: []\n", encoding="utf-8")

    resolved = layout(explicit=project)

    assert resolved.root == project
    assert resolved.sources()["pytest_ini"] == "package", "有 src 要求时不许退化成'显式根必须自带 pyproject'"


# --------------------------------------------------- 两边都没有才报 ----

# ("两边都没有 → 加载期报错、消息含两侧路径"那条在 test_platform_paths.py:
#  `test_a_resource_missing_on_both_sides_lists_the_paths_each_side_expects`,一份事实只守一处。)


# ----------------------------------------------------- 用例不回落 ----

def test_cases_is_never_sourced_from_the_package(tmp_path, monkeypatch):
    """用例不随包:哪怕包内_resource 目录里塞了用例,来源也只可能是 project。"""
    package = tmp_path / "package" / "_data"
    shutil.copytree(paths.PACKAGE_DATA, package)
    (package / "cases").mkdir()
    (package / "cases" / "test_decoy.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "PACKAGE_DATA", package)

    sources = paths.PlatformLayout(_project(tmp_path, "configs")).sources()

    assert sources["cases"] == paths.FROM_PROJECT == "project"
    assert "cases" not in paths.PACKAGE_RESOURCES, "用例一旦进了包内资源表,回落就会把示例用例当成项目用例跑"


def test_an_explicit_root_still_needs_the_marker_nobody_ships(tmp_path):
    """回落只补"包内真有"的那几样:`required` 里有项目专有的东西时,显式根照样要自己带。"""
    project = _project(tmp_path, "configs")

    with pytest.raises(PlatformRootError) as error:
        resolve_root(explicit=project, required=("configs/devices.yaml",))

    assert "configs/devices.yaml" in str(error.value), str(error.value)
