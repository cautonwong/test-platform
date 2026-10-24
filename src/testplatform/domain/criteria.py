"""
判据 —— "合格与否"的值对象。

纯数据 + 求值:不认识协议、不认识具体设备类型。设备把一次读数按
物理量名打包成 mapping 交给 CriteriaProfile.evaluate(),拿回一个 Evaluation:
合格/不合格,以及每一条判据超出多少。聚合根只负责"记下来",判合格是判据的事。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class Severity(Enum):
    REJECT = "reject"  # 超出即判不合格
    WARN = "warn"  # 超出只是软信号,不影响合格结论


@dataclass(frozen=True)
class Criterion:
    """一个物理量允许的落点范围;某一边为 None 表示该侧不判。

    边界默认闭区间(清单里写 min/max);exclusive 侧改成开区间(清单里写 gt/lt),
    此时落在边界上也算违反——0V 不是一个合格电压。

    equals 判等值(写回参数类点:"写 1601 → 读回应等于 1601"),与区间互斥——
    同一个判据既要区间又要等值说不清期望是什么,清单在装配前就拒掉。
    """

    quantity: str
    low: Optional[float] = None
    low_exclusive: bool = False
    high: Optional[float] = None
    high_exclusive: bool = False
    severity: Severity = Severity.REJECT
    # 期望值可以是数字 / 字符串 / 布尔:参数类点在清单里常写成编码后的样子("1601")。
    equals: Any = None

    def evaluate(self, value: float) -> Optional["Breach"]:
        # 只有数值才谈"有限":等值判据允许字符串 token(如计量模式 "1601"),那是文本比较
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            # NaN 参与比较恒为 False(0 <= NaN <= 上限 是假的),所以"没越界 = 通过"会把
            # 采集失败判成合格 —— 这正是 12b 复现出来的那条:读数是 NaN、passed=True。
            # 读数是物理量,NaN/±inf 只可能表示采集坏了:直接判不合格,并写明原因。
            return Breach(
                self.quantity,
                value,
                None,
                0.0,
                Severity.REJECT,
                invalid_reason="不是有限数(NaN/inf):读数不可用,不能判合格",
            )
        if self.equals is not None:
            if _same_value(self.equals, value):
                return None
            return Breach(self.quantity, value, None, 0.0, self.severity, expected=self.equals)
        if self.high is not None:
            crossed = value >= self.high if self.high_exclusive else value > self.high
            if crossed:
                return Breach(self.quantity, value, self.high, value - self.high, self.severity)
        if self.low is not None:
            crossed = value <= self.low if self.low_exclusive else value < self.low
            if crossed:
                return Breach(self.quantity, value, self.low, self.low - value, self.severity)
        return None


def coerce_number(value: Any) -> Optional[float]:
    """能当数字比就返回数字,否则 None(文本 token 点读回来的是字符串)。"""
    if isinstance(value, bool):
        return float(value)  # 布尔点读回来是 0/1:equals: true 与 1.0 是同一个值
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _same_value(expected: Any, value: Any) -> bool:
    """按值比,不按类型比:清单写 "1601" 与点表读回 1601.0 是同一个值。

    两边都能当数字就按数字比(倍率换算会带浮点尾巴,所以用 isclose);任一边不是数字
    (文本 token 点,如编码位串 "F001")就退化成按文本比——不去猜一个编码字符串的数值。
    """
    left, right = coerce_number(expected), coerce_number(value)
    if left is not None and right is not None:
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return _text(expected) == _text(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else repr(value)


@dataclass(frozen=True)
class Breach:
    """一条被违反的判据:实测值、越过的边界、超出多少(恒为正,单位同读数)。

    equals 判据没有边界:limit 为 None、exceeded_by 为 0,期望值在 expected 里。
    """

    quantity: str
    value: float
    limit: Optional[float]
    exceeded_by: float
    severity: Severity
    expected: Any = None
    # 非空 = 这条违反不是"越界",而是**读数本身不可用**(NaN / ±inf,见 12b)。
    # 只进 describe(),不进证据字段:证据的 reading.evaluation 形状由契约用例锁着,
    # 加一个"原因"字段会让所有下游解析器同时改;人读的报告那一行里有它就够了。
    invalid_reason: str = ""

    def describe(self) -> str:
        if self.invalid_reason:
            return f"{self.quantity}={self.value!r} {self.invalid_reason}"
        if self.expected is not None:
            # 等值判定没有"超出多少":能说的只有期望什么、实际什么。repr 带上类型,
            # 现场才看得出清单里写的是字符串还是数字。
            return f"{self.quantity} 期望 {self.expected!r},实测 {self.value!r}"
        if self.exceeded_by == 0:
            # 开区间边界:落在边界上也算越界,读成"超出 0"会让人以为设备其实合格。
            return f"{self.quantity}={self.value} 等于边界 {self.limit}(不允许等于)"
        return f"{self.quantity}={self.value} 越过 {self.limit},超出 {self.exceeded_by}"


@dataclass(frozen=True)
class Evaluation:
    """一次读数的判据结论。passed 只被 REJECT 级判据影响。"""

    breaches: Tuple[Breach, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(b.severity is Severity.REJECT for b in self.breaches)

    @property
    def warnings(self) -> Tuple[Breach, ...]:
        return tuple(b for b in self.breaches if b.severity is Severity.WARN)


@dataclass(frozen=True)
class CriteriaProfile:
    """具名判据集:按整机声明一次,设备引用它,个别设备可整条替换其中的判据。"""

    name: str
    criteria: Tuple[Criterion, ...] = ()

    def evaluate(self, readings: Mapping[str, float]) -> Evaluation:
        breaches = []
        for criterion in self.criteria:
            if criterion.quantity not in readings:
                raise KeyError(
                    f"判据集 {self.name!r} 判的 {criterion.quantity!r} 不在本次读数里:"
                    f" {sorted(readings)}"
                )
            breach = criterion.evaluate(readings[criterion.quantity])
            if breach is not None:
                breaches.append(breach)
        return Evaluation(tuple(breaches))
