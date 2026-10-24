"""
判据是纯数据 + 求值:不需要设备、协议、清单,毫秒级验证"合格/不合格"与"超出多少"。
"""
import pytest

from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity


def _profile(*criteria: Criterion) -> CriteriaProfile:
    return CriteriaProfile(name="t", criteria=criteria)


def test_reading_inside_range_passes():
    profile = _profile(Criterion("grid_freq_hz", low=45, high=55))

    evaluation = profile.evaluate({"grid_freq_hz": 50.0})

    assert evaluation.passed
    assert evaluation.breaches == ()


def test_exceeding_upper_bound_reports_by_how_much():
    profile = _profile(Criterion("grid_freq_hz", low=45, high=55))

    evaluation = profile.evaluate({"grid_freq_hz": 60.0})

    assert not evaluation.passed
    (breach,) = evaluation.breaches
    assert breach.quantity == "grid_freq_hz"
    assert breach.limit == 55
    assert breach.exceeded_by == pytest.approx(5.0)
    assert breach.describe() == "grid_freq_hz=60.0 越过 55,超出 5.0"


def test_under_lower_bound_reports_by_how_much():
    profile = _profile(Criterion("soc_percent", low=10, severity=Severity.WARN))

    evaluation = profile.evaluate({"soc_percent": 6.5})

    (breach,) = evaluation.breaches
    assert breach.limit == 10
    assert breach.exceeded_by == pytest.approx(3.5)


def test_warn_severity_does_not_fail_the_evaluation():
    profile = _profile(Criterion("output_power_w", high=900, severity=Severity.WARN))

    evaluation = profile.evaluate({"output_power_w": 950})

    assert evaluation.passed
    assert evaluation.warnings[0].exceeded_by == pytest.approx(50.0)


def test_exclusive_bound_rejects_a_value_sitting_exactly_on_it():
    """gt/lt 是开区间:落在边界上也违反——这就是 0V 判不合格的依据。"""
    profile = _profile(
        Criterion("grid_voltage_v", low=0, low_exclusive=True),
        Criterion("grid_freq_hz", high=55, high_exclusive=True),
    )

    assert not profile.evaluate({"grid_voltage_v": 0, "grid_freq_hz": 50}).passed
    assert not profile.evaluate({"grid_voltage_v": 230, "grid_freq_hz": 55}).passed
    assert profile.evaluate({"grid_voltage_v": 0.1, "grid_freq_hz": 54.9}).passed


def test_breach_on_an_exclusive_bound_reads_as_equal_not_exceeded_by_zero():
    """落在开区间边界上时 exceeded_by 是 0,读成"超出 0"会让人以为没差多少:
    呈现要说清是等于边界、而边界不允许等于。"""
    profile = _profile(Criterion("grid_voltage_v", low=0, low_exclusive=True))

    (breach,) = profile.evaluate({"grid_voltage_v": 0.0}).breaches

    assert breach.exceeded_by == pytest.approx(0.0)
    assert breach.describe() == "grid_voltage_v=0.0 等于边界 0(不允许等于)"


def test_criterion_for_absent_reading_is_a_configuration_error():
    profile = _profile(Criterion("grid_freq_hz", low=45, high=55))

    with pytest.raises(KeyError):
        profile.evaluate({"soc_percent": 50})


def test_equals_passes_when_the_reading_is_the_expected_value():
    """等值判定:写 1601 之后读回归 1601。清单里写的是字符串,点表解出来的却是 float。"""
    profile = _profile(Criterion("有功计量模式", equals="1601"))

    evaluation = profile.evaluate({"有功计量模式": 1601.0})

    assert evaluation.passed
    assert evaluation.breaches == ()


def test_equals_failure_names_expected_and_actual_values():
    """等值判定没有"超出多少":能说的只有期望什么、实测什么。"""
    profile = _profile(Criterion("有功计量模式", equals="1601"))

    (breach,) = profile.evaluate({"有功计量模式": 1602.0}).breaches

    assert breach.expected == "1601"
    assert breach.limit is None
    assert breach.describe() == "有功计量模式 期望 '1601',实测 1602.0"


def test_equals_compares_by_value_not_by_type():
    """同一个值写成数字、写成字符串、读数带上倍率的浮点尾巴,都算相等。"""
    for expected in (1601, 1601.0, "1601", " 1601 "):
        profile = _profile(Criterion("有功计量模式", equals=expected))
        assert profile.evaluate({"有功计量模式": 1601.0}).passed, expected
        assert profile.evaluate({"有功计量模式": "1601"}).passed, expected
        assert not profile.evaluate({"有功计量模式": 1600.0}).passed, expected


def test_equals_on_a_text_token_compares_as_text():
    """文本 token 点(编码位串)不是数字:按文本比,不去猜它的数值。"""
    profile = _profile(Criterion("编码位串", equals="F001"))

    assert profile.evaluate({"编码位串": "F001"}).passed
    (breach,) = profile.evaluate({"编码位串": "F002"}).breaches
    assert breach.describe() == "编码位串 期望 'F001',实测 'F002'"


def test_equals_boolean_matches_a_flag_reading():
    """布尔点读回来是 0/1:equals: true 判的就是 1。"""
    profile = _profile(Criterion("继电器状态", equals=True))

    assert profile.evaluate({"继电器状态": 1.0}).passed
    assert not profile.evaluate({"继电器状态": 0.0}).passed


def test_equals_warn_severity_does_not_fail_the_evaluation():
    """等值判定也吃 severity:写成 warn 就只进 warnings。"""
    profile = _profile(Criterion("有功计量模式", equals="1601", severity=Severity.WARN))

    evaluation = profile.evaluate({"有功计量模式": 0.0})

    assert evaluation.passed
    assert evaluation.warnings[0].expected == "1601"
