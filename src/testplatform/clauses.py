"""
标准条款受控清单:条款在 `configs/clauses.yaml` 里声明一次(id → 标准 / 编号 / summary),
用例按 id 引用;拼错在**收集期**报错并列出可用 id(见 testplatform/plugin 的同名校验)。

自由文本只有一个显式入口:`free:` 前缀。旧用例的 clause 是自由文本,迁移期允许原样保留,
但必须写成 `free: <原文>` —— 让"这条没有受控条款"在代码里一眼看得见。裸自由文本直接报错,
否则兼容会变成永久双轨:新用例顺手写一句文本,谁也不知道它到底指哪条标准。

`summary` 是本仓库这组用例的**用法概括**,不是标准原文标题(条款原文没有入库)。
报告与目录只能用它说明"这组用例测什么",不得渲染成"依据某条标准的要求:……"。

聚合(报告 / 资产目录)一律按 id:`group_by_clause`。要人读的文字从注册表取。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

import yaml

from testplatform.cases import Case
from testplatform.config import paths

FREE_PREFIX = "free:"
# 平台自带的条款清单。缺省给**绝对路径**:条款清单是平台资产,不该随 cwd 漂 ——
# 嵌套跑的 pytest(cwd 在临时项目里)也仍然读同一份受控集合。
# 位置由 config/paths.py 统一解析(显式 > TESTPLATFORM_ROOT > 探测),这里不按源码树相对层级推路径。
DEFAULT_CLAUSES_RELATIVE = "configs/clauses.yaml"

# 写入清单时可以给的字段。`not_applicable` 可选:只有"确认验不了"的条款才写。
_ENTRY_FIELDS = ("standard", "number", "summary", "not_applicable")
_REQUIRED_FIELDS = ("standard", "number", "summary")


class ClauseConfigError(ValueError):
    """条款清单本身写坏了:缺字段、重复 id、未知字段。消息里带是哪一条。"""


@dataclass(frozen=True)
class Clause:
    """清单里的一条条款。`number` 可以为空(有些条款只有主题、没有编号)。"""

    id: str
    standard: str
    number: str
    summary: str
    # 非空 = 这条条款经确认**本平台验不了**,理由写在这里(不许留空话)。
    # 目录据此把它从"缺口"挪到"已知豁免":没被验这件事仍然写在产物里,只是不再是待办。
    not_applicable: str = ""

    @property
    def label(self) -> str:
        """人读的"标准 + 编号",不包含 summary —— summary 不是标准原文。"""
        return f"{self.standard} {self.number}".strip()


class ClauseRegistry:
    """条款 id → 条款。未知 id 一律报错,不返回 None:静默 None 会让拼错的条款混进报告。"""

    def __init__(self, clauses: Mapping[str, Clause]) -> None:
        self._clauses: Dict[str, Clause] = dict(clauses)

    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._clauses))

    def has(self, reference: str) -> bool:
        return reference in self._clauses

    def get(self, reference: str) -> Clause:
        try:
            return self._clauses[reference]
        except KeyError:
            raise ClauseConfigError(
                f"条款 {reference!r} 不在条款清单里;可用 id: {list(self.ids())}"
            ) from None

    @staticmethod
    def is_free(reference: str) -> bool:
        """显式声明为自由文本(迁移期旧写法)。空字符串不算:那是漏写。"""
        return reference.startswith(FREE_PREFIX) and bool(
            reference[len(FREE_PREFIX):].strip()
        )

    @staticmethod
    def free_text(reference: str) -> str:
        return reference[len(FREE_PREFIX):].strip()

    def resolve(self, reference: str) -> Clause | None:
        """条款 id → 条款对象;`free:` 原文 → None;其余 → ClauseConfigError。"""
        if self.is_free(reference):
            return None
        return self.get(reference)


def default_clauses_path() -> Path:
    """平台自带的那份条款清单(按平台根解析)。"""
    return paths.clauses_path()


def load_clauses(path: str | Path | None = None) -> ClauseRegistry:
    """读条款清单(省略 path = 平台自带那份)。任何一处写坏都在这里报,并指出是哪个 id 的哪个字段。"""
    clauses_path = default_clauses_path() if path is None else Path(path)
    if not clauses_path.is_file():
        raise ClauseConfigError(f"条款清单不存在: {clauses_path}")
    try:
        raw = yaml.load(clauses_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ClauseConfigError(f"条款清单 {clauses_path}: YAML 读不了: {exc}") from exc
    if not isinstance(raw, Mapping) or "clauses" not in raw:
        raise ClauseConfigError(f"条款清单 {clauses_path}: 顶层必须是 {{clauses: {{id: ...}}}}")
    unknown_top = sorted(set(raw) - {"clauses"})
    if unknown_top:
        raise ClauseConfigError(f"条款清单 {clauses_path}: 不认识的顶层字段 {unknown_top}")
    declared = raw["clauses"]
    if not isinstance(declared, Mapping):
        raise ClauseConfigError(f"条款清单 {clauses_path}: clauses 必须是映射(id → 条款)")

    clauses: Dict[str, Clause] = {}
    for clause_id, entry in declared.items():
        where = f"条款清单 {clauses_path}: 条款 {clause_id!r}"
        if not isinstance(entry, Mapping):
            raise ClauseConfigError(f"{where} 必须是映射,字段 {list(_ENTRY_FIELDS)}")
        unknown = sorted(set(entry) - set(_ENTRY_FIELDS))
        if unknown:
            raise ClauseConfigError(f"{where} 有不认识的字段 {unknown},只允许 {list(_ENTRY_FIELDS)}")
        missing = [name for name in _REQUIRED_FIELDS if name not in entry]
        if missing:
            raise ClauseConfigError(f"{where} 缺字段 {missing},必须有 {list(_REQUIRED_FIELDS)}")
        standard = str(entry["standard"]).strip()
        number = str(entry["number"]).strip()
        summary = str(entry["summary"]).strip()
        not_applicable = str(entry.get("not_applicable") or "").strip()
        for name, value in (("standard", standard), ("summary", summary)):
            if not value:
                raise ClauseConfigError(f"{where} 的 {name} 不能为空")
        if str(clause_id).startswith(FREE_PREFIX):
            raise ClauseConfigError(f"{where}: id 不能以 {FREE_PREFIX!r} 开头(那是自由文本的写法)")
        clauses[str(clause_id)] = Clause(
            id=str(clause_id),
            standard=standard,
            number=number,
            summary=summary,
            not_applicable=not_applicable,
        )
    return ClauseRegistry(clauses)


def group_by_clause(cases: Iterable[Case]) -> Dict[str, Tuple[str, ...]]:
    """条款 → 用例编号(排序去重)。报告与资产目录按条款聚合只用这一个入口。

    自由文本用例按 `free: <原文>` 整串聚合:它们没有受控条款,不能被并进任何标准条款,
    也不能因为"看起来像"就被归到某条 id 下。
    """
    grouped: Dict[str, set] = {}
    for identity in cases:
        grouped.setdefault(identity.clause, set()).add(identity.id)
    return {clause: tuple(sorted(ids)) for clause, ids in sorted(grouped.items())}


class _UniqueKeyLoader(yaml.SafeLoader):
    """重复 key 直接报错:pyyaml 默认保留后一个,条款清单里等于**静默丢条款**。"""

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ClauseConfigError(f"条款清单里有重复的 key {key!r}:后一条会静默覆盖前一条")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)
