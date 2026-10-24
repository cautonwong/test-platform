"""守卫:给写用例工程师的缺口清单必须能独立转发,不能夹带本仓标识符。

`docs/import-gaps-13comm.md` 是**临时产物**:清单清算完就该删。所以文件不在了直接 skip ——
否则守卫会挡住自己的退休(删掉文档,守卫先红)。
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "import-gaps-13comm.md"

# 宁宽不窄:凡是我们内部才认识的名字,出现在这份外发件里就是错的 —— 对方看不懂,
# 或者看懂了也没法行动(他们不装我们的工具、不看我们的报表代码)。
BANNED = (
    # 报表 / 导入器里的判据名与字段名
    "no_expected", "duplicate_case_id", "route_of", "setting_cases", "parse_manifest",
    "readings_recorded", "evidence_gap", "bench:", "device_params", "point_table",
    "writable", "manifest", "criteria", "clause",
    # 本仓路径与产物
    "configs/", "configs\\", "tests_unit", "tools/", "build/", "docs/", ".scratch/",
    ".py", ".yaml", ".yml", "coverage", "coverage.md", "xlsx",
    # 型号参数名(本仓命名):外发件只该说"额定电压",不该说我们的字段名
    "rated_voltage_v", "rated_current_a", "rated_freq_hz", "rated_power_w",
    # 工具与流程名
    "pytest", "openpyxl", "import_cases", "yaml", "TODO", "ADR",
    # 测试规模里的内部记号
    "bench: ", "snake_case", "worktree", "git ",
)


def _text() -> str:
    if not DOC.exists():
        pytest.skip(f"{DOC.name} 是临时产物:清单清算后该删,守卫不挡退休")
    return DOC.read_text(encoding="utf-8")


def test_the_doc_carries_no_internal_identifiers():
    """一份能整封转发的清单:里面出现的每个词,对方都得能看懂、能照做。"""
    text = _text()
    hits = sorted({word for word in BANNED if word in text})
    assert hits == [], f"外发件里出现了内部标识符: {hits}"


def test_the_doc_opens_with_a_filled_correct_example():
    """先给一个已经填对的样板,读者才知道"补内容"要补成什么样。"""
    text = _text()
    head = text.split("## 1.")[0]

    assert "M_00_00_13_000101_01" in head
    assert "第 23 行" in head and "步骤2" in head
    assert "读写一致为合格" in head


def test_the_doc_ends_with_the_handback_process():
    """末尾必须写清回填方式:改完发回 → 我们重跑 → 他们不用跑任何工具。"""
    text = _text()
    tail = text.split("改完怎么回填")[-1]

    assert "不需要跑任何工具" in tail
    assert "不需要" in tail and "重跑" in tail
