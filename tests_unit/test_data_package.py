"""
公共数据包(票 09):只装数据、不含用例逻辑;条款清单不跟平台那份分叉。

两条都能机械验,而且都能失败:往包里放一个 `.py` 用例 → 第一条红;
改了 `configs/clauses.yaml` 没同步到包里 → 第二条红。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "packages" / "e2e-data" / "src" / "e2e_data"
PLATFORM_CLAUSES = REPO / "configs" / "clauses.yaml"


def test_the_data_package_holds_data_only():
    """公共包不含用例逻辑:除了包声明文件,里面只有数据文件。"""
    code = [
        path.name
        for path in PACKAGE.rglob("*.py")
        if path.name != "__init__.py"
    ]
    assert not code, f"公共数据包里出现了代码(用例逻辑该留在 monorepo): {code}"


def test_the_shipped_clauses_do_not_drift_from_the_platform_copy():
    """条款清单在仓库里有平台那份与公共包那份:两份都进版本库,内容必须一样。"""
    shipped = PACKAGE / "clauses.yaml"

    assert shipped.is_file(), "公共包里缺 clauses.yaml"
    assert shipped.read_bytes() == PLATFORM_CLAUSES.read_bytes(), (
        "packages/e2e-data/src/e2e_data/clauses.yaml 与 configs/clauses.yaml 不一致:"
        "它现在是从真源生成的,跑一次 python tools/sync_data.py"
    )
