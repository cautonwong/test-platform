"""把上游 xlsx 的结构化表单解析成中间产物,再按对照表生成 setting_cases 与覆盖率报表。

上游 ``docs/表计功能测试用例V3.2.1（20241108F）.xlsx`` 是一张**结构化表单**,不是自由文本:
每个 sheet 都是"标签列 + 步骤子标签列 + 取值列 + 预期结果列"。列数每个 sheet 不同(5–40 列),
所以列位置**按表头标签定位**,不写死列号;步骤用 ``步骤N`` 标签切片,并保留原始行号。

四份人工维护的对照表在 ``configs/import/``:
  ops.yaml     操作类型(67 种实测取值)→ 8 个原子动作;没有的取值进"映射缺口",不默认记 none
  params.yaml  对象参数名 → 台架量 / 点表读数键(+ "300bps" → 300 这样的取值翻译)
  expects.yaml 预期结果短语 → 判据类型(等于写入值 / 读到了)
  symbols.yaml 符号值(Un / In / Hz)→ 型号参数名;值从型号声明 + 设备实例上取,没有就拒收
猜错一个量名就是静默假读数,所以四份表都在导入时校验:拼错动作名、缺 values、未知 kind 直接报错;
参数名与点表读数键不同名的,没有 synonyms 显式声明同义也直接报错(名字不同就是另一个量)。

条款落进三档:完整(所有步骤都能落进 setting_cases)/ 部分(有步骤落不进)/ 不可生成(对照表缺)。
只有完整的条款进试点:少一半步骤也绿的用例,读报告的人分不出它验了半个还是全个。

产物(全部可 diff、可重复:同一份 xlsx 跑两次逐字节相同):
  build/cases.json                     解析结果 + 原始行号
  build/coverage.md                    覆盖率报表:三档计数 / 映射缺口(按档分开)/ 结构异常 / 试点
  configs/setting_cases_pilot.yaml     试点条款(清单片段,不并入出厂 configs/devices.yaml)
  build/pilot_devices.yaml             configs/devices.yaml + 试点条款,给 pytest --device-config 用
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

REPO = Path(__file__).resolve().parents[1]
XLSX = REPO / "docs" / "表计功能测试用例V3.2.1（20241108F）.xlsx"
IMPORT_DIR = REPO / "configs" / "import"
DEVICES = REPO / "configs" / "devices.yaml"
PILOT_CASES = REPO / "configs" / "setting_cases_pilot.yaml"
BUILD = REPO / "build"

# 8 个原子动作。清单里的 setting_cases 只能表达 write(写 → 读回 → 判相等)与 manual,
# 其余动作留在中间产物里,供场景层与报表区分"能自动 / 要人做 / 要台架"。
ACTIONS = ("write", "read", "bench", "wait", "manual", "exec", "none", "loop")

# 上游表单的标签。列位置按这些标签找,不按列号。
STEP_LABEL = re.compile(r"^步骤\s*\d+$")
CASE_LABEL = "用例标题"
KEY_POINT_LABEL = "要点"
STEP_TABLE_LABEL = "步骤编号"
EXPECTED_LABEL = "预期结果"
SUB_LABELS = ("操作描述", "操作类型", "对象参数")
# 条款编号是标题的第一个词(M_00_00_13_000101_01)。标题里没有它就没法当证据身份。
CASE_ID = re.compile(r"^M_[0-9A-Fa-f_]+$")

# 上游表单没有"标准条款"这一列。试点沿用手写样板用过的条款:参数设置与回读。
DEFAULT_CLAUSE = "DLT645-4.5.2"
# 对象参数写 "/" 表示这一步没有参数(不是"一个叫 / 的参数")。
NO_PARAM = "/"
# 设备实例上的占位哨兵:型号声明了参数,实例上还没填铭牌值。它不是 0,也不是默认值。
PLACEHOLDER_VALUE = "TODO"
MAPPING_TOP = 50
# 试点要补的名字只列最靠前的几个:报表的作用是让人知道下一步补什么,不是全量清单。
PILOT_GAP_TOP = 10


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    _utf8_stdout()
    tables = load_tables(IMPORT_DIR, model=args.model)
    workbook = read_workbook(XLSX, args.sheet)
    payload = {"source": {"file": _rel(XLSX), "sheets": [s["name"] for s in workbook]}, "sheets": workbook}
    _write_text(args.json, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    reports = [coverage_report(sheet, tables) for sheet in workbook]
    # 试点只从显式点名的 sheet 生成:不带 --sheet 是"全量报表"跑法,不该顺手改写
    # 已进版本库的 configs/setting_cases_pilot.yaml。
    pilot = pilot_cases(workbook, tables, limit=args.pilot_limit) if args.sheet else None
    _write_text(args.coverage, render_coverage(workbook, reports, tables, pilot))
    if pilot is not None:
        _write_text(PILOT_CASES, render_pilot(pilot, tables, source_workbook=workbook))
        _write_text(_pilot_manifest_path(), render_pilot_manifest(pilot))

    for report in reports:
        print(
            f"{report['sheet']}: 条款 {report['total']} 条 | "
            f"完整 {len(report['complete'])} | 部分 {len(report['partial'])} | "
            f"映射缺口 {len(report['gaps'])} | 结构异常 {len(report['structural'])}"
        )
    if pilot is None:
        print("试点清单没生成(没有 --sheet):全量跑法只出中间产物与报表")
    else:
        print(f"试点 setting_cases {len(pilot['cases'])} 条(model {pilot['model']})"
              + ("" if pilot["cases"] else ":没有一条完整条款,试点为空(缺口见报表)"))
    print(f"产物: {_rel(args.json)} / {_rel(args.coverage)}" + (
        "" if pilot is None else f" / {_rel(PILOT_CASES)} / {_rel(_pilot_manifest_path())}"))
    return 0


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="xlsx 条款 → 中间产物 + 覆盖率报表 + 试点清单")
    parser.add_argument("--sheet", action="append", help="只跑这些 sheet(可重复);缺省跑全部")
    parser.add_argument("--model", default="dtsd1352_modbus", help="试点条款落在哪个型号的点表上")
    parser.add_argument("--pilot-limit", type=int, default=10, help="试点最多生成几条条款")
    parser.add_argument("--json", default=str(BUILD / "cases.json"), help="中间产物路径")
    parser.add_argument("--coverage", default=str(BUILD / "coverage.md"), help="覆盖率报表路径")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- 对照表


def load_tables(directory: Path, model: str) -> Dict[str, Any]:
    """四份对照表 + 试点型号的可写点 / 设备参数 / 实例上给的值。拼错一个键就在这里报错,不留到生成时静默跳过。"""
    ops = _load_yaml(directory / "ops.yaml")
    _validate_ops(ops, directory / "ops.yaml")
    params = _load_yaml(directory / "params.yaml")
    _validate_params(params, directory / "params.yaml")
    expects = _load_yaml(directory / "expects.yaml")
    _validate_expects(expects, directory / "expects.yaml")
    symbols = _load_yaml(directory / "symbols.yaml")
    _validate_symbols(symbols, directory / "symbols.yaml")
    manifest = yaml.safe_load(DEVICES.read_text(encoding="utf-8")) or {}
    writable = _writable_points(manifest, model)
    declared, instance_values = _model_parameters(manifest, model)
    for name, mapping in params.items():
        if mapping["device"] != "dut":
            continue
        if mapping["key"] not in writable:
            raise KeyError(
                f"{directory / 'params.yaml'}: 参数 {name!r} 映射到型号 {model!r} 的读数键 "
                f"{mapping['key']!r},但那个型号没有它或没声明 writable;可写点: {sorted(writable)}"
            )
    return {
        "ops": {op: action for action, names in ops.items() for op in names},
        "params": params,
        "expects": expects,
        "symbols": symbols,
        "model": model,
        "writable": writable,
        "device_params": declared,
        "instance_values": instance_values,
    }


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 必须是一层映射,拿到 {data!r}")
    return data


def _validate_ops(ops: Mapping[str, Any], path: Path) -> None:
    unknown = set(ops) - set(ACTIONS)
    if unknown:
        raise KeyError(f"{path}: 动作名只能是 {list(ACTIONS)},多出来 {sorted(unknown)}")
    missing = set(ACTIONS) - set(ops)
    if missing:
        raise KeyError(f"{path}: 每个动作都要有一行(可以是空列表),缺 {sorted(missing)}")
    seen: Dict[str, str] = {}
    for action, names in ops.items():
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise ValueError(f"{path}: {action} 必须是非空字符串列表,拿到 {names!r}")
        for name in names:
            if name in seen:
                raise ValueError(f"{path}: 操作类型 {name!r} 同时写在 {seen[name]} 和 {action} 下")
            seen[name] = action


def _validate_params(params: Mapping[str, Any], path: Path) -> None:
    for name, mapping in params.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: 参数名必须是非空字符串,拿到 {name!r}")
        if not isinstance(mapping, dict) or "device" not in mapping:
            raise ValueError(f"{path}: 参数 {name!r} 必须写成映射且带 device: {mapping!r}")
        device = mapping["device"]
        if device not in ("dut", "bench"):
            raise ValueError(f"{path}: 参数 {name!r} 的 device 只能是 dut / bench,拿到 {device!r}")
        allowed = (
            {"device", "quantity", "unit", "values"}
            if device == "bench"
            else {"device", "key", "values", "synonyms"}
        )
        unknown = set(mapping) - allowed
        if unknown:
            raise KeyError(f"{path}: 参数 {name!r} 有未知键 {sorted(unknown)},可用: {sorted(allowed)}")
        required = ("quantity",) if device == "bench" else ("key", "values")
        for key in required:
            if key not in mapping:
                raise KeyError(f"{path}: 参数 {name!r} 缺 {key}(device: {device})")
        if device == "dut" and not isinstance(mapping["values"], dict):
            raise ValueError(
                f"{path}: 参数 {name!r} 的 values 必须是映射 \"源文件取值\" → 数字"
                f"(源文件写 300bps,点表要 300);拿到 {mapping['values']!r}"
            )
        if device == "dut":
            _validate_synonyms(name, mapping, path)
        else:
            _validate_bench_values(name, mapping, path)


def _validate_bench_values(name: str, mapping: Mapping[str, Any], path: Path) -> None:
    """台架量的取值对照(可选):源文件写的名字 → 台架要的整数。

    枚举类台架量(接线方式)不是模拟量,源文件写 "3P4W",台架要一个整数。名字 → 整数
    这一列与寄存器地址是同一类外部输入,没拿到之前写 null(未知),不能拿 0 顶 ——
    0 是一个真取值的候选,而 null 在报表里是一行看得见的“映射缺口”。
    """
    values = mapping.get("values")
    if values is None:
        return
    if not isinstance(values, dict) or not values:
        raise ValueError(
            f"{path}: 参数 {name!r} 的 values 必须是非空映射 \"源文件取值\" → 整数或 null,拿到 {values!r}"
        )
    for raw, code in values.items():
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"{path}: 参数 {name!r} 的 values 的键是源文件里的写法,必须是非空字符串,拿到 {raw!r}")
        if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
            raise ValueError(
                f"{path}: 参数 {name!r} 的取值 {raw!r} 必须对应一个整数编码或 null"
                f"(枚举不是模拟量),拿到 {code!r}"
            )


def _validate_synonyms(name: str, mapping: Mapping[str, Any], path: Path) -> None:
    """参数名与点表读数键不同名时,必须有显式同义声明。

    没有这道校验,把"红外通信波特率"指到"有功计量模式"上也照样生成用例:写进去再读回来
    当然一致,但验的不是被测条款 —— 比没做更糟,它让人相信覆盖到了。
    """
    synonyms = mapping.get("synonyms") or []
    if not isinstance(synonyms, list) or not all(isinstance(item, str) and item for item in synonyms):
        raise ValueError(f"{path}: 参数 {name!r} 的 synonyms 必须是非空字符串列表,拿到 {synonyms!r}")
    if name != mapping["key"] and name not in synonyms:
        raise KeyError(
            f"{path}: 参数 {name!r} 映射到读数键 {mapping['key']!r},两个名字不同却没有同义声明: "
            f"要么把条目名改成 {mapping['key']!r},要么加 synonyms: [{name!r}] 并写一句为什么是同一个量"
        )


def _validate_symbols(symbols: Mapping[str, Any], path: Path) -> None:
    """符号表:符号 → 型号参数名 + 出厂处的上游行。

    没有 fallback 这一档:型号没声明参数就该拒收,拿 220 / 50 兜底是猜额定值 ——
    台架会收到一个看起来正常的数,错了也看不出来。未知键(比如 fallback / value)
    在这里直接报错,写不进来。
    """
    allowed = {"param", "unit", "source", "expect", "note"}
    for name, entry in symbols.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: 符号必须是非空字符串,拿到 {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: 符号 {name!r} 必须写成映射且带 param / source,拿到 {entry!r}")
        unknown = set(entry) - allowed
        if unknown:
            raise KeyError(f"{path}: 符号 {name!r} 有未知键 {sorted(unknown)},可用: {sorted(allowed)}")
        for key in ("param", "source", "expect"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise KeyError(
                    f"{path}: 符号 {name!r} 缺 {key} —— 符号要指到型号参数名,并给出上游那一行"
                )
        if not re.fullmatch(r".+:\d+", entry["source"]):
            raise ValueError(
                f"{path}: 符号 {name!r} 的 source 必须写成 <相对路径>:<行号>,拿到 {entry['source']!r}"
            )


def _validate_expects(expects: Mapping[str, Any], path: Path) -> None:
    kinds = {"equals", "exists"}
    for phrase, rule in expects.items():
        if not isinstance(phrase, str) or not phrase:
            raise ValueError(f"{path}: 预期结果短语必须是非空字符串,拿到 {phrase!r}")
        if not isinstance(rule, dict) or rule.get("kind") not in kinds:
            raise ValueError(f"{path}: {phrase!r} 的 kind 只能是 {sorted(kinds)},拿到 {rule!r}")
        unknown = set(rule) - {"kind", "compare"}
        if unknown:
            raise KeyError(f"{path}: {phrase!r} 有未知键 {sorted(unknown)}")
        if rule["kind"] != "equals" and "compare" in rule:
            raise ValueError(f"{path}: {phrase!r} 只有 equals 才有 compare")
        if rule.get("compare") not in (None, "written_value"):
            raise ValueError(f"{path}: {phrase!r} 的 compare 只能是 written_value,拿到 {rule['compare']!r}")


def _writable_points(config: Mapping[str, Any], model: str) -> Dict[str, Any]:
    """试点型号能不能写下这个键:装配期校验同一条规则(点表里必须 writable: true)。"""
    models = config.get("models") or {}
    if model not in models:
        raise KeyError(f"{_rel(DEVICES)}: 没有型号 {model!r},已声明: {sorted(models)}")
    points = models[model].get("point_table") or {}
    writable = {key: point for key, point in points.items() if point.get("writable")}
    if not writable:
        raise KeyError(f"{_rel(DEVICES)} 型号 {model!r} 没有可写点,试点条款没地方落")
    return writable


def _model_parameters(
    config: Mapping[str, Any], model: str
) -> Tuple[frozenset, Dict[str, set]]:
    """型号声明的设备参数 + 同型号设备实例上给的那些值(可能一台都没给)。

    符号值(Un / Hz)在导入期就要落成一个数,而它既不是常数也不是型号自己能回答的:
    型号说"我有 rated_voltage_v",值写在每台设备实例上。一台都没给、或者几台给的值不一致,
    就落不成一个数(条款里只有一列值)—— 挑第一台等于替人定额定值。
    """
    declared = frozenset(((config.get("models") or {}).get(model) or {}).get("device_params") or ())
    given: Dict[str, set] = {}
    for device in config.get("devices") or []:
        if device.get("model") != model:
            continue
        for name in declared:
            if name in device:
                given.setdefault(name, set()).add(device[name])
    return declared, given


# ---------------------------------------------------------------- 解析


def read_workbook(path: Path, sheets: Optional[List[str]]) -> List[Dict[str, Any]]:
    """按标签定位列,把每个 sheet 切成"条款 → 步骤";列位置与行号都留在结果里。"""
    try:
        import openpyxl
    except ModuleNotFoundError as error:  # pragma: no cover - 环境问题,不是分支逻辑
        raise SystemExit(
            "需要 openpyxl 读 xlsx:换一个装了它的解释器跑,例如 "
            f"{sys.executable} tools/import_cases.py"
        ) from error
    if not path.exists():
        raise SystemExit(f"找不到上游表单: {path}")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = list(workbook.sheetnames)
    unknown = set(sheets or []) - set(names)
    if unknown:
        raise SystemExit(f"没有这些 sheet: {sorted(unknown)};实际有: {names}")
    selected = sheets or names
    return [parse_sheet(workbook[name]) for name in selected if _has_cases(workbook[name])]


def _has_cases(sheet) -> bool:
    """版本说明不是条款表,没有 用例标题 标签:跳过而不是硬解析出一堆垃圾。"""
    try:
        _locate_columns(sheet)
    except ValueError:
        return False
    return True


def parse_sheet(sheet) -> Dict[str, Any]:
    layout = _locate_columns(sheet)
    cases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    step: Optional[Dict[str, Any]] = None
    for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        cells = {column: _text(value) for column, value in enumerate(row, start=1)}
        label = cells.get(layout["label"]) if layout["label"] else None
        if label == CASE_LABEL:
            title = cells.get(layout["title"]) or ""
            current, step = _new_case(row_index, title), None
            cases.append(current)
            continue
        if label and STEP_LABEL.match(label):
            step = {"index": label, "row": row_index, "description": None, "op": None,
                    "params": [], "expected": None}
            if current is not None:
                current["steps"].append(step)
            # 步骤标题行本身也可能带内容:上游常把"预期结果"写在这一行(而不是子标签行)。
            # 整行 continue 会把它连同行上的操作描述一起丢掉,下游于是把它报成 no_expected
            # —— 让工程师去改一个没错的地方。这一行的非空内容必须参与解析。
            if step is not None:
                expected_on_title = cells.get(layout["expected"])
                if expected_on_title is not None:
                    step["expected"] = expected_on_title
                if step["description"] is None:
                    described = cells.get(layout["value"])
                    if described is not None:
                        step["description"] = described
            continue
        if current is None:
            continue
        # 合并单元格会把子标签吃掉:一个步骤跨多行时,续行只有取值列有内容。
        # 操作类型已经出现之后,不带子标签的续行就是对象参数(上游常常省掉"对象参数"三字,
        # 或者 C 列与上一行纵向合并)。不能按"沿用上一行"处理 —— 上一行是操作类型,
        # 那样会把 "通信模式=HDLC" 读成操作类型。
        sub = cells.get(layout["sublabel"])
        if sub is None and step is not None and step["op"] is not None:
            sub = "对象参数"
        if sub == "操作描述" and step is not None:
            step["description"] = cells.get(layout["value"])
        elif sub == "操作类型" and step is not None:
            step["op"] = cells.get(layout["value"])
        elif sub == "对象参数" and step is not None:
            value = cells.get(layout["value"])
            if value is not None:
                step["params"].append(value)
        if step is not None:
            expected = cells.get(layout["expected"])
            if expected is not None:
                step["expected"] = expected
        stray = _stray_cells(cells, layout)
        if stray:
            current["stray"] = current.get("stray", []) + [
                {"row": row_index, "cells": stray}
            ]
    return {"name": sheet.title, "columns": layout, "cases": cases}


def _locate_columns(sheet) -> Dict[str, Any]:
    """标签列 / 子标签列 / 取值列 / 预期结果列:全部从表头找,不写死列号。"""
    label_column = None
    expected_column = None
    for row in sheet.iter_rows(min_row=1, max_row=20, values_only=True):
        for column, value in enumerate(row, start=1):
            text = _text(value)
            if text == CASE_LABEL and label_column is None:
                label_column = column
            if text == EXPECTED_LABEL and expected_column is None:
                expected_column = column
    if label_column is None:
        raise ValueError(f"sheet {sheet.title!r} 里没有 {CASE_LABEL!r} 标签,认不出表格布局")
    return {
        "label": label_column,
        "title": label_column + 1,
        "sublabel": label_column + 1,
        "value": label_column + 2,
        "expected": expected_column or label_column + 3,
    }


def _stray_cells(cells: Mapping[int, Optional[str]], layout: Mapping[str, int]) -> Dict[int, str]:
    """表格四列以外的内容(07事件 40 列里的附件名 / 备注):进"结构异常",不当步骤读。"""
    known = set(layout.values())
    return {
        column: text
        for column, text in cells.items()
        if text is not None and column not in known and column > layout["expected"]
    }


def _new_case(row_index: int, title: str) -> Dict[str, Any]:
    first = title.split()[0] if title.split() else ""
    return {
        "case_id": first if CASE_ID.match(first) else None,
        "title": title,
        "row": row_index,
        "steps": [],
    }


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------- 覆盖率


def coverage_report(sheet: Mapping[str, Any], tables: Mapping[str, Any]) -> Dict[str, Any]:
    """一条条款落进一档:结构异常 / 映射缺口 / 部分 / 归场景层 / 完整(可进试点)。

    先按 ADR 0006 分流(route_of):静态台架前置 + 写/人工 → consistency;被观测的台架动作 →
    场景层,不计入可生成数。分流结果写进 report["routes"],每条条款都标出来 —— 静默二选一
    正是本票要防的:读报表的人必须看得出这 105 条里哪几条进了 setting_cases、哪些归了场景层。
    """
    report = {
        "sheet": sheet["name"],
        "total": len(sheet["cases"]),
        "complete": [], "partial": [], "gaps": [], "structural": [],
        "scenario": [], "routes": [], "bench": {}, "gap_kinds": {},
    }
    seen_ids: Dict[str, int] = {}
    for case in sheet["cases"]:
        if case["case_id"]:
            seen_ids[case["case_id"]] = seen_ids.get(case["case_id"], 0) + 1
    for case in sheet["cases"]:
        route, reason = route_of(case, tables)
        report["routes"].append((case, route, reason))
        anomalies = structural_anomalies(case, seen_ids, tables)
        if anomalies:
            report["structural"].append((case, anomalies))
            continue
        if route == SCENARIO_ROUTE:
            report["scenario"].append((case, reason))
            continue
        gaps = mapping_gaps(case, tables)
        if gaps:
            report["gaps"].append((case, gaps))
            for gap in gaps:
                kind = gap.split(":", 1)[0]
                report["gap_kinds"][kind] = report["gap_kinds"].get(kind, 0) + 1
            continue
        dropped = unlandable_steps(case, tables)
        if dropped:
            report["partial"].append((case, dropped))
            continue
        report["complete"].append(case)
        if case["case_id"]:
            report["bench"][case["case_id"]] = bench_prefix(case, tables)
    report["unmapped"] = _unmapped_names(sheet, tables)
    return report


def unlandable_steps(case: Mapping[str, Any], tables: Mapping[str, Any]) -> Dict[str, int]:
    """落不进 setting_cases 的步骤,按动作类型计数。空字典 = 这条条款的步骤都能落进去。

    清单只能表达写→读回→判等与人工占位。读 / 延时 / 循环步骤落不进去;人工步骤能写进
    清单但跑起来是 skip,不算试绿。有这些步骤的条款只是上游条款的子集,不叫可生成。
    台架步骤不算落不进去:它是前置,要么进 bench: 字段,要么把整条条款推到场景层(见 route_of)。
    """
    dropped: Dict[str, int] = {}
    for step in case["steps"]:
        kind = tables["ops"].get(step["op"], step["op"])
        if kind == "bench":
            continue
        if kind != "write" or _write_steps(step, tables) is None:
            dropped[kind] = dropped.get(kind, 0) + 1
    return dropped


CONSISTENCY_ROUTE = "consistency"
SCENARIO_ROUTE = "scenario"
UNLANDABLE_ROUTE = "落不进 setting_cases"
# 这些动作是在观测结果:与台架动作同在一条条款里就是因果链(把台架调到 X → 读表 → 判 Y)。
OBSERVING_ACTIONS = ("read", "wait", "exec", "loop")


def route_of(case: Mapping[str, Any], tables: Mapping[str, Any]) -> Tuple[str, str]:
    """按 ADR 0006 把条款分流:静态台架前置 + 写/人工 → consistency;被观测的台架动作 → 场景层。

    判定显式而窄:
      - 条款里不止一次调台体(先 Ua=Un 再 Ua=0)→ 台架设定是被测行为,归场景层;
      - 台架动作后面还有读 / 延时 / 执行 / 循环 → 因果链,归场景层;
      - 没有台架动作、却有这些观测动作 → 落不进 setting_cases(也不是场景);
      - 其余(写 / 人工 + 最多一段台架前置)→ consistency。
    返回 (归属, 理由)。
    """
    kinds = [tables["ops"].get(step["op"], step["op"]) for step in case["steps"]]
    bench = [step for step, kind in zip(case["steps"], kinds) if kind == "bench"]
    observing = [
        step for step, kind in zip(case["steps"], kinds) if kind in OBSERVING_ACTIONS
    ]
    reasons = []
    if len(bench) > 1:
        reasons.append(f"条款中途多次调台体({', '.join(s['index'] for s in bench)})")
    if bench and observing:
        reasons.append(
            f"台架设定被观测({', '.join(s['index'] for s in observing)} 跟在台架动作后面)"
        )
    if reasons:
        return SCENARIO_ROUTE, "; ".join(reasons)
    if observing:
        return UNLANDABLE_ROUTE, (
            f"含 setting_cases 表达不了的步骤({', '.join(s['index'] for s in observing)})"
        )
    prefix = f" + 台架前置({', '.join(s['index'] for s in bench)})" if bench else ""
    return CONSISTENCY_ROUTE, f"只有写 / 人工步骤{prefix}"


def bench_prefix(case: Mapping[str, Any], tables: Mapping[str, Any]) -> Dict[str, float]:
    """条款里那一段台架前置 → {台架点表的读数键: 值}。

    量名与取值都过 configs/import/params.yaml,落不进去的那些已经在 mapping_gaps 里点过名;
    这里只收能落数的那些,给生成的条款写成 bench:。
    """
    prefix: Dict[str, float] = {}
    for step in case["steps"]:
        if tables["ops"].get(step["op"]) != "bench":
            continue
        for param in step["params"]:
            name, raw = split_param(param)
            mapping = tables["params"].get(name or "")
            if mapping is None or mapping["device"] != "bench" or raw is None:
                continue
            value, _, _ = bench_value(mapping, raw, tables)
            if value is not None:
                prefix[mapping["quantity"]] = value
    return prefix


def bench_value(
    mapping: Mapping[str, Any], raw: str, tables: Mapping[str, Any]
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """源文件写的台架取值 → (数, 缺口类别, 原因)。落不进去时第一个是 None。

    枚举类台架量(接线方式)的对照在 params.yaml 的 values 里:名字不在表里就是未知取值,
    值是 null 就是编码还没拿到 —— 两种都不是 0,编一个整数和编一个寄存器地址是同一种假绿。
    模拟量直接读字面数:源文件写 "0°" / "120°",单位就是 params.yaml 里的 unit。
    读不成数的是符号值(Un / Hz):按 symbols.yaml 指到型号参数,值从型号声明 + 设备实例上取。
    符号认不出、型号没声明参数、实例没给值三种情况分开记 —— 最后两种是可以直接改成动作的拒收。
    """
    values = mapping.get("values")
    if values is not None:
        if raw not in values:
            return None, "bench_enum", f"未知取值(可用: {sorted(values)})"
        code = values[raw]
        if code is None:
            return None, "bench_enum", "整数编码未定(params.yaml 里这一档还是 null,不拿 0 顶)"
        return float(code), None, None
    unit = mapping.get("unit")
    spellings = [raw]
    if unit and raw.endswith(unit):
        spellings.append(raw[: -len(unit)])
    for spelling in spellings:
        try:
            return float(spelling), None, None
        except ValueError:
            continue
    # 符号查表要在剥掉单位**之后**也查一次:Hz 本身既是单位也是符号(Fre=Hz)。
    for spelling in spellings:
        entry = tables["symbols"].get(spelling)
        if entry is not None:
            return _symbol_value(entry, spelling, tables)
    return None, "bench_symbol", (
        "不是数值,也不是 symbols.yaml 里的符号(符号值要有人给出它对应哪个型号参数)"
    )


def _yaml_example(model: str, param: str) -> str:
    """一行能直接粘进清单的示例:只给形状,不给数字。

    额定值是铭牌事实,示例里抄一个 220,下一个人就照着填而不看表 —— 台架按错的额定值跑,
    报表里一切正常。所以占位写成 `<铭牌上的数>`。
    """
    return f"- {{id: meter_01, model: {model}, {param}: <铭牌上的数>}}"


def _symbol_value(
    entry: Mapping[str, Any], symbol: str, tables: Mapping[str, Any]
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """符号 → 型号参数 → 设备实例上的值。缺哪一层都拒收,并在消息里说清楚加在哪。

    消息不给具体的示例值(不写 220 / 50):额定值是型号事实,抄一个数字进 device_params
    比拒收更危险 —— 台架会收到一个能跑、但错了一倍的值。

    "没声明这个参数"与"声明了但值是 TODO"分两句:前者要改型号,后者只要把占位换成铭牌值,
    混成一句会让读报表的人去改错地方。
    """
    param = entry["param"]
    unit = f",单位 {entry['unit']}" if entry.get("unit") else ""
    model = tables["model"]
    example = _yaml_example(model, param)
    if param not in tables["device_params"]:
        return None, "bench_model_param", (
            f"型号 {model!r} 没有声明 {param},无法解析符号 {symbol!r};"
            f"请在型号的 device_params 里加上它,并在设备实例上给值(按型号实际额定值{unit});"
            f"示例: {example}"
        )
    given = tables["instance_values"].get(param) or ()
    if not given:
        return None, "bench_model_param", (
            f"型号 {model!r} 缺参数 {param}:没有一台设备实例给它值,无法解析符号 {symbol!r};"
            f"请在设备实例上给 {param} 一个数(按型号实际额定值{unit});示例: {example}"
        )
    if len(given) > 1:
        return None, "bench_model_param", (
            f"型号 {model!r} 的设备实例上 {param} 取了不同的值 {sorted(given)},符号 {symbol!r} 落不成一个数;"
            f"请先统一额定值(一条条款里只有一列值)"
        )
    value = next(iter(given))
    try:
        return float(value), None, None
    except (TypeError, ValueError):
        pass
    if str(value).strip().upper() == PLACEHOLDER_VALUE:
        return None, "bench_model_param", (
            f"型号 {model!r} 缺参数 {param}:设备实例上还是 TODO 占位,没有实际额定值,"
            f"无法解析符号 {symbol!r};请把 TODO 换成铭牌上的数(不拿默认值兜底{unit});"
            f"示例: {example}"
        )
    return None, "bench_model_param", (
        f"型号 {model!r} 的设备实例上 {param} 不是数({value!r}),台架收不下去;示例: {example}"
    )


def structural_anomalies(
    case: Mapping[str, Any], seen_ids: Mapping[str, int], tables: Mapping[str, Any]
) -> List[str]:
    """结构异常:不是对照表能补的,是表格本身表达不了的。

    合并单元格本身**不**算异常:一个步骤跨多行(多个对象参数)是正常排版。
    真正要抓的是下面这些 —— 每一条都会让解析器读不到该读的东西。
    """
    found = []
    if not case["case_id"]:
        found.append(f"no_case_id: 标题里没有条款编号({case['title'][:40]!r})")
    elif seen_ids.get(case["case_id"], 0) > 1:
        found.append(f"duplicate_case_id: 编号 {case['case_id']} 在本 sheet 出现不止一次")
    if not case["steps"]:
        found.append("no_steps: 条款没有步骤行")
    for step in case["steps"]:
        where = f"{step['index']}@{step['row']}"
        if step["op"] is None:
            found.append(f"no_op_type: {where} 有操作描述但没有操作类型")
        if _judged(step["op"], tables) and step["expected"] is None:
            found.append(f"no_expected: {where} 是判定步({step['op']})但没有预期结果")
    for stray in case.get("stray", []):
        found.append(f"stray_cell: 第 {stray['row']} 行表格四列以外还有内容 {stray['cells']}")
    return found


def _judged(op: Optional[str], tables: Mapping[str, Any]) -> bool:
    """判定步要有预期结果:自动执行 + 操作类型自己写着"判断"两步都成立才算。

    两个反例都不是表格缺陷:
      人工操作/判断 —— 操作员看什么写在操作描述里,平台只留占位(见 pending_manual);
      写 / 读/记录   —— 只是动手,没有判断(进工厂模式、记录读数),预期结果本来就没有。
    """
    return (
        op is not None
        and tables["ops"].get(op) in ("write", "read")
        and ("判断" in op or "合格" in op)
    )


def mapping_gaps(case: Mapping[str, Any], tables: Mapping[str, Any]) -> List[str]:
    """对照表里找不到的东西:操作类型 / 对象参数名 / 台架取值 / 预期结果短语。"""
    gaps = []
    for step in case["steps"]:
        where = f"{step['index']}@{step['row']}"
        if step["op"] is None:
            continue
        if step["op"] not in tables["ops"]:
            gaps.append(f"op_type: {where} {step['op']!r} 不在 ops.yaml")
            continue
        kind = tables["ops"][step["op"]]
        if kind in ("write", "read"):
            for param in step["params"]:
                name = split_param(param)[0] or param
                if name != NO_PARAM and name not in tables["params"]:
                    gaps.append(f"param: {where} {name!r} 不在 params.yaml")
        if kind == "bench":
            gaps += _bench_gaps(step["params"], where, tables)
        if step["expected"] is not None and step["expected"] not in tables["expects"]:
            gaps.append(f"expect: {where} {step['expected'][:30]!r} 不在 expects.yaml")
    return gaps


def _bench_gaps(params: List[str], where: str, tables: Mapping[str, Any]) -> List[str]:
    """台架步骤的每个 量名=值:名字要在 params.yaml 里且是台架量,取值要落得成数。"""
    gaps = []
    for param in params:
        name, raw = split_param(param)
        mapping = tables["params"].get(name or "")
        if name is None:
            gaps.append(f"bench_param: {where} {param[:30]!r} 不是 量名=值")
        elif mapping is None:
            gaps.append(f"bench_param: {where} {name!r} 不在 params.yaml")
        elif mapping["device"] != "bench":
            gaps.append(
                f"bench_param: {where} {name!r} 在 params.yaml 里不是台架量(device: {mapping['device']})"
            )
        elif raw is None:
            gaps.append(f"bench_value: {where} {param[:30]!r} 缺取值")
        else:
            value, kind, why = bench_value(mapping, raw, tables)
            if value is None:
                gaps.append(f"{kind}: {where} {param[:30]!r} {why}")
    return gaps


def _unmapped_names(sheet: Mapping[str, Any], tables: Mapping[str, Any]) -> Dict[str, List[Tuple[str, int, Optional[str]]]]:
    """报表要的"前 50 个未映射名":从全部条款里数,包括已经归进结构异常的那些。

    名字 → (出现次数, 第一次见到的原因):台架取值那一档的原因本身就是要转给对方的动作
    (缺哪个参数、加到哪),报表里逐行带着它,读者不用再去翻条款。
    """
    counts: Dict[str, Dict[str, List[Any]]] = {
        "param": {}, "expect": {}, "op_type": {}, "bench_param": {},
        "bench_enum": {}, "bench_symbol": {}, "bench_model_param": {}, "bench_value": {},
    }

    def bump(kind: str, name: str, why: Optional[str] = None) -> None:
        entry = counts[kind].setdefault(name, [0, None])
        entry[0] += 1
        entry[1] = entry[1] or why

    for case in sheet["cases"]:
        for step in case["steps"]:
            op = step["op"]
            if op is not None and op not in tables["ops"]:
                bump("op_type", op)
            if op is not None and tables["ops"].get(op) in ("write", "read"):
                for param in step["params"]:
                    name = split_param(param)[0] or param
                    if name != NO_PARAM and name not in tables["params"]:
                        bump("param", name)
            if op is not None and tables["ops"].get(op) == "bench":
                for param in step["params"]:
                    name, raw = split_param(param)
                    mapping = tables["params"].get(name or "")
                    if name is None or mapping is None or mapping["device"] != "bench":
                        bump("bench_param", name or param)
                    elif raw is None:
                        bump("bench_value", param, "缺取值")
                    else:
                        _, kind, why = bench_value(mapping, raw, tables)
                        if kind is not None:
                            bump(kind, param, why)
            if step["expected"] is not None and step["expected"] not in tables["expects"]:
                bump("expect", step["expected"])
    return {
        kind: sorted(
            ((name, count, why) for name, (count, why) in values.items()),
            key=lambda item: (-item[1], item[0]),
        )[:MAPPING_TOP]
        for kind, values in counts.items()
    }


def op_coverage(sheets: List[Mapping[str, Any]], tables: Mapping[str, Any]) -> Dict[str, Any]:
    """归一化覆盖:在扫描到的取值里,有多少已经写进 ops.yaml。"""
    seen: Dict[str, int] = {}
    for sheet in sheets:
        for case in sheet["cases"]:
            for step in case["steps"]:
                if step["op"] is not None:
                    seen[step["op"]] = seen.get(step["op"], 0) + 1
    unmapped = sorted(name for name in seen if name not in tables["ops"])
    return {"seen": len(seen), "mapped": len(seen) - len(unmapped), "unmapped": unmapped}


def render_coverage(
    sheets: List[Mapping[str, Any]],
    reports: List[Mapping[str, Any]],
    tables: Mapping[str, Any],
    pilot: Optional[Mapping[str, Any]] = None,
) -> str:
    lines = ["# 覆盖率报表", "", f"来源: {_rel(XLSX)}", ""]
    for report in reports:
        consistency = len(report["routes"]) - len(report["scenario"]) - len(report["structural"])
        tiers = " / ".join(f"{label} {count} 处" for label, count in _gap_tiers(report["gap_kinds"]))
        lines += [
            f"## {report['sheet']}",
            "",
            "```",
            f"条款总数 {report['total']}",
            f"  分流:进 consistency {consistency} / 归场景层(不计入可生成数) {len(report['scenario'])}",
            f"  consistency 侧:完整(可生成) {len(report['complete'])} / 部分 {len(report['partial'])} / "
            f"不可生成(映射缺口 {len(report['gaps'])} 条)",
            f"  缺口分档: {tiers}",
            f"  结构异常(表格本身读不全,先修表再看分流) {len(report['structural'])}",
            "```",
            "",
        ]
        lines += _render_routes(report)
        lines += _render_partial(report)
        lines += _render_gaps(report)
        lines += _render_structural(report)
    lines += _render_op_coverage(sheets, tables)
    if pilot is not None:
        lines += _render_pilot_section(pilot)
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_routes(report: Mapping[str, Any]) -> List[str]:
    """逐条条款的归属(ADR 0006):进 consistency / 归场景层 / 落不进。

    这一节不许省:分流是判定,不是推导结果 —— 读报表的人要看得出哪几条进了 setting_cases、
    哪几条因为“台架设定被观测”而去了场景层,而不是只看到一个总数。
    """
    counts: Dict[str, int] = {}
    broken = {id(case) for case, _ in report["structural"]}
    for case, route, _ in report["routes"]:
        if id(case) not in broken:
            counts[route] = counts.get(route, 0) + 1
    lines = [
        "### 分流:每条条款归哪一边(ADR 0006)",
        "",
        "口径:只有写 / 人工步骤 + 一段静态台架前置 → 进 consistency(setting_cases,带 bench:);",
        "台架设定本身被观测(中途多次调台体、或台架动作后面跟着读 / 延时 / 执行)→ 归场景层,",
        "不计入可生成数。判定见 tools/import_cases.py::route_of。",
        "",
        "```",
    ]
    lines += [f"{route}  {count}" for route, count in sorted(counts.items())]
    if broken:
        lines.append(f"结构异常(先修表,不参与分流)  {len(broken)}")
    lines += ["```", ""]
    lines += [
        f"- {case['case_id'] or '(无编号)'}: {route} —— {reason}"
        + ("(表格有结构异常,先修表)" if id(case) in broken else "")
        for case, route, reason in report["routes"]
    ]
    return lines + [""]


def _render_partial(report: Mapping[str, Any]) -> List[str]:
    """部分可生成:缺的到底是哪几步,否则读者分不清它验了半个还是全个。"""
    lines = ["### 部分可生成:落不进 setting_cases 的步骤", ""]
    if not report["partial"]:
        return lines + ["（没有）", ""]
    reasons: Dict[str, int] = {}
    for _, dropped in report["partial"]:
        for kind, count in dropped.items():
            reasons[kind] = reasons.get(kind, 0) + count
    lines += [f"- {kind}: {count} 步" for kind, count in sorted(reasons.items(), key=lambda i: (-i[1], i[0]))]
    lines += ["", "清单只能表达 写 → 读回 → 判等 与人工占位;差步骤的条款不算可生成。"]
    return lines + [""]


def _render_gaps(report: Mapping[str, Any]) -> List[str]:
    """三类缺口分开显示:缺型号参数 / 符号值认不出 / 枚举与短语待确认。

    混成一档的话,读报表的人分不出"还差外部资料"和"就差一行 yaml":前者只能等,
    后者今天就能推。写入参数名另列一档 —— 它缺的是点表里的可写点,不是符号也不是枚举。
    """
    if not report["gaps"]:
        return ["### 映射缺口", "", "（没有）", ""]
    lines = ["### 映射缺口", "", "按档计数（一条条款可以同时欠几档）:", "", "```"]
    lines += [f"{label} {count} 处" for label, count in _gap_tiers(report["gap_kinds"])]
    lines += ["```", ""]
    for label, title, kinds, trailer in GAP_TIERS:
        lines += [f"#### {label}:{title}", ""]
        lines += [trailer, ""] if trailer else []
        for kind in kinds:
            lines += [f"- `{_short(name)}` × {count}" + (f" —— {why}" if why else "")
                      for name, count, why in report["unmapped"][kind]]
        if not any(report["unmapped"][kind] for kind in kinds):
            lines.append("（没有）")
        lines.append("")
    return lines


# 报表里的五档。明细按这个顺序出,计数也按这里归。
GAP_TIERS = (
    ("缺型号参数",
     "符号认得出,型号没声明它 —— 拒收,不拿 220 / 50 兜底",
     ("bench_model_param",),
     "每个人名后面那句话就是要做的动作:在型号的 device_params 里加参数、在设备实例上给值。"),
    ("符号值认不出",
     "不是数,也不是 symbols.yaml 里的符号",
     ("bench_symbol",),
     "要么是写法变体、要么是表达式:得有人先说清它指哪个量,再往 symbols.yaml 里加一行。"),
    ("枚举与短语待确认",
     "外部资料 / 写用例的人确认",
     ("bench_enum", "expect"),
     "枚举编码要台架手册;预期结果短语要写用例的人确认本意(有的短语反过来写:失败也算合格)。"),
    ("写入参数没落处",
     "点表里的可写点 / params.yaml 的条目",
     ("param", "bench_param"),
     "这些不是符号也不是枚举:型号点表里还没有这个可写点,或 params.yaml 还没有这个名字。"),
    ("其余写法异常", "缺取值 / 认不出的操作类型", ("bench_value", "op_type"), None),
)


def _gap_tiers(kinds: Mapping[str, int]) -> List[Tuple[str, int]]:
    return [(label, sum(kinds.get(kind, 0) for kind in rules)) for label, _, rules, _ in GAP_TIERS]


def _short(name: str, limit: int = 80) -> str:
    """报表里只放开头:对象参数常常是一整帧十六进制报文,原样贴出来没人能读。"""
    return name if len(name) <= limit else name[:limit] + "…"


def _render_structural(report: Mapping[str, Any]) -> List[str]:
    lines = ["### 结构异常", ""]
    if not report["structural"]:
        return lines + ["（没有）", ""]
    classes: Dict[str, int] = {}
    for _, anomalies in report["structural"]:
        for anomaly in anomalies:
            classes[anomaly.split(":", 1)[0]] = classes.get(anomaly.split(":", 1)[0], 0) + 1
    lines += [f"- {kind}: {count}" for kind, count in sorted(classes.items(), key=lambda i: (-i[1], i[0]))]
    lines += ["", "明细（前 20 条）:", ""]
    for case, anomalies in report["structural"][:20]:
        lines.append(f"- {case['case_id'] or '(无编号)'} 第 {case['row']} 行: {'; '.join(anomalies)}")
    return lines + [""]


def _render_op_coverage(sheets: List[Mapping[str, Any]], tables: Mapping[str, Any]) -> List[str]:
    coverage = op_coverage(sheets, tables)
    percent = 100.0 if not coverage["seen"] else 100.0 * coverage["mapped"] / coverage["seen"]
    lines = [
        "## 操作类型归一化",
        "",
        f"扫描到的操作类型取值 {coverage['seen']} 种,已归一化 {coverage['mapped']} 种"
        f"({percent:.1f}%),未归一化 {len(coverage['unmapped'])} 种。",
        "",
    ]
    lines += [f"- `{name}`" for name in coverage["unmapped"]] or ["（没有）"]
    lines += ["", "## 8 个原子动作的映射表（configs/import/ops.yaml）", ""]
    for action in ACTIONS:
        names = [name for name, mapped in tables["ops"].items() if mapped == action]
        lines.append(f"- {action}: {' / '.join(names) if names else '(空)'}")
    return lines + [""]


def _render_pilot_section(pilot: Mapping[str, Any]) -> List[str]:
    lines = [
        "## 试点",
        "",
        f"落成 configs/setting_cases_pilot.yaml 的条款 {len(pilot['cases'])} 条"
        f"(型号 {pilot['model']}),全部由 test_setting_cases 跑。",
        "",
        "口径:只有**全部步骤**都能落进 setting_cases 的条款才算可生成(每个步骤都是写、参数名与取值"
        "在 configs/import/params.yaml 里有映射、预期结果是等值),台架前置另外落成 bench:。"
        "含读 / 延时 / 人工步骤的条款只是上游条款的子集 —— 少了步骤也绿,读报告的人分不出它验了半个"
        "还是全个 —— 标\"部分\",不进试点。",
        "",
        "```",
    ]
    for tier in pilot["tiers"]:
        lines.append(
            f"{tier['sheet']} 条款 {tier['total']}: 完整 {tier['complete']} / 归场景层 {tier['scenario']} / "
            f"部分 {tier['partial']} / 不可生成 {tier['gaps'] + tier['structural']}"
        )
    lines += ["```", ""]
    if pilot["cases"]:
        lines += [
            f"- {case['case_id']} (第 {case['row']} 行,{len(case['steps'])} 个写步骤,完整)"
            for case in pilot["cases"]
        ]
        return lines + [""]
    lines += [
        "试点是 0 条,这是结论不是失败:没有一条条款的步骤能全部落地时,生成出来的每一条都是"
        "与被测条款无关的假绿(把波特率写进\"有功计量模式\",回读当然一致)。",
        "",
    ]
    return lines + _render_write_gaps(pilot)


def _render_write_gaps(pilot: Mapping[str, Any]) -> List[str]:
    """缺什么才能生成:写步骤用到的参数名里,params.yaml 还没有的那些。"""
    if not pilot["write_gaps"]:
        return []
    lines = [
        "要继续这条路的下一步:先在点表里给出对应的可写读数键,再在 params.yaml 里写同名映射。"
        "写步骤用到的这些名字现在都还没有:",
        "",
    ]
    return lines + [f"- `{_short(name)}` × {count}" for name, count in pilot["write_gaps"]]


# ---------------------------------------------------------------- 试点


def pilot_cases(workbook: List[Mapping[str, Any]], tables: Mapping[str, Any], limit: int) -> Dict[str, Any]:
    """只收**完整**条款:每个步骤都是写、参数名与取值都在对照表里、预期结果是等值。

    部分条款(有步骤落不进 setting_cases)不进试点 —— 它们的子集在报表里已经标了缺哪几步。
    """
    selected: Dict[str, Any] = {
        "model": tables["model"], "limit": limit, "cases": [], "tiers": [],
    }
    for sheet in workbook:
        report = coverage_report(sheet, tables)
        selected["tiers"].append(
            {"sheet": report["sheet"], "total": report["total"], "complete": len(report["complete"]),
             "scenario": len(report["scenario"]), "partial": len(report["partial"]),
             "gaps": len(report["gaps"]), "structural": len(report["structural"])}
        )
        for case in report["complete"]:
            if len(selected["cases"]) >= limit:
                continue
            selected["cases"].append(
                {"case_id": case["case_id"], "sheet": sheet["name"], "row": case["row"],
                 "title": case["title"], "bench": report["bench"].get(case["case_id"], {}),
                 "steps": [step for row in case["steps"] for step in _write_steps(row, tables) or []]}
            )
    selected["write_gaps"] = _unmapped_write_params(workbook, tables)
    return selected


def _write_steps(step: Mapping[str, Any], tables: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """一个写步骤落成清单里的 ``{write, value, expect}``;任何一个名字 / 取值对不上就是落不进去。"""
    rule = tables["expects"].get(step["expected"] or "")
    if rule is None or rule["kind"] != "equals":
        return None
    steps: List[Dict[str, Any]] = []
    for param in step["params"]:
        name, value = split_param(param)
        mapping = tables["params"].get(name or "")
        if mapping is None or mapping["device"] != "dut" or value not in mapping["values"]:
            return None
        encoded = mapping["values"][value]
        steps.append({"write": mapping["key"], "value": encoded, "expect": encoded})
    return steps or None


def _unmapped_write_params(
    workbook: List[Mapping[str, Any]], tables: Mapping[str, Any]
) -> List[Tuple[str, int]]:
    """试点要落的那些写步骤,用到的参数名里对照表还没有的(按出现次数)。"""
    counts: Dict[str, int] = {}
    for sheet in workbook:
        for case in sheet["cases"]:
            for step in case["steps"]:
                if tables["ops"].get(step["op"]) != "write":
                    continue
                for param in step["params"]:
                    name = split_param(param)[0] or param
                    if name != NO_PARAM and name not in tables["params"]:
                        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:PILOT_GAP_TOP]


def split_param(param: str) -> Tuple[Optional[str], Optional[str]]:
    """对象参数是 ``键=值``;没有 = 的(整条对象名、报文)没有键,归映射缺口。"""
    if "=" not in param:
        return None, None
    name, value = param.split("=", 1)
    return name.strip() or None, value.strip()


def render_pilot(pilot: Mapping[str, Any], tables: Mapping[str, Any], source_workbook: List[Mapping[str, Any]]) -> str:
    lines = [
        "# 试点清单:由 tools/import_cases.py 从上游 xlsx 生成,不要手改。",
        f"# 命令: python tools/import_cases.py --sheet {' --sheet '.join(s['name'] for s in source_workbook)}"
        f" --model {pilot['model']} --pilot-limit {pilot['limit']}",
        "# 跑法: python -m pytest tests/test_setting_cases.py --device-config build/pilot_devices.yaml",
        "# 只收**完整**条款:每个步骤都是写、参数名与取值都在 params.yaml 里、预期结果是等值。",
        "# 条款开头那段重复的台架设定落成 bench:(ADR 0006):它不进 steps,也不参与判定。",
        "# 含读 / 延时 / 人工步骤的条款是子集,不进试点(少一半步骤也绿,分不出验了半个还是全个)。",
        "# 参数名 → 点表键的映射见 configs/import/params.yaml(不同名必须显式写 synonyms,否则导入报错)。",
    ]
    if not pilot["cases"]:
        lines.append(
            "# 本次 0 条: "
            + "; ".join(
                f"{tier['sheet']} 完整 {tier['complete']} / 归场景层 {tier['scenario']} / "
                f"部分 {tier['partial']} / 不可生成 {tier['gaps'] + tier['structural']}"
                for tier in pilot["tiers"]
            )
            + " —— 先补点表与 params.yaml,缺口见 build/coverage.md。"
        )
    body = {"setting_cases": {pilot["model"]: {}}}
    for case in pilot["cases"]:
        body["setting_cases"][pilot["model"]][case["case_id"]] = _clause_body(case)
    return "\n".join(lines) + "\n" + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=False)


def render_pilot_manifest(pilot: Mapping[str, Any]) -> str:
    """出厂清单 + 试点条款。单独一份文件:默认套件不因为试点多跑一堆节点。

    试点 0 条时不建 `型号: {}` —— 清单装配拒收空映射,一份 0 条的试点清单会连加载
    都过不了,那样连“0 条”这个结果都拿不到。
    """
    manifest = copy.deepcopy(yaml.safe_load(DEVICES.read_text(encoding="utf-8")))
    clauses = manifest.setdefault("setting_cases", {})
    for case in pilot["cases"]:
        clauses.setdefault(pilot["model"], {})[case["case_id"]] = _clause_body(case)
    return yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _clause_body(case: Mapping[str, Any]) -> Dict[str, Any]:
    """一条完整条款落成清单片段:bench: 前置(有就写,不进 steps)+ 写步骤。"""
    body: Dict[str, Any] = {"clause": DEFAULT_CLAUSE}
    if case["bench"]:
        body["bench"] = dict(case["bench"])
    body["steps"] = [
        {"write": step["write"], "value": step["value"], "expect": step["expect"]}
        for step in case["steps"]
    ]
    return body


def _pilot_manifest_path() -> Path:
    return BUILD / "pilot_devices.yaml"


# ---------------------------------------------------------------- 输出


def _write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    if not target.is_absolute():
        target = REPO / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _rel(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return path.as_posix()


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
