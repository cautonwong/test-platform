"""P1 的实测脚本:分组真的防住了"同一条物理总线被两个 worker 抢占"。

不是用例:文件名不以 test_ 开头,默认套件不收集它。分组键与分组的常驻守卫在
tests_platform/test_topology_grouping.py;这里证明的是另一半 —— 分组之后,总线上确实只剩一个请求者。
跑法(仓库根目录):``python tests/parallel_bus_check.py``

三轮动作:
1. 起两个 mock,一个 mock 就是一条物理总线。每条总线上一台总线主站(它持有那条链路)+
   两块用 ``via:`` 挂在主站下面的表(所以三台设备是同一组);
2. 正常组:``pytest tests/parallel_bus_cases.py -n 4 --dist loadgroup``。断言同一总线上的
   三台设备由同一个 worker 跑、两条总线落在不同 worker 上(否则"零抢占"什么也说明不了),
   并且两个 mock 一条抢占记录都没有;
3. 对照组:同一份清单、同一批用例,只用 ``-p tests.parallel_split_group_plugin`` 把其中一块表
   额外标成另一个 xdist_group —— 分组被拆开,两块表被两个 worker 抢着访问同一条总线,
   mock 必须记录到抢占。没有这个对照,"零冲突"等于什么都没证明。

mock 用 ``--fail slow --delay 0.4`` 把每条应答压慢:一帧还没答完的窗口因此有 0.4 秒,
两条连接交替发帧必然落在窗口里 —— 放大窗口,不靠运气。
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from tests_support.mock_meter_process import mock_process
from tests_support.nested_pytest import pytest_command

REPO = Path(__file__).resolve().parent.parent
CASES = "tests/parallel_bus_cases.py"
REGISTERS = REPO / "configs" / "mock_registers.yaml"
WORKERS = 4
DELAY = "0.4"

# 每条总线:一台总线主站(从站地址 1,持有那条 TCP 链路)+ 两块挂在它下面的表。
BUSES = {
    "bus_a": {"meter_a1": 2, "meter_a2": 3},
    "bus_b": {"meter_b1": 2, "meter_b2": 3},
}
BUS_MASTER_ADDRESS = 1

METER_MODEL = {
    "type": "meter",
    "protocol": "modbus_rtu",
    "criteria": "meter_220v",
    "capabilities": ["有功电量", "电压"],
    "point_table": {
        "有功电量": {
            "address": 0,
            "length": 4,
            "encoding": "uint",
            "scale": 0.01,
            "unit": "kWh",
        },
        "电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1, "unit": "V"},
    },
}


def _manifest(ports: dict[str, int]) -> dict:
    devices = []
    for bus_id, meters in BUSES.items():
        devices.append(
            {
                "id": bus_id,
                "model": "bus_meter",
                "access": "tcp",
                "mode": "real",
                "bus_address": BUS_MASTER_ADDRESS,
                "connection": {"host": "127.0.0.1", "port": ports[bus_id]},
            }
        )
        devices.extend(
            {
                "id": meter_id,
                "model": "bus_meter",
                "via": bus_id,
                "access": "tcp",
                "bus_address": address,
            }
            for meter_id, address in meters.items()
        )
    return {
        "criteria_profiles": {
            "meter_220v": [
                {"quantity": "电压", "min": 198, "max": 242},
                {"quantity": "有功电量", "min": 0},
            ]
        },
        "models": {"bus_meter": METER_MODEL},
        "devices": devices,
    }


def _mock_arguments(bus_id: str, log_path: Path) -> list[str]:
    """一条总线的 mock:这条总线上挂着的每个从站地址都给一次 --slave,抢占记录落到独立文件。"""
    addresses = [BUS_MASTER_ADDRESS, *BUSES[bus_id].values()]
    return [
        "--registers",
        str(REGISTERS),
        *[argument for address in addresses for argument in ("--slave", str(address))],
        "--fail",
        "slow",
        "--delay",
        DELAY,
        "--contention-log",
        str(log_path),
    ]


def _write_manifest(directory: Path, name: str, manifest: dict) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return path


def _run(manifest_path: Path, assignments_path: Path, extra: tuple[str, ...] = ()):
    env = {
        **os.environ,
        "TESTPLATFORM_PARALLEL_LOG": str(assignments_path),
        "PYTHONIOENCODING": "utf-8",
    }
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
            "--no-evidence",
            *extra,
        ),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _measure(directory: Path, name: str, extra: tuple[str, ...] = ()):
    """起两条总线的 mock → 跑一轮 → 同时收走 worker 分布与抢占记录。"""
    logs = {bus_id: directory / f"{name}-{bus_id}.jsonl" for bus_id in BUSES}
    with contextlib.ExitStack() as stack:
        ports = {
            bus_id: stack.enter_context(mock_process(*_mock_arguments(bus_id, log_path)))[0]
            for bus_id, log_path in logs.items()
        }
        manifest_path = _write_manifest(directory, name, _manifest(ports))
        assignments_path = directory / f"{name}-assignments.csv"
        result = _run(manifest_path, assignments_path, extra)
        print(result.stdout[-2000:])
        if result.returncode != 0:
            raise AssertionError(f"这一轮 pytest 没跑绿: {(result.stdout + result.stderr)[-2000:]}")
    return _assignments(assignments_path), {
        bus_id: _contention(log_path) for bus_id, log_path in logs.items()
    }


def _assignments(log_path: Path) -> dict[str, set[str]]:
    """device_id → 跑过它的 worker 集合。同一台设备出现在两个 worker 上就是分组失效。"""
    workers: dict[str, set[str]] = defaultdict(set)
    with log_path.open(encoding="utf-8") as stream:
        for worker_id, device_id, _via in csv.reader(stream):
            workers[device_id].add(worker_id)
    return workers


def _contention(log_path: Path) -> list[dict]:
    """mock 的抢占记录:每行一条 JSON(kind/at/connection/other)。"""
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _report(title: str, assignments: dict[str, set[str]]) -> None:
    print(title)
    print(f"{'worker':<8}{'device':<12}{'via'}")
    for bus_id, meters in BUSES.items():
        for device_id in (bus_id, *meters):
            via = "" if device_id == bus_id else bus_id
            print(f"{'/'.join(sorted(assignments.get(device_id, {'-'}))):<8}{device_id:<12}{via}")


def _worker_of_bus(assignments: dict[str, set[str]]) -> dict[str, str]:
    """一条总线(主站 + 挂在它下面的表)由哪个 worker 跑;分散在多个 worker 上就是分组失效。"""
    worker_of_bus = {}
    for bus_id, meters in BUSES.items():
        workers: set[str] = set()
        for device_id in (bus_id, *meters):
            assert device_id in assignments, f"{device_id} 一条用例都没跑"
            workers |= assignments[device_id]
        assert len(workers) == 1, f"总线 {bus_id} 上的设备分散在多个 worker: {sorted(workers)}"
        worker_of_bus[bus_id] = workers.pop()
    return worker_of_bus


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)

        grouped, grouped_contention = _measure(directory, "grouped")
        _report("正常组(--dist loadgroup,分组生效):", grouped)
        worker_of_bus = _worker_of_bus(grouped)
        assert len(set(worker_of_bus.values())) == len(BUSES), (
            f"两条总线挤在同一个 worker 上,并行没生效,这一轮的零抢占说明不了什么: {worker_of_bus}"
        )
        assert not any(grouped_contention.values()), (
            f"分组生效时竟然记录到抢占: {grouped_contention}"
        )
        print(f"两条总线分别在 {worker_of_bus} 上跑,抢占记录 0 条 —— 分组防住了\n")

        split, split_contention = _measure(
            directory, "split", ("-p", "tests.parallel_split_group_plugin")
        )
        _report("对照组(-p tests.parallel_split_group_plugin:meter_a2 被拆出 bus_a 组):", split)
        assert split["meter_a1"] != split["meter_a2"], f"两块表还是同一个 worker: {split}"
        assert any(split_contention.values()), "分组拆开后 mock 竟然没记录到抢占"
        for bus_id, events in split_contention.items():
            if not events:
                continue
            print(f"{bus_id} 记录到 {len(events)} 条抢占:")
            for event in events:
                print("  " + json.dumps(event, ensure_ascii=False))
        print("分组拆开后抢占被记录到 —— 上一轮的零抢占不是空话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
