"""
票 09e:示例产品随包走 —— 装完的用户跑得了平台自带的示例任务。

缺陷形状:安装形态下项目目录里只有 `configs/`,任务里写的 `tests_support/fake_product.py`
按项目目录拼出来不存在 → 加载期 exit 4,而消息还指着他自己的项目。要做的是三件事,这里各有一条:
    ① 只挑 `fake_product.py` 一个文件进包(`tests_support/` 整个目录是测试夹具,不进发布物)
    ② 任务里的产品路径走资源的同一个解析入口(`configs/paths.py::resource`),不另拼一遍路径
    ③ 包内真缺这一样时,消息说的是**包内缺**(安装损坏),不是"你项目里没有"
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from testplatform.cases import case
from testplatform.config import paths
from testplatform.execution.task import TaskFileError, load_task

tomllib = pytest.importorskip("tomllib", reason="tomllib 是 3.11+ 的标准库")

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
SAMPLE_TASK = REPO / "configs" / "tasks" / "acceptance_sample.yaml"
DEVICES = REPO / "configs" / "devices.yaml"


def _installed_shaped_project(base: Path) -> Path:
    """装完的用户项目目录:只有 `configs/`,没有 `tests_support/`、没有 `src/`。"""
    project = base / "project"
    (project / "configs").mkdir(parents=True)
    shutil.copy(DEVICES, project / "configs" / "devices.yaml")
    shutil.copy(SAMPLE_TASK, project / "configs" / "acceptance_sample.yaml")
    return project


@case(id="T_00_00_06_000000_01", clause="free: 平台:分发", tags=["分发"])
def test_only_the_sample_product_ships_from_the_tests_support_directory():
    """`tests_support/` 整个目录是测试夹具:进包的只能是 `fake_product.py` 一个文件,且不许与源漂移。"""
    shipped = sorted(
        path.name
        for path in (paths.PACKAGE_DATA / "tests_support").iterdir()
        if not path.name.startswith("__")
    )

    assert shipped == ["fake_product.py"], f"包里多了测试夹具:{shipped}"
    assert (paths.PACKAGE_DATA / paths.SAMPLE_PRODUCT).read_bytes() == (REPO / paths.SAMPLE_PRODUCT).read_bytes(), (
        "包内副本与 tests_support/fake_product.py 不一致:改源之后要把副本同步过去(两份都在版本库里)"
    )


@case(id="T_00_00_06_000000_02", clause="free: 平台:分发", tags=["分发"])
def test_the_sample_product_is_declared_as_package_data():
    """`_data/` 里躺着不等于随包走:package-data 少了这一条,装完照样没有这一样。"""
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"
    ]["testplatform"]

    assert "_data/tests_support/*.py" in declared, declared


@case(id="T_00_00_06_000000_03", clause="free: 平台:分发", tags=["分发"])
def test_a_project_without_tests_support_takes_the_sample_product_from_the_package(tmp_path):
    """装完的用户目录里没有 `tests_support/` → 产品路径回落到包内那份,来源如实记 package。

    变异:把 `_existing_script` 改回 `root / written`(绕过解析入口)
    → 这条在加载期抛"不存在",红在这里。
    """
    project = _installed_shaped_project(tmp_path)
    assert not (project / "tests_support").exists()

    task = load_task(project / "configs" / "acceptance_sample.yaml", root=project)

    assert task.launch[1] == str(paths.PACKAGE_DATA / paths.SAMPLE_PRODUCT), task.launch
    assert task.platform.sources()["sample_product"] == paths.FROM_PACKAGE


@case(id="T_00_00_06_000000_04", clause="free: 平台:分发", tags=["分发"])
def test_a_package_that_lacks_the_sample_product_says_so(tmp_path, monkeypatch):
    """包内缺示例产品 = 安装损坏:消息必须说「包内缺」,并把责任指回包,不是用户的项目。"""
    broken = tmp_path / "package" / "_data"
    shutil.copytree(paths.PACKAGE_DATA, broken)
    (broken / paths.SAMPLE_PRODUCT).unlink()
    monkeypatch.setattr(paths, "PACKAGE_DATA", broken)
    project = _installed_shaped_project(tmp_path)

    with pytest.raises(TaskFileError) as error:
        load_task(project / "configs" / "acceptance_sample.yaml", root=project)

    message = str(error.value)
    assert "包内缺 tests_support/fake_product.py" in message, message
    assert "重装平台包" in message, message
    assert str(broken / paths.SAMPLE_PRODUCT) in message, f"没说是包里的哪一处:{message}"
