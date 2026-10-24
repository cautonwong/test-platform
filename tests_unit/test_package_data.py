"""
随包资源(票 09):`src/testplatform/_data/` 是执行器那几样资源随包走的那份,**源在仓库根**。

两份文件必然有两处,所以这里守住不漂移:少一样 → 装完跑不了任务;内容不一致 → 装的平台
和仓库里跑的平台是两个东西。前者由 `PlatformLayout.missing()` 在加载任务时报,后者由这里报。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from testplatform.config import paths

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "src" / "testplatform" / "_data"

# 随包副本 → 仓库里的源。改了源没同步这里,这条用例红。
COPIED = {
    "configs/clauses.yaml": REPO / "configs" / "clauses.yaml",
    "configs/mock_registers.yaml": REPO / "configs" / "mock_registers.yaml",
    "configs/import/params.yaml": REPO / "configs" / "import" / "params.yaml",
    "tools/mock_modbus_meter.py": REPO / "tools" / "mock_modbus_meter.py",
}


def test_the_package_data_directory_is_where_the_layout_looks_for_it():
    """解析点只有一个:`paths.PACKAGE_DATA` 必须就是仓库里这个目录(装完是 site-packages 的同一层)。"""
    assert paths.PACKAGE_DATA == DATA.resolve()


@pytest.mark.parametrize("relative", sorted(paths.PACKAGE_REQUIRED))
def test_every_required_resource_ships_with_the_package(relative):
    """哪一样不随包走,装完就少一样 —— `layout()` 会在加载任务时报平台资源不全。"""
    assert (DATA / relative).is_file(), f"包内缺 {relative}"


@pytest.mark.parametrize("relative", sorted(COPIED))
def test_the_shipped_copy_does_not_drift_from_the_repository_source(relative):
    assert (DATA / relative).read_bytes() == COPIED[relative].read_bytes(), (
        f"包内 {relative} 与 {COPIED[relative].relative_to(REPO)} 不一致:"
        f"改源之后要把副本同步过去(两份都在版本库里)"
    )
