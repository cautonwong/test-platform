"""
非有限读数(NaN / ±inf)不许被判合格(票 12b)。

复现(修前):模拟从站回 float32 的 NaN 字节 → 读数是 `nan`,而判据求值走"没越界就通过":
`nan > 242` 与 `nan < 198` 都是 False → **breaches 为空、passed=True**。
这类缺陷最贵的一点是它不红、它绿。
"""

from __future__ import annotations

import math

import pytest

from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity


def _profile() -> CriteriaProfile:
    return CriteriaProfile(
        name="p", criteria=(Criterion("电压", low=198.0, high=242.0),)
    )


@pytest.mark.parametrize(
    "value, label",
    [(math.nan, "NaN"), (math.inf, "+inf"), (-math.inf, "-inf")],
)
def test_a_non_finite_reading_is_rejected_with_a_named_reason(value, label):
    evaluation = _profile().evaluate({"电压": value})

    assert evaluation.passed is False, f"{label} 被判合格了"
    (breach,) = evaluation.breaches
    assert breach.severity is Severity.REJECT
    assert "不是有限数" in breach.describe(), breach.describe()
    # 原因里要能看出是哪种非有限值,现场才分得清"采集坏了"与"量程选错了"
    assert label in breach.describe() or "NaN/inf" in breach.describe()


def test_a_finite_reading_still_passes():
    """守卫不能反过来:正常读数必须照旧合格。"""
    assert _profile().evaluate({"电压": 230.0}).passed is True


def test_nan_is_not_quietly_dropped_from_the_readings():
    """NaN 不许被静默丢掉(丢掉会让判据集变成"缺量",而缺量在别处另有口径)。"""
    evaluation = _profile().evaluate({"电压": math.nan})

    assert len(evaluation.breaches) == 1
