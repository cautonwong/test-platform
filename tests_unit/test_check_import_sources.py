"""`check_import_sources.collect()` 必须把 `symbols.yaml` 也收进逐行断言。

守卫用例不加会把"符号表出处"变成唯一靠人工核对的产物 —— 而这条正是 55 要消灭的差异。
它只验"收进来了"(纯函数,不读上游),真正的逐行断言由 `UPSTREAM_DIR` 下的手工运行覆盖。
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "check_import_sources.py"


def _tool():
    pytest.importorskip("yaml")
    spec = importlib.util.spec_from_file_location("check_import_sources_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_symbol_sources_are_collected():
    checks, _problems, _negatives = _tool().collect()

    # 按 label 前缀取,不按 source 取:bench_quantities / wiring_modes 也引用同一个上游文件。
    symbols = [c for c in checks if str(c[0]).startswith("symbols.yaml ")]
    assert symbols, "check_import_sources 没把 symbols.yaml 收进来 —— 它又变成只靠人工核对的了"
    assert [c[0].split(" ", 1)[1] for c in symbols] == ["Un", "In", "Hz"], symbols
    for label, source, expect in symbols:
        assert source.count(":") == 1, f"{label}: source 必须是 '文件:行号',拿到 {source!r}"
        assert expect, f"{label}: 缺 expect(要断言的原文)"
