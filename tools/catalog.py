"""
生成或校验资产目录。

    python tools/catalog.py                       # 生成 catalog.json + CATALOG.md
    python tools/catalog.py --out dist/catalog    # 换输出目录
    python tools/catalog.py --verify              # 与磁盘上的目录比,不一致 → 退出码 1

判决逻辑在 src/testplatform/catalog/:这个文件只是入口。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from testplatform.catalog import catalog as assets  # noqa: E402

DEFAULT_OUT = "catalog"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="资产目录:生成 / 校验(目录是派生数据,不要手改)")
    parser.add_argument("--devices", default=None, help="设备清单(缺省:平台根下的 configs/devices.yaml)")
    parser.add_argument("--clauses", default=None, help="条款清单(缺省:平台自带那份)")
    parser.add_argument("--tests", nargs="*", default=None, help="扫描的测试目录(缺省 tests 系列的四个目录)")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"产物目录(缺省 {DEFAULT_OUT}/)")
    parser.add_argument("--verify", action="store_true", help="只校验:与磁盘上的产物比,不一致 → 退出码 1")
    arguments = parser.parse_args(argv)

    root = Path.cwd()
    devices = arguments.devices or str(assets.default_devices_path())
    test_roots = arguments.tests or [str(root / name) for name in assets.DEFAULT_TEST_ROOTS]
    built = assets.build(devices, clauses_path=arguments.clauses, test_roots=test_roots)

    if arguments.verify:
        differences = assets.verify(built, arguments.out)
        for line in differences:
            print(line)
        if differences:
            print(f"目录与磁盘不一致:{len(differences)} 处(重新生成一次,或看看是谁手改了)")
            return 1
        print(f"目录与磁盘一致:{arguments.out}/")
        return 0

    machine, human = assets.write(built, arguments.out)
    counts = built.as_record()["counts"]
    print(
        f"生成 {machine} 与 {human}:型号 {counts['models']} · 设备 {counts['devices']}"
        f" · 用例 {counts['cases']} · 条款 {counts['clauses']} · 缺口 {counts['gaps']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
