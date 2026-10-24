"""用例身份(id / 标准条款 / 标签)与筛选条件(model / capability / tag / case)。

`clause` 是**条款 id**(见 configs/clauses.yaml),不是自由文本;确实没有受控条款时写成
``free: <原文>``(迁移期旧写法)。收集期校验:拼错的 id 报错并列出可用 id ——
条款只有能被聚合,报告才回答得了"这条标准有没有人测"。

元数据的唯一事实源就是测试函数上的 ``@case(...)``:身份挂在函数对象上,
收集期的筛选(testplatform/plugin.py)和证据层(票 6)都从 ``case_of`` 读同一份值。
没有第二份用例清单,所以不存在"代码改了忘了改表"。

唯一的例外是设置类条款:一个函数跑 N 条条款,身份由参数给(``case_from_param``),
编号与 clause 取自设备清单的 ``setting_cases``,收集期由插件挂到**节点**上 ——
仍然只有 ``case_of`` 一个访问点。

这个模块不 import pytest:它只认识"挂在函数/节点上的属性"和一个节点 id,
pytest 的 item 由调用方传进来(``case_of`` / ``case_param_of`` 取 ``item.obj``)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

CASE_ATTR = "__case_identity__"
# 身份"由参数给"的声明:函数上先挂一个参名,收集期从该参数取条款编号、从清单取 clause。
CASE_PARAM_ATTR = "__case_from_param__"

# --select 与清单里的 test_matrix 共用同一套写法。
SELECTOR_KINDS = ("model", "capability", "tag", "case")
# 矩阵项只表达"哪些用例",所以只允许这两种。
MATRIX_KINDS = ("case", "tag")

SELECT_HELP = "model:<型号名> / capability:<能力名> / tag:<标签> / case:<编号前缀>"


@dataclass(frozen=True)
class Case:
    """一条用例的身份。id 用上游 legacy 平台的编号形式(如 M_00_00_01_000002_09)。"""

    id: str
    # 条款 id(条款清单里的键)或 `free: <原文>`。收集期校验,不在这里猜。
    clause: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParamCase:
    """一条用例身份"由参数给"的声明:参数值就是清单 setting_cases 里的条款编号。

    标签只能写在这里(同一函数的 N 个节点共用);编号与 clause 取自清单,一处一条。
    """

    param: str
    tags: tuple[str, ...] = ()


def case(id: str, clause: str, tags: Iterable[str] = ()):
    """声明一条用例的编号、标准条款与标签::

        @case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2", tags=["一致性", "读数"])
        @pytest.mark.contract
        def test_meter_reads_physical_value_by_reading_key(meter): ...

    身份挂在测试函数本身上,@case 与其它装饰器的先后顺序不影响结果。
    `clause` 写条款清单里的 id;不是受控条款时写 ``free: <原文>``。
    """
    identity = Case(id=id, clause=clause, tags=tuple(tags))

    def _attach(test_function):
        setattr(test_function, CASE_ATTR, identity)
        return test_function

    return _attach


def case_of(target) -> Case | None:
    """公开访问点:测试函数或 pytest 的 item 都能拿到身份,没声明 @case 的返回 None。

    先看挂在节点上的身份:``case_from_param`` 声明的用例,收集期会把 resolve 出来的身份
    挂到每个参数节点上(参数不同 = 条款不同)。再退回函数上的 ``@case``。
    证据层(票 6)从这里取 id/clause/tags 写进证据文件,不另建索引。
    """
    for holder in (target, getattr(target, "obj", None)):
        identity = getattr(holder, CASE_ATTR, None)
        if isinstance(identity, Case):
            return identity
    return None


def case_from_param(param: str = "case_id", tags: Iterable[str] = ()):
    """身份由参数给:一个函数跑 N 条条款,N 个节点各用自己的编号与 clause::

        @case_from_param("case_id", tags=["一致性", "设置"])
        def test_setting_roundtrip(setting_case, composition_root): ...

    编号与 clause 从清单的 ``setting_cases`` 取(参数值就是条款编号),收集期由
    testplatform/plugin.py 挂到每个参数节点上。写成 527 个函数就退化成上游那个形状了 ——
    参考 .scratch/device-model/issues/24-case-id-uniqueness-under-parametrization.md。
    """
    declaration = ParamCase(param=param, tags=tuple(tags))

    def _attach(test_function):
        setattr(test_function, CASE_PARAM_ATTR, declaration)
        return test_function

    return _attach


def case_param_of(target) -> ParamCase | None:
    """用例声明的"身份由参数给"(没有声明返回 None)。没 resolve 之前 ``case_of`` 是空的:
    编号在清单里,收集期才拿得到。"""
    return getattr(getattr(target, "obj", target), CASE_PARAM_ATTR, None)


def set_case_of(target, identity: Case) -> None:
    """把收集期 resolve 出来的身份挂到节点上:参数化节点各挂各的,函数上的身份不动。"""
    setattr(target, CASE_ATTR, identity)


@dataclass(frozen=True)
class Selector:
    """一条筛选条件。--select 与适用矩阵项共用;矩阵只允许 case 与 tag。"""

    kind: str
    value: str

    def matches(self, *, identity: Case | None = None, nodeid: str = "", model_names=(), capabilities=()) -> bool:
        """tag / case 只看用例身份;model / capability 只看用例落在哪些设备上。

        没标 @case 的用例,case 条件退化成按 pytest 节点 id(或函数名)前缀匹配,
        仍然可以只跑某一条。
        """
        if self.kind == "tag":
            return identity is not None and self.value in identity.tags
        if self.kind == "case":
            if identity is not None:
                return identity.id.startswith(self.value)
            return nodeid.startswith(self.value) or nodeid.split("::")[-1].startswith(self.value)
        if self.kind == "model":
            return self.value in model_names
        return self.value in capabilities


def parse_select(text: str) -> tuple[Selector, ...]:
    """解析 --select:逗号分隔、可叠加(叠加是"且"),如 ``model:inverter_pv_series,tag:一致性``。

    空字符串 = 不筛选。写法不认识就报错,不静默当成"全跑"——那会让人以为筛过了。
    """
    selectors = []
    for raw in (text or "").split(","):
        written = raw.strip()
        if not written:
            continue
        kind, sep, value = written.partition(":")
        if not sep or kind not in SELECTOR_KINDS or not value:
            raise ValueError(f"--select 无法识别 {written!r},可用写法: {SELECT_HELP}")
        selectors.append(Selector(kind, value))
    return tuple(selectors)


def parse_matrix_entries(model_name: str, entries: Sequence) -> tuple[Selector, ...]:
    """适用矩阵里某个型号的项:裸写用例编号,或 ``tag:<标签>``。"""
    selectors = []
    for entry in entries:
        written = str(entry).strip()
        kind, sep, value = written.partition(":")
        if not sep:
            kind, value = "case", written
        if kind not in MATRIX_KINDS or not value:
            raise ValueError(
                f"test_matrix 型号 {model_name!r}: {entry!r} 只能写用例编号"
                f"(如 M_00_00_01_000002_09)或 tag:<标签>,可用写法: {list(MATRIX_KINDS)}"
            )
        selectors.append(Selector(kind, value))
    return tuple(selectors)
