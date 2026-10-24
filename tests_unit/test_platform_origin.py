"""平台来源与指纹(票 15a):`eject` 之后平台可改,结论必须能追溯到"谁改的"。

三条不许:来源是闭集(package/project)、指纹永不缺席、报告首行写出 project。
"""

from __future__ import annotations

import pathlib

import pytest

from testplatform.config import origin
from testplatform.reporting.report import Report, _VERDICT_TEXT, render


def _report(**env) -> Report:
    return Report(
        directory=pathlib.Path("."),
        task_id="t",
        kind="task",
        product=None,
        select=None,
        verdict=next(iter(_VERDICT_TEXT)),
        note="全绿",
        reason=None,
        devices_passed=1,
        devices_total=1,
        scenarios_passed=1,
        scenarios_total=1,
        collected=1,
        deselected=0,
        env=env,
    )


def test_the_source_is_a_closed_set():
    assert origin.SOURCES == ("package", "project")


def test_code_inside_the_project_is_project_source(tmp_path):
    package = tmp_path / "platform" / "testplatform"
    package.mkdir(parents=True)

    assert origin.source_of(tmp_path, package_dir=package) == origin.PROJECT
    # 落在项目外 = 装进来的那份,只读
    assert origin.source_of(tmp_path / "other", package_dir=package) == origin.PACKAGE


def test_the_digest_follows_the_content_and_the_name(tmp_path):
    package = tmp_path / "testplatform"
    package.mkdir()
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    (package / "_data").mkdir()
    (package / "_data" / "d.txt").write_text("d\n", encoding="utf-8")
    before = origin.digest(origin.project_files(package), root=package)

    (package / "a.py").write_text("x = 2\n", encoding="utf-8")
    changed = origin.digest(origin.project_files(package), root=package)
    (package / "a.py").write_text("x = 1\n", encoding="utf-8")
    restored = origin.digest(origin.project_files(package), root=package)

    assert before != changed
    assert before == restored
    # 改名也算改了平台(路径进哈希)
    (package / "a.py").rename(package / "b.py")
    assert origin.digest(origin.project_files(package), root=package) != before


def test_the_digest_is_never_empty(tmp_path):
    """纪律 §12:字段不许缺席。源码布局下也算得出值(不是 null、不是空串)。"""
    assert origin.sha256_of(tmp_path)


@pytest.mark.parametrize("source", ["package", "project"])
def test_the_report_first_line_names_the_platform_source(source):
    """报告是唯一能承载"这轮结论基于谁改过的平台"的地方。"""
    first = render(_report(platform_source=source), registry=None)[0]

    assert first.startswith("结论:"), first
    assert f"platform_source: {source}" in first, first


def test_a_report_without_the_field_says_unknown_instead_of_nothing():
    """字段缺席时写 unknown —— 空着会让读者以为"没这回事"。"""
    assert "platform_source: unknown" in render(_report(), registry=None)[0]
