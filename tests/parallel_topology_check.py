"""ticket 8b 的实测脚本:证明"同一个 via: 下的设备落在同一个 worker"。

不是用例:文件名不以 test_ 开头,默认套件不收集它,不计入基线。
分组键与分组的常驻守卫在 tests_platform/test_topology_grouping.py,不靠这个脚本。
跑法(仓库根目录):``python tests/parallel_topology_check.py``

它做三件事:
1. 造一份带拓扑的清单:两台终端,各挂两块表,跑
   ``pytest tests/parallel_topology_cases.py -n 4 --dist loadgroup``;
2. tests/conftest.py 的 _record_parallel_assignment 会把每条按设备参数化的用例的
   (worker_id, device_id, via) 追加进一个临时 CSV,这里据此断言:同一 via 下的设备
   连同那台终端本身 worker_id 全相同,并且不同终端没有被挤到同一个 worker 上;
3. 顺带断言分组键的推导:分组键跟到 via 链的根,链中间那层的设备也归到同一组。

组数少于 worker 数时如实打印"只用到哪几个 worker":那证明的是"不同终端确实分开了",
不是"四路都跑满了"。
"""
from __future__ import annotations

import csv
import importlib.util
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from tests_support.nested_pytest import pytest_command

REPO = Path(__file__).resolve().parent.parent
CASES = "tests/parallel_topology_cases.py"
CONFTEST_PATH = REPO / "tests" / "conftest.py"
WORKERS = 4

METER_MODEL = {
    "type": "meter",
    "protocol": "dlt645",
    "criteria": "meter_220v",
    "capabilities": ["电压", "正向有功总电能"],
    "point_table": {
        "电压": {
            "address": "02010100",
            "length": 2,
            "encoding": "bcd",
            "scale": 0.1,
            "byteorder": "little",
            "unit": "V",
        },
        "正向有功总电能": {
            "address": "00010000",
            "length": 4,
            "encoding": "bcd",
            "scale": 0.01,
            "byteorder": "little",
            "unit": "kWh",
        },
    },
}

TERMINAL_MODEL = {
    "type": "terminal",
    "protocol": "dlt645",
    "capabilities": ["上电运行时间"],
    "point_table": {
        "上电运行时间": {
            "address": "02800085",
            "length": 4,
            "encoding": "int",
            "scale": 1,
            "byteorder": "little",
            "unit": "s",
        },
    },
}

# 一台终端 + 挂在它下面的表:id → 12 位 BCD 表地址。它们的应答都从终端那条链路来。
TOPOLOGY = {
    "terminal_01": ("/dev/ttyUSB2", "000000000001", {"meter_a": "000000000002", "meter_b": "000000000003"}),
    "terminal_02": ("/dev/ttyUSB3", "000000000004", {"meter_c": "000000000005", "meter_d": "000000000006"}),
}


def _manifest() -> dict:
    simulated: dict[str, dict[str, str]] = {}
    devices = []
    for terminal_id, (port, address, meters) in TOPOLOGY.items():
        simulated[address] = {"02800085": "80510100"}  # 86400s 上电运行时间
        for meter_address in meters.values():
            simulated[meter_address] = {"02010100": "0023", "00010000": "00000000"}  # 230.0V / 0kWh
        devices.append(
            {
                "id": terminal_id,
                "model": "dlt645_terminal",
                "access": "serial",
                "mode": "simulator",
                "bus_address": address,
                "connection": {"port": port, "baudrate": 2400, "simulated_645": simulated},
            }
        )
        devices.extend(
            {
                "id": meter_id,
                "model": "dlt645_meter",
                "via": terminal_id,
                "access": "serial",
                "bus_address": meter_address,
            }
            for meter_id, meter_address in meters.items()
        )
    return {
        "criteria_profiles": {
            "meter_220v": [
                {"quantity": "电压", "min": 198, "max": 242},
                {"quantity": "正向有功总电能", "min": 0},
            ]
        },
        "models": {"dlt645_meter": METER_MODEL, "dlt645_terminal": TERMINAL_MODEL},
        "devices": devices,
    }


def _write_manifest(directory: Path, manifest: dict) -> Path:
    import yaml

    path = directory / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return path


def _load_tests_conftest():
    spec = importlib.util.spec_from_file_location("tests_conftest_parallel", CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_group_keys(conftest, manifest: dict) -> None:
    """分组键的推导:链中间那层也归到根那台终端;没有 via 的设备按自己的 id 分组。"""
    from testplatform.config.manifest import parse_manifest

    chained = {
        **manifest,
        "devices": manifest["devices"]
        + [
            {
                "id": "meter_x",
                "model": "dlt645_meter",
                "via": "meter_a",  # 三层链:meter_x → meter_a → terminal_01
                "access": "serial",
                "bus_address": "000000000007",
            }
        ],
    }
    parsed = parse_manifest(manifest)
    parsed_chained = parse_manifest(chained)

    assert conftest._topology_group(parsed, "terminal_01") == "terminal_01"
    assert conftest._topology_group(parsed, "meter_a") == "terminal_01"
    assert conftest._topology_group(parsed, "meter_d") == "terminal_02"
    assert conftest._topology_group(parsed_chained, "meter_x") == "terminal_01"
    print("分组键:两台终端各自成根,下游表(含三层链)归到根 —— OK")


def _run(manifest_path: Path, log_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TESTPLATFORM_PARALLEL_LOG": str(log_path)}
    return subprocess.run(
        pytest_command(
            CASES,
            "-q",
            "-n",
            str(WORKERS),
            "--dist",
            "loadgroup",
            "--device-config",
            str(manifest_path),
        ),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )


def _assignments(log_path: Path) -> dict[str, set[str]]:
    """device_id → 跑过它的 worker 集合。同一台设备出现在两个 worker 上就是分组失效。"""
    workers: dict[str, set[str]] = defaultdict(set)
    with log_path.open(encoding="utf-8") as stream:
        for worker_id, device_id, _via in csv.reader(stream):
            workers[device_id].add(worker_id)
    return workers


def _report(manifest: dict, workers: dict[str, set[str]]) -> None:
    via_of = {spec["id"]: spec.get("via") or "" for spec in manifest["devices"]}
    print(f"{'worker':<8}{'device':<14}{'via'}")
    for device_id in via_of:
        print(f"{'/'.join(sorted(workers.get(device_id, {'-'}))):<8}{device_id:<14}{via_of[device_id]}")

    missing = sorted(set(via_of) - set(workers))
    assert not missing, f"这些设备一条用例都没跑: {missing}"

    groups: dict[str, set[str]] = defaultdict(set)
    for device_id, worker_ids in workers.items():
        groups[via_of[device_id] or device_id] |= worker_ids

    for root, worker_ids in sorted(groups.items()):
        assert len(worker_ids) == 1, f"终端 {root} 下的设备分散在多个 worker: {sorted(worker_ids)}"

    used = sorted({worker for ids in groups.values() for worker in ids})
    assert len(used) >= 2, f"所有组都挤在同一个 worker 上,并行没生效: {groups}"
    print(f"组数 {len(groups)},用到的 worker: {used}")
    if len(groups) < WORKERS:
        print(f"注意:只有 {len(groups)} 个组,证明的是不同终端分到了不同 worker,不是 {WORKERS} 路都跑满")


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))  # 脚本自己也要 import 到这份 worktree 的 testplatform
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest = _manifest()
        _check_group_keys(_load_tests_conftest(), manifest)

        log_path = directory / "assignments.csv"
        result = _run(_write_manifest(directory, manifest), log_path)
        print(result.stdout[-2000:])
        if result.returncode != 0:
            print(result.stderr[-2000:], file=sys.stderr)
            return result.returncode

        _report(manifest, _assignments(log_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
