"""
资产目录 —— 从清单 + 条款清单 + 测试文件**扫描生成**的索引。

它回答六个问题,一个不多:
    型号有哪些用例覆盖 / 条款有哪些用例 / 设备能跑哪些用例 /
    型号在 CI 上可跑吗 / 缺口是什么 / 改一条判据影响谁

三条边界(写在这里因为它们每一条都能变成假数据):

1. **索引,不是容器**。用例、场景、设备仍留在原地(`tests_platform/`、`tests_scenario/`、
   `configs/`);目录只记"有什么、在哪(file:line)、属于谁",不复制正文、不搬文件。
2. **派生数据,不手改**。同一份输入重新生成必须逐字节一致(排序、无时间戳);
   `--verify` 与磁盘不一致就报出差异:多出 / 缺失 / 改了 id。
3. **只读**:不 import `reporting/`、`execution/`,也不执行测试 —— 扫描用 AST 读源码,
   **逐文件**解析后立刻丢掉语法树,不 import 任何测试模块(导入会执行它,而且收集期筛选会让
   重复编号"看不见";目录的编号唯一性必须独立于 `--select`)。

用例归属怎么算(静态可得的三个信号,不靠猜):
    * 用例引用了哪个设备类型的泛型 fixture(`meter` / `inverter` / …)→ 它对那个类型的所有型号生效
    * 型号的 `test_matrix`(适用矩阵)有定义时,按矩阵收窄
    * 型号的 `setting_cases`(设置类条款)直接点名叫例编号
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from testplatform.clauses import load_clauses
# 目录要读两个注册表(设备类型、模拟器能力),它们的内容靠"import 具体实现"时注册。
# 只 import 注册清单(只注册、不装配)—— 不 import composition.root:
# 那会把组合根与报告层一起拖进来,目录就变成"为了问一句注册表,背上整条链路"。
from testplatform.composition import registrations as _registrations  # noqa: F401
from testplatform.config import paths
from testplatform.config.manifest import PROTOCOL_ACCESS
from testplatform.transport import registry as transport_registry

CATALOG_FILE = "catalog.json"
CATALOG_DOC = "CATALOG.md"
CATALOG_VERSION = 1

# 用例归属靠"函数里出现了哪个设备类型的泛型 fixture"。这些名字来自 tests/conftest.py 的
# 按类型参数化(registry 里的类型名),目录不认识具体设备,只认类型。
DEFAULT_TEST_ROOTS = ("tests", "tests_platform", "tests_scenario", "tests_unit")

GAP_MISSING_MODEL = "missing_model"
GAP_MISSING_CRITERIA_PROFILE = "missing_criteria_profile"
GAP_QUANTITY_NOT_IN_POINT_TABLE = "quantity_not_in_point_table"
GAP_CAPABILITY_NOT_IN_POINT_TABLE = "capability_not_in_point_table"
GAP_DUPLICATE_CASE_ID = "duplicate_case_id"
GAP_CASE_WITHOUT_MODEL = "case_without_model"
GAP_MODEL_WITHOUT_SIMULATOR = "model_without_simulator"
GAP_CLAUSE_WITHOUT_CASES = "clause_without_cases"
GAP_UNRESOLVED_CASE_ID = "unresolved_case_id"

GAP_TITLES = {
    GAP_MISSING_MODEL: "设备引用的型号不存在",
    GAP_MISSING_CRITERIA_PROFILE: "型号引用的判据集不存在",
    GAP_QUANTITY_NOT_IN_POINT_TABLE: "判据量不在点表(也不是驱动映射过的域量)",
    GAP_CAPABILITY_NOT_IN_POINT_TABLE: "能力不在点表",
    GAP_DUPLICATE_CASE_ID: "用例编号重复",
    GAP_CASE_WITHOUT_MODEL: "矩阵/设置条款引用的型号不存在",
    GAP_MODEL_WITHOUT_SIMULATOR: "型号没有可跑的模拟器",
    GAP_CLAUSE_WITHOUT_CASES: "条款零覆盖",
    GAP_UNRESOLVED_CASE_ID: "用例编号不是字面量",
}


@dataclass(frozen=True)
class CaseEntry:
    """一条用例的身份与位置。`file`/`line` 是它唯一被允许记住的"正文"。"""

    id: str | None
    clause: str
    tags: tuple[str, ...]
    file: str
    line: int
    device_types: tuple[str, ...] = ()

    def as_record(self) -> dict:
        return {
            "clause": self.clause,
            "tags": list(self.tags),
            "file": self.file,
            "line": self.line,
        }


@dataclass
class Catalog:
    models: dict[str, dict] = field(default_factory=dict)
    devices: dict[str, dict] = field(default_factory=dict)
    clauses: dict[str, dict] = field(default_factory=dict)
    cases: dict[str, dict] = field(default_factory=dict)
    criteria_profiles: dict[str, dict] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    # 已知豁免:清单里标了 not_applicable 的条款 —— 验不了这件事仍然写进产物,只是不再算缺口。
    waived: list[dict] = field(default_factory=list)

    def as_record(self) -> dict:
        return {
            "version": CATALOG_VERSION,
            "counts": {
                "models": len(self.models),
                "devices": len(self.devices),
                "cases": len(self.cases),
                "clauses": len(self.clauses),
                "gaps": len(self.gaps),
            },
            "models": self.models,
            "devices": self.devices,
            "clauses": self.clauses,
            "cases": self.cases,
            "criteria_profiles": self.criteria_profiles,
            "gaps": sorted(self.gaps, key=lambda gap: (gap["kind"], gap["where"])),
            "waived": sorted(self.waived, key=lambda entry: entry["where"]),
        }


# ---------------------------------------------------------------- 扫描 ----


def scan_cases(roots: Iterable[str | Path], *, base: str | Path | None = None) -> list[CaseEntry]:
    """逐文件扫 `@case(...)` 声明。不 import、不整棵读进内存:一个文件解析完就丢树。

    用 AST 而不是正则,是因为**测试文件里会嵌测试源码**(夹具把 `@case` 写在字符串里):
    正则会把那些模板当成真用例,目录里就多出一批不存在的编号。AST 只认真实的装饰器。
    """
    base = Path(base) if base is not None else Path.cwd()
    entries: list[CaseEntry] = []
    for path in _python_files(roots):
        entries.extend(_cases_in(path, base))
    return entries


def _python_files(roots: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in roots:
        root = Path(raw)
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _cases_in(path: Path, base: Path) -> list[CaseEntry]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    constants = _module_string_constants(tree)
    # 一律 POSIX 分隔符:目录是入库产物。Windows 上生成的 `tests/x.py` 若写成反斜杠,Linux CI 重新
    # 扫出来的是正斜杠,`--verify` 会判成“变了”(跨平台假红)。指纹口径先统一,不放宽校验。
    written = (
        path.relative_to(base).as_posix() if path.is_relative_to(base) else path.as_posix()
    )
    entries = []
    for node in ast.walk(tree):
        # 只认 pytest 会收集的函数(test_*):`tests_unit/` 里有大量"用真实编号做样例"的 demo
        # 函数(它们不叫 test_,不会被收集),把那些算进目录就会凭空多出一堆重复编号。
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for decorator in node.decorator_list:
            if not _is_case_decorator(decorator):
                continue
            entries.append(
                CaseEntry(
                    id=_string_of(_keyword(decorator, "id"), constants),
                    clause=_string_of(_keyword(decorator, "clause"), constants) or "",
                    tags=tuple(_string_list(_keyword(decorator, "tags"), constants)),
                    file=written,
                    line=decorator.lineno,
                    device_types=tuple(_device_types_of(node)),
                )
            )
    return entries


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """模块级 `NAME = "字面量"`。用例编号走常量时(如 `@case(id=CASE_ID, …)`)才解得出来。"""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _is_case_decorator(node: ast.expr) -> bool:
    """`@case(...)` 或 `@testplatform.cases.case(...)`:只看最后一段名字。"""
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if isinstance(function, ast.Name):
        return function.id == "case"
    if isinstance(function, ast.Attribute):
        return function.attr == "case"
    return False


def _keyword(decorator: ast.Call, name: str) -> ast.expr | None:
    for keyword in decorator.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _string_of(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _string_list(node: ast.expr | None, constants: Mapping[str, str]) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Name) and node.id in constants:
        return [constants[node.id]]
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    return [
        value
        for value in (_string_of(element, constants) for element in node.elts)
        if value is not None
    ]


def _device_types_of(function: ast.FunctionDef) -> list[str]:
    """函数参数里出现的设备类型泛型 fixture(名字就是 registry 里的类型名)。"""
    names = {argument.arg for argument in function.args.args}
    return sorted(name for name in names if name in _device_types())


def _device_types() -> frozenset[str]:
    from testplatform.drivers.registry import registered_types

    return frozenset(registered_types())


# ---------------------------------------------------------------- 生成 ----


def build(devices_path: str | Path, *, clauses_path: str | Path | None = None,
          test_roots: Sequence[str | Path] = DEFAULT_TEST_ROOTS) -> Catalog:
    """扫清单、条款清单与测试文件,生成目录(纯读)。同一份输入 → 逐字节一致。"""
    raw = _read_yaml(devices_path)
    manifest = _ManifestView(raw)
    clause_registry = load_clauses(clauses_path)
    cases = scan_cases(test_roots)

    catalog = Catalog()
    catalog.gaps.extend(manifest.structure_gaps())

    for case in cases:
        if case.id is None:
            catalog.gaps.append(
                {
                    "kind": GAP_UNRESOLVED_CASE_ID,
                    "where": f"{case.file}:{case.line}",
                    "detail": "编号不是字面量/模块常量:目录不猜,把它改成字面量或登记到清单的 setting_cases",
                }
            )
            continue
        catalog.cases[case.id] = case.as_record()

    catalog.gaps.extend(_duplicate_gaps(cases, manifest))
    catalog.gaps.extend(manifest.setting_case_gaps())
    catalog.gaps.extend(_quantity_gaps(manifest))
    catalog.gaps.extend(_capability_gaps(manifest))

    catalog.models = manifest.models_record(_cases_by_type(cases), manifest.matrix)
    catalog.devices = manifest.devices_record()
    catalog.criteria_profiles = manifest.criteria_record()
    catalog.clauses = _clauses_record(clause_registry, catalog.cases)
    catalog.gaps.extend(_simulator_gaps(manifest))
    catalog.gaps.extend(_clause_coverage_gaps(catalog.clauses))
    catalog.waived = _waived_clauses(clause_registry)
    return catalog


def dumps(catalog: Catalog) -> str:
    """机器读的产物。排序 + 无时间戳 = 重新生成逐字节一致(校验模式靠这一点)。"""
    return json.dumps(catalog.as_record(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write(catalog: Catalog, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    machine = out / CATALOG_FILE
    human = out / CATALOG_DOC
    machine.write_text(dumps(catalog), encoding="utf-8")
    human.write_text(render(catalog), encoding="utf-8")
    return machine, human


def render(catalog: Catalog) -> str:
    """给人读的那一份:先缺口,再型号表,再条款覆盖。数字都在,正文不抄。"""
    record = catalog.as_record()
    lines = [
        "# 资产目录(生成物,不要手改)",
        "",
        "由清单 + 条款清单 + 测试文件扫描生成。校验: `python tools/catalog.py --verify`。",
        "",
        f"- 型号 {record['counts']['models']} · 设备 {record['counts']['devices']}"
        f" · 用例 {record['counts']['cases']} · 条款 {record['counts']['clauses']}"
        f" · 缺口 {record['counts']['gaps']}",
        "",
    ]
    if record["waived"]:
        lines += ["## 已知豁免(验不了,理由在此)", ""]
        lines += [f"- `{entry['where']}`: {entry['reason']}" for entry in record["waived"]]
        lines.append("")
    gaps = record["gaps"]
    if gaps:
        lines += ["## 缺口", ""]
        for kind, title in GAP_TITLES.items():
            entries = [gap for gap in gaps if gap["kind"] == kind]
            if not entries:
                continue
            lines.append(f"### {title}({len(entries)})")
            lines += [f"- `{gap['where']}`: {gap['detail']}" for gap in entries]
            lines.append("")
    else:
        lines += ["## 缺口", "", "没有缺口。", ""]

    lines += ["## 型号", "", "| 型号 | 类型 | 判据集 | 用例 | 条款 | CI 可跑 | 可注入故障 |", "|---|---|---|---|---|---|---|"]
    for name in sorted(catalog.models):
        model = catalog.models[name]
        ci = "、".join(
            f"{entry['access']}{'✓' if entry['runnable'] else '✗'}" for entry in model["ci"]
        ) or "无接入方式"
        faults = sorted({fault for entry in model["ci"] for fault in entry["faults"]})
        lines.append(
            f"| {name} | {model['type']} | {model['criteria'] or '—'} | {len(model['cases'])}"
            f" | {len(model['clauses'])} | {ci} | {'、'.join(faults) or '无'} |"
        )
    lines.append("")

    lines += ["## 条款覆盖", "", "| 条款 | 用例数 |", "|---|---|"]
    for clause_id in sorted(catalog.clauses):
        lines.append(f"| {clause_id} | {len(catalog.clauses[clause_id]['cases'])} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 校验 ----


def verify(catalog: Catalog, out_dir: str | Path) -> list[str]:
    """把"现在扫出来的"和磁盘上的产物比:多出 / 缺失 / 改了。返回差异行(空 = 一致)。"""
    machine = Path(out_dir) / CATALOG_FILE
    if not machine.is_file():
        return [f"目录不存在: {machine}(先跑一次不带 --verify 的生成)"]
    try:
        written = json.loads(machine.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"目录读不了: {machine}({exc})"]
    current = catalog.as_record()
    differences: list[str] = []
    # 版本:格式变了却没重新生成,后面的比较就失去意义 —— 先比它
    if written.get("version") != current["version"]:
        differences.append(f"version: 磁盘 {written.get('version')!r} → 现在 {current['version']!r}")
    # 各段计数:它们是"这一批有多少东西"的唯一摘要,被改掉必须看得见
    for key, value in sorted(current["counts"].items()):
        if (written.get("counts") or {}).get(key) != value:
            differences.append(
                f"counts.{key}: 磁盘 {(written.get('counts') or {}).get(key)!r} → 现在 {value!r}"
            )
    for section in ("cases", "models", "devices", "clauses", "criteria_profiles"):
        differences.extend(_diff_section(section, written.get(section) or {}, current[section]))
    differences.extend(_diff_gaps(written.get("gaps") or [], current["gaps"]))
    # 已知豁免**单独比**,不并进 gaps:豁免是"这条标准我们不验"的唯一记录,
    # 混进缺口里比对会让"豁免被删掉"看起来像"缺口多了几条"(方向完全不同)。
    differences.extend(_diff_waived(written.get("waived") or [], current["waived"]))
    return differences


def _diff_waived(written: list, current: list) -> list[str]:
    """豁免逐条比:where(哪条条款)+ reason(为什么不验)。"""
    before = {entry.get("where"): entry.get("reason") for entry in written}
    after = {entry.get("where"): entry.get("reason") for entry in current}
    lines = [f"waived: 多出 {where}(磁盘没有)" for where in sorted(set(after) - set(before))]
    lines += [f"waived: 缺失 {where}(磁盘有、扫描不到)" for where in sorted(set(before) - set(after))]
    lines += [
        f"waived: 变了 {where}: {before[where]!r} → {after[where]!r}"
        for where in sorted(set(before) & set(after))
        if before[where] != after[where]
    ]
    return lines


def _diff_section(section: str, written: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    lines = []
    for key in sorted(set(current) - set(written)):
        lines.append(f"{section}: 多出 {key!r}(磁盘没有)")
    for key in sorted(set(written) - set(current)):
        lines.append(f"{section}: 缺失 {key!r}(磁盘有、扫描不到)")
    for key in sorted(set(current) & set(written)):
        if _canonical(written[key]) != _canonical(current[key]):
            lines.append(
                f"{section}: 变了 {key!r}: 磁盘 {_short(written[key])} → 现在 {_short(current[key])}"
            )
    return lines


def _diff_gaps(written: Sequence[dict], current: Sequence[dict]) -> list[str]:
    def keyed(entries):
        return {(entry.get("kind"), entry.get("where")): entry.get("detail") for entry in entries}

    before, after = keyed(written), keyed(current)
    lines = [f"缺口: 多出 {kind}:{where}" for kind, where in sorted(set(after) - set(before))]
    lines += [f"缺口: 缺失 {kind}:{where}" for kind, where in sorted(set(before) - set(after))]
    lines += [
        f"缺口: 变了 {kind}:{where}: {before[(kind, where)]} → {after[(kind, where)]}"
        for kind, where in sorted(set(before) & set(after))
        if before[(kind, where)] != after[(kind, where)]
    ]
    return lines


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _short(value: Any) -> str:
    written = _canonical(value)
    return written if len(written) <= 80 else written[:77] + "…"


# ------------------------------------------------- 内部:清单视图与缺口 ----


class _ManifestView:
    """清单的**只读视图**:目录要在一次运行里把所有缺口都报出来,所以不用 parse_manifest
    (它遇到第一处错就抛)。这里只读字段、只判引用,不装配任何东西。"""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw
        self.models: Mapping[str, Mapping[str, Any]] = raw.get("models") or {}
        self.devices: tuple[Mapping[str, Any], ...] = tuple(raw.get("devices") or ())
        self.profiles: Mapping[str, Any] = raw.get("criteria_profiles") or {}
        self.setting_cases: Mapping[str, Mapping[str, Any]] = raw.get("setting_cases") or {}
        self.matrix: Mapping[str, Any] = raw.get("test_matrix") or {}

    # --- 结构引用 ---
    def structure_gaps(self) -> list[dict]:
        gaps = []
        for spec in self.devices:
            device_id = spec.get("id")
            model_name = spec.get("model")
            if model_name not in self.models:
                gaps.append(
                    {
                        "kind": GAP_MISSING_MODEL,
                        "where": f"devices.{device_id}",
                        "detail": f"型号 {model_name!r} 不存在;已声明: {sorted(self.models)}",
                    }
                )
        for name, model in sorted(self.models.items()):
            criteria = model.get("criteria")
            if criteria is not None and criteria not in self.profiles:
                gaps.append(
                    {
                        "kind": GAP_MISSING_CRITERIA_PROFILE,
                        "where": f"models.{name}",
                        "detail": f"判据集 {criteria!r} 不存在;已声明: {sorted(self.profiles)}",
                    }
                )
        return gaps

    def setting_case_gaps(self) -> list[dict]:
        gaps = []
        for model_name, clauses in sorted(self.setting_cases.items()):
            if model_name not in self.models:
                gaps.append(
                    {
                        "kind": GAP_CASE_WITHOUT_MODEL,
                        "where": f"setting_cases.{model_name}",
                        "detail": f"型号 {model_name!r} 不存在;已声明: {sorted(self.models)}",
                    }
                )
        for model_name in sorted(self.matrix):
            if model_name not in self.models:
                gaps.append(
                    {
                        "kind": GAP_CASE_WITHOUT_MODEL,
                        "where": f"test_matrix.{model_name}",
                        "detail": f"型号 {model_name!r} 不存在;已声明: {sorted(self.models)}",
                    }
                )
        return gaps

    def setting_case_entries(self) -> dict[str, str]:
        """清单里声明的设置类条款:编号 → 条款 id(它们是"用例"的一部分,只是身份由清单给)。"""
        entries: dict[str, str] = {}
        for clauses in self.setting_cases.values():
            for case_id, body in (clauses or {}).items():
                entries[str(case_id)] = str((body or {}).get("clause") or "")
        return entries

    # --- 产物片段 ---
    def models_record(
        self, cases_by_type: Mapping[str, list[CaseEntry]], matrix: Mapping[str, Any]
    ) -> dict:
        records = {}
        for name, model in sorted(self.models.items()):
            model_type = str(model.get("type") or "")
            selected = _cases_for_model(cases_by_type.get(model_type, []), matrix.get(name))
            records[name] = {
                "type": model_type,
                "protocol": model.get("protocol"),
                "criteria": model.get("criteria"),
                "capabilities": sorted(model.get("capabilities") or ()),
                "reading_keys": sorted((model.get("point_table") or {}).keys()),
                "instances": sorted(
                    str(spec.get("id")) for spec in self.devices if spec.get("model") == name
                ),
                "cases": sorted({case.id for case in selected if case.id}),
                "clauses": sorted({case.clause for case in selected if case.clause}),
                "ci": self.ci_record(name, model),
            }
        return records

    def connection_hint(self, model_name: str) -> dict:
        return _connection_hint(self, model_name)

    def ci_record(self, name: str, model: Mapping[str, Any]) -> list[dict]:
        """型号的 CI 可跑性:每种允许的接入方式问一次模拟器能力声明(06 的口径)。"""
        protocol = str(model.get("protocol") or "")
        accesses = sorted(PROTOCOL_ACCESS.get(protocol, ()))
        entries = []
        for access in accesses:
            profile = transport_registry.simulator_profile(
                access, self.connection_hint(name)
            )
            entries.append(
                {
                    "access": access,
                    "runnable": bool(profile["runnable"]),
                    "faults": sorted(profile["faults"]),
                    "data_injection": bool(profile["data_injection"]),
                    "faults_are_static": bool(profile.get("faults_are_static", True)),
                }
            )
        return entries

    def devices_record(self) -> dict:
        records = {}
        for spec in sorted(self.devices, key=lambda item: str(item.get("id"))):
            device_id = str(spec.get("id"))
            model_name = str(spec.get("model"))
            records[device_id] = {
                "model": model_name,
                "role": spec.get("role", "dut"),
                "mode": spec.get("mode", "real"),
                "access": spec.get("access"),
            }
        return records

    def criteria_record(self) -> dict:
        records = {}
        for name in sorted(self.profiles):
            models = sorted(m for m, spec in self.models.items() if spec.get("criteria") == name)
            devices = sorted(
                str(spec.get("id")) for spec in self.devices if spec.get("model") in models
            )
            records[name] = {
                "quantities": [
                    str(entry.get("quantity"))
                    for entry in (self.profiles.get(name) or [])
                    if isinstance(entry, Mapping)
                ],
                "models": models,
                "devices": devices,
            }
        return records


def _connection_hint(manifest: "_ManifestView", model_name: str) -> dict:
    """用这个型号某台设备的 connection 问模拟器能力(有模拟寄存器就按它的)。

    只取"决定模拟器形态"的那三个键:目录问的是"这个型号能跑哪些模拟器",
    不是"这台设备的接线参数"。
    """
    for spec in manifest.devices:
        if spec.get("model") != model_name:
            continue
        connection = spec.get("connection") or {}
        if isinstance(connection, Mapping):
            return {
                key: connection[key]
                for key in ("simulated_registers", "simulated_frames", "simulated_response")
                if key in connection
            }
    return {}


def _cases_by_type(cases: Sequence[CaseEntry]) -> dict[str, list[CaseEntry]]:
    grouped: dict[str, list[CaseEntry]] = {}
    for case in cases:
        for device_type in case.device_types:
            grouped.setdefault(device_type, []).append(case)
    return grouped


def _cases_for_model(cases: Sequence[CaseEntry], matrix: Any) -> list[CaseEntry]:
    """矩阵有定义就按矩阵收窄(编号前缀 / tag),没有就不受限(漏测比多测危险)。"""
    if not matrix:
        return list(cases)
    selectors = matrix if isinstance(matrix, list) else []
    kept = []
    for case in cases:
        for written in selectors:
            text = str(written).strip()
            kind, _, value = text.partition(":")
            if not _:
                kind, value = "case", text
            if kind == "case" and (case.id or "").startswith(value):
                kept.append(case)
                break
            if kind == "tag" and value in case.tags:
                kept.append(case)
                break
    return kept


def _duplicate_gaps(cases: Sequence[CaseEntry], manifest: _ManifestView) -> list[dict]:
    """编号全局唯一:独立于 `--select`(目录扫源文件,不跑收集期)。"""
    seen: dict[str, list[str]] = {}
    for case in cases:
        if case.id is None:
            continue
        seen.setdefault(case.id, []).append(f"{case.file}:{case.line}")
    for case_id, clause_id in manifest.setting_case_entries().items():
        seen.setdefault(case_id, []).append("setting_cases")
    return [
        {
            "kind": GAP_DUPLICATE_CASE_ID,
            "where": case_id,
            "detail": f"出现在 {len(places)} 处: {', '.join(sorted(places))}",
        }
        for case_id, places in sorted(seen.items())
        if len(places) > 1
    ]


def _capability_gaps(manifest: _ManifestView) -> list[dict]:
    gaps = []
    for name, model in sorted(manifest.models.items()):
        point_keys = set((model.get("point_table") or {}).keys())
        absent = sorted(set(model.get("capabilities") or ()) - point_keys)
        if absent:
            gaps.append(
                {
                    "kind": GAP_CAPABILITY_NOT_IN_POINT_TABLE,
                    "where": f"models.{name}",
                    "detail": f"能力 {absent} 不在点表读数键 {sorted(point_keys)} 里",
                }
            )
    return gaps


def _quantity_gaps(manifest: _ManifestView) -> list[dict]:
    """判据量必须落在这台型号真能判的量里。

    能判的量是并集:点表读数键 + 驱动自述的"读数键 → 域量名"映射(inverter/battery 把
    `有功功率` 记成 `output_power_w`)。映射由驱动在注册表里声明(见 drivers/registry.py),
    目录不 import 具体驱动,也不另抄一份 —— 这样"判据量不在点表"不会把正常的域量误报成缺口。
    """
    from testplatform.drivers.registry import judged_quantities

    gaps = []
    for name, model in sorted(manifest.models.items()):
        criteria = model.get("criteria")
        if criteria is None:
            continue
        point_keys = set((model.get("point_table") or {}).keys())
        mapping = judged_quantities(str(model.get("type")), str(model.get("protocol")))
        reachable = point_keys | set(mapping.values())
        absent = sorted(
            {
                str(entry.get("quantity"))
                for entry in (manifest.profiles.get(criteria) or ())
                if isinstance(entry, Mapping)
            }
            - reachable
        )
        if absent:
            gaps.append(
                {
                    "kind": GAP_QUANTITY_NOT_IN_POINT_TABLE,
                    "where": f"models.{name}",
                    "detail": (
                        f"判据集 {criteria!r} 判的量 {absent} 既不在点表读数键 "
                        f"{sorted(point_keys)} 里,也没有驱动映射到它"
                    ),
                }
            )
    return gaps


def _simulator_gaps(manifest: _ManifestView) -> list[dict]:
    gaps = []
    for name, model in sorted(manifest.models.items()):
        protocol = str(model.get("protocol"))
        accesses = sorted(PROTOCOL_ACCESS.get(protocol, ()))
        runnable = any(
            transport_registry.simulator_profile(
                access, _connection_hint(manifest, name)
            )["runnable"]
            for access in accesses
        )
        if not runnable:
            gaps.append(
                {
                    "kind": GAP_MODEL_WITHOUT_SIMULATOR,
                    "where": f"models.{name}",
                    "detail": (
                        f"规约 {protocol!r} 允许的接入方式 {accesses} 都没有注册模拟器:"
                        f"这个型号在 CI 上跑不了"
                    ),
                }
            )
    return gaps


def _clauses_record(registry, cases: Mapping[str, dict]) -> dict:
    records = {}
    for clause_id in registry.ids():
        clause = registry.get(clause_id)
        records[clause_id] = {
            "standard": clause.standard,
            "number": clause.number,
            "summary": clause.summary,
            "cases": sorted(
                case_id
                for case_id, record in cases.items()
                if record["clause"] == clause_id
            ),
            # 非空 = 这条条款经确认验不了(理由原样带出);目录据此把它算"已知豁免"而不是缺口
            "not_applicable": clause.not_applicable,
        }
    return records


def _waived_clauses(registry) -> list[dict]:
    """清单里标了 not_applicable 的条款:验不了,但**明说**,而且理由必须在产物里。"""
    return [
        {
            "where": clause_id,
            "reason": registry.get(clause_id).not_applicable,
        }
        for clause_id in registry.ids()
        if registry.get(clause_id).not_applicable
    ]


def _clause_coverage_gaps(clauses: Mapping[str, dict]) -> list[dict]:
    """零覆盖 = 缺口 —— 除非清单里明确写了"这条本平台验不了"(那就是已知豁免)。"""
    return [
        {
            "kind": GAP_CLAUSE_WITHOUT_CASES,
            "where": clause_id,
            "detail": f"条款 {clause_id} 没有任何用例引用它(标准 {body['standard']} {body['number']})",
        }
        for clause_id, body in sorted(clauses.items())
        if not body["cases"] and not body.get("not_applicable")
    ]


def _read_yaml(path: str | Path) -> Mapping[str, Any]:
    loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise ValueError(f"清单顶层必须是映射: {path}")
    return loaded


def default_devices_path() -> Path:
    """出厂清单:走 01b 的平台根解析(与执行器、条款清单同一个来源)。"""
    return paths.resolve_root(required=("configs/devices.yaml",)) / "configs" / "devices.yaml"
