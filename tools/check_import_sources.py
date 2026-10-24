"""全量机械校验 configs/import/ 里提取类产物的出处:每条 source 指向上游的哪一行,那一行真的写着它。

    python tools/check_import_sources.py --upstream E:/workspace/cmake/power/autotest-10544
    UPSTREAM_DIR=E:/workspace/cmake/power/autotest-10544 python tools/check_import_sources.py

上游目录只读、不并入仓库,所以路径不给就 skip(退出码 0 并说明原因),不写死任何本机路径。
与 mock 一族同一个模式:没有外部输入时不许假装跑过。

为什么要有它:提取类工作最典型的错法是**抄错一位** —— 行号差一行、DI 少一位、元件数写成相邻
那个。这种错在 yaml 里看着完全正常,而 42 的 mode: real 守卫只挡"地址还是 TODO",挡不住
"地址不是 TODO 但抄错了"。所以人眼抽查 3 条不够,这里做全量:逐条打开文件、逐行比对。

五类断言:
  1. source: "<相对路径>:<行号>" + expect   —— 那一行必须含 expect(行号错一位就红)
  2. Ctype 行按 (Ctype==<取值>)*<元件数> 比对 —— 元件数抄错就红
  3. name_needle 在 upstream_dicts 里必须 0 命中 —— 反向断言:上游真补了,回来重新提取
  4. source 逐字是「本仓定义(上游无中文名)」的行不受文件校验,但必须逐字如此 —— 上游没有
     中文名,给它编一个上游出处是本文件最该挡的事
  5. writes.yaml 的 expect 不在 yaml 里,由记录自己的 di/c/length/type 拼出来 —— 改 yaml 里
     任何一位都红;同时用图例的 digits 与 tuple 的 length 交叉核对(2 位十进制 = 1 字节)

退出码:全绿 0,有红 1,跳过 0(带原因)。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

REPO = Path(__file__).resolve().parents[1]
IMPORT_DIR = REPO / "configs" / "import"
LOCAL_SOURCE = "本仓定义(上游无中文名)"

# (产物里的位置, source "相对路径:行号", 那一行必须出现的字符串)
Check = Tuple[str, str, str]


def _from_yaml(name: str) -> Dict:
    return yaml.load(
        (IMPORT_DIR / name).read_text(encoding="utf-8"), Loader=_NoDuplicateKeys
    ) or {}


class _NoDuplicateKeys(yaml.SafeLoader):
    """重复键一律报错。yaml 默认后一个覆盖前一个 —— 把 DI 改成另一个已存在的 DI,
    被覆盖的那条连检查都不会跑,校验就绿了(实测过一次:110 条全绿,而 24 条里已经少了一条)。
    """

    def construct_mapping(self, node, deep=False):
        keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"{duplicates} 重复出现,yaml 会静默覆盖前一条")
        return super().construct_mapping(node, deep=deep)


def collect() -> Tuple[List[Check], List[str], List[Tuple[str, str]]]:
    """逐行断言 / 结构问题 / 反向断言 (条款里的写法, 上游字典文件)。"""
    checks: List[Check] = []
    problems: List[str] = []
    negatives: List[Tuple[str, str]] = []

    bench = _from_yaml("bench_quantities.yaml")
    for name, row in (bench.get("symbolic_values") or {}).items():
        checks.append((f"bench_quantities.yaml symbolic_values.{name}", row["source"], row["expect"]))
    for name, row in (bench.get("quantities") or {}).items():
        for evidence in row.get("evidence") or []:
            checks.append((f"bench_quantities.yaml quantities.{name}", evidence["source"], evidence["expect"]))

    wiring = _from_yaml("wiring_modes.yaml")
    for name, row in (wiring.get("names_zh") or {}).items():
        if row.get("source") != LOCAL_SOURCE:
            problems.append(
                f"wiring_modes.yaml names_zh.{name}: 上游没有中文名,source 必须逐字是「{LOCAL_SOURCE}」,"
                f"拿到 {row.get('source')!r}"
            )
    for row in wiring.get("ctypes") or []:
        # 元件数直接与上游那一行的公式项比对:写成相邻的数字就找不到这一项
        checks.append((
            f"wiring_modes.yaml ctypes[{row['ctype']}]",
            row["source"],
            f"(Ctype=={row['ctype']})*{row['elements']}",
        ))
    for row in wiring.get("meter_types") or []:
        checks.append((
            f"wiring_modes.yaml meter_types[{row['upstream_name']}]",
            row["source"],
            f"{row['upstream_name']} = {row['code']}",
        ))
    side = wiring.get("meter_side") or {}
    if side:
        checks.append(("wiring_modes.yaml meter_side", side["source"], side["expect"]))

    writable = _from_yaml("writable_params.yaml")
    for name, row in (writable.get("params") or {}).items():
        for item in (row.get("found") or []) + (row.get("near_miss") or []):
            checks.append((f"writable_params.yaml params.{name}", item["source"], item["expect"]))
        needle = row.get("name_needle")
        if not needle:
            problems.append(f"writable_params.yaml params.{name}: 缺 name_needle(条款里的写法),反向断言没法做")
            continue
        negatives += [(needle, path) for path in writable.get("upstream_dicts") or []]

    # symbols.yaml 的顶层键就是符号本身(Un / In / Hz),每条自己的 source + expect
    for name, row in _from_yaml("symbols.yaml").items():
        checks.append((f"symbols.yaml {name}", row["source"], row["expect"]))

    writes = _from_yaml("writes.yaml")
    legend = writes.get("legend") or {}
    for type_name, row in legend.items():
        checks.append((f"writes.yaml legend.{type_name}", row["source"], f"#{type_name}"))
        if row.get("layout"):
            checks.append((f"writes.yaml legend.{type_name}", row["source"], row["layout"]))
    for di, row in (writes.get("fields") or {}).items():
        # expect 由记录自己的字段拼出来:yaml 里改一位就与上游那一行对不上
        checks.append((
            f"writes.yaml fields.{di}",
            row["source"],
            f"'{row['name_upstream']}':('{di}', {row['c']}, {row['length']}, '{row['type']}'",
        ))
        spec = legend.get(row["type"])
        if not spec:
            problems.append(f"writes.yaml fields.{di}: 类型 {row['type']!r} 不在 legend 里,图例换算无从查")
        elif spec.get("digits") is not None and spec["digits"] != 2 * row["length"]:
            problems.append(
                f"writes.yaml fields.{di}: 图例 {row['type']} 是 {spec['digits']} 位数字,"
                f"tuple 的 length 是 {row['length']}(应为 {spec['digits'] // 2})—— 2 位十进制 1 字节对不上"
            )

    return checks, problems, negatives


def _split(source: str) -> Tuple[str, int]:
    path, _, line = source.rpartition(":")
    if not path or not line.isdigit():
        raise ValueError(f"source 必须写成 <相对路径>:<行号>,拿到 {source!r}")
    return path, int(line)


def run(root: Path) -> int:
    checks, problems, negatives = collect()
    failures: List[str] = [f"结构: {problem}" for problem in problems]
    cache: Dict[str, List[str]] = {}

    def lines_of(path: str) -> List[str]:
        if path not in cache:
            cache[path] = (root / path).read_text(encoding="utf-8").splitlines()
        return cache[path]

    for where, source, expect in checks:
        try:
            path, lineno = _split(source)
        except ValueError as error:
            failures.append(f"{where}: {error}")
            continue
        try:
            lines = lines_of(path)
        except FileNotFoundError:
            failures.append(f"{where}: 上游没有 {path}(source: {source})")
            continue
        if not 1 <= lineno <= len(lines):
            failures.append(f"{where}: {path} 只有 {len(lines)} 行,source 指到第 {lineno} 行")
            continue
        line = lines[lineno - 1]
        if expect not in line:
            failures.append(
                f"{where}: {source} 这一行不含 {expect!r}\n        实际是: {line.strip()[:160]}"
            )

    for needle, path in negatives:
        try:
            lines = lines_of(path)
        except FileNotFoundError:
            failures.append(f"反向断言: 上游没有 {path}")
            continue
        hits = [number for number, line in enumerate(lines, 1) if needle in line]
        if hits:
            failures.append(
                f"反向断言: {needle!r} 本该在 {path} 里 0 命中,第 {hits[:5]} 行出现了 —— "
                f"上游补上了,回来重新提取"
            )

    print(f"上游: {root}")
    print(f"逐行断言 {len(checks)} 条 + 反向断言 {len(negatives)} 条")
    if failures:
        print(f"\n{len(failures)} 处红:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("全绿:每条 source 都指到上游那一行,那一行确实写着记载的内容。")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 configs/import/ 提取产物的上游出处")
    parser.add_argument("--upstream", help="上游 legacy 平台目录(只读);缺省读 UPSTREAM_DIR 环境变量")
    args = parser.parse_args(argv)
    raw = args.upstream or os.environ.get("UPSTREAM_DIR")
    if not raw:
        print(
            "跳过:没给上游目录。\n"
            "  要指到 legacy 平台源码目录才能校验出处,例如:\n"
            "    python tools/check_import_sources.py --upstream <上游目录>\n"
            "    UPSTREAM_DIR=<上游目录> python tools/check_import_sources.py\n"
            "  上游不并入本仓,所以这里不写死路径 —— 没设就 skip,不假装校验过。"
        )
        return 0
    root = Path(raw)
    if not root.is_dir():
        print(f"跳过:{root} 不是目录。校验要指到上游源码根目录(其下有 tests/ 与 common/)。")
        return 0
    try:
        return run(root)
    except (KeyError, ValueError, FileNotFoundError) as error:
        print(f"红:产物结构不对({error})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
