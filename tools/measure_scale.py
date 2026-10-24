"""P0 规模验收:量五个数字 + 验证两处预判瓶颈。

前置:先跑 `python tools/gen_scale_manifest.py` 生成 configs/devices_scale.yaml。
跑法:``python tools/measure_scale.py``(约 2 分钟;证据目录测完即删,不留几百 MB)

五个数:
    1 装配耗时      CompositionRoot.build(<大清单>)
    2 收集耗时      pytest --collect-only -q 的墙钟与用例条数
    3 证据体积      跑一轮后 evidence/ 的体积、文件数、单文件平均行数(附 --no-evidence 对照)
    4 归因输出行数  对同一轮跑 tools/attribute_round.py 的行数,以及其中枚举噪声的占比
    5 并行收益      同一轮 串行 vs -n 4 --dist loadgroup 的墙钟

两处预判瓶颈用同一组微基准回答(与 pytest 无关,只测文件 I/O 形状):
    A 每台设备一个文件 + 每条记录一次 open/close(现状)
    B 全部记录挤在一个文件里 + 每条记录一次 open/close  → 与 A 的差 = 「每台设备一个文件」的代价
    C 一个文件 + 一次 open 批量写                        → 与 B 的差 = 「每帧一次 open/close」的代价
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = "configs/devices_scale.yaml"
EVIDENCE_ROOT = REPO / "evidence"

_COLLECTED = re.compile(r"(\d+) tests? collected")


# 子进程(windows 控制台默认 GBK)统一说 UTF-8,否则中文输出到这里是乱码,按行分类会全落空。
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def _run(*args: str) -> tuple[float, subprocess.CompletedProcess]:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, *args],
        cwd=REPO,
        env=_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return time.perf_counter() - started, result


def _evidence_dirs() -> set[Path]:
    return {path for path in EVIDENCE_ROOT.iterdir() if path.is_dir()} if EVIDENCE_ROOT.is_dir() else set()


# ------------------------------------------------------------------ 1 装配

def measure_build(runs: int = 5) -> dict:
    sys.path.insert(0, str(REPO / "src"))
    from testplatform.composition.root import CompositionRoot

    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        root = CompositionRoot.build(CONFIG)
        timings.append(time.perf_counter() - started)
    return {
        "median_ms": round(statistics.median(timings) * 1000, 1),
        "min_ms": round(min(timings) * 1000, 1),
        "max_ms": round(max(timings) * 1000, 1),
        "devices": len(root.drivers),
        "runs": runs,
    }


# ------------------------------------------------------------------ 2 收集

def measure_collect() -> dict:
    elapsed, result = _run("-m", "pytest", "--collect-only", "-q", "--device-config", CONFIG)
    match = _COLLECTED.search(result.stdout)
    return {
        "seconds": round(elapsed, 2),
        "collected": int(match.group(1)) if match else None,
        "exit": result.returncode,
    }


# ------------------------------------------------------------------ 3 证据

def measure_evidence_round(runs: int = 3) -> tuple[dict, Path]:
    """跑几轮取墙钟中位数,体积统计取最后一轮(每轮跑的都是同一条命令)。"""
    timings, last = [], None
    for _ in range(runs):
        before = _evidence_dirs()
        elapsed, result = _run("-m", "pytest", "-q", "--device-config", CONFIG)
        new_dirs = sorted(_evidence_dirs() - before)
        if len(new_dirs) != 1:
            raise SystemExit(f"预期恰好一轮证据目录,拿到 {new_dirs}")
        if last is not None:
            shutil.rmtree(last, ignore_errors=True)
        last = new_dirs[0]
        timings.append(elapsed)
    directory = last

    files = sorted(directory.iterdir())
    jsonl = [path for path in files if path.suffix == ".jsonl"]
    line_counts = [len(path.read_text(encoding="utf-8").splitlines()) for path in jsonl]
    non_empty = [count for count in line_counts if count]
    total_bytes = sum(path.stat().st_size for path in files)
    records = sum(line_counts)
    stats = {
        "directory": directory.name,
        "seconds": round(statistics.median(timings), 2),
        "seconds_range": [round(min(timings), 2), round(max(timings), 2)],
        "summary": _summary(result.stdout),
        "files": len(files),
        "jsonl_files": len(jsonl),
        "empty_jsonl_files": len(jsonl) - len(non_empty),
        "bytes": total_bytes,
        "megabytes": round(total_bytes / 1024 / 1024, 2),
        "records": records,
        "avg_lines_per_nonempty_file": round(sum(non_empty) / len(non_empty), 1),
        "max_lines_in_a_file": max(line_counts),
        "avg_bytes_per_record": round(total_bytes / records, 1),
    }
    # 记录数与 open/close 次数相等:evidence.py:_append 每条记录 open 一次(见报告)。
    stats["open_close_cycles"] = records
    return stats, directory


# ------------------------------------------------------------------ 4 归因

def measure_attribution(directory: Path) -> dict:
    elapsed, result = _run("tools/attribute_round.py", str(directory))
    lines = result.stdout.splitlines()
    # 噪声 = 逐台枚举出来的行:同一类错误的重复行(只差 nodeid)、设备名单长行、每台设备的说明行。
    noise = [
        line
        for line in lines
        if line.startswith("  设备 ") or line.startswith("  涉及设备") or line.startswith("  设备范围")
    ]
    repeated = [line for line in lines if "DeviceNotConnectedError" in line]
    return {
        "seconds": round(elapsed, 2),
        "lines": len(lines),
        "chars": sum(len(line) for line in lines),
        "noise_lines": len(noise),
        "noise_pct": round(len(noise) / len(lines) * 100, 1),
        "repeated_error_lines": len(repeated),
        "longest_line_chars": max(len(line) for line in lines),
        "exit": result.returncode,
        "verdict_line": next((line for line in lines if line.startswith("结论:")), ""),
    }


# ------------------------------------------------------------------ 5 并行

def measure_parallel(runs: int = 3) -> dict:
    serial, parallel = [], []
    for _ in range(runs):
        elapsed, _ = _run("-m", "pytest", "-q", "--device-config", CONFIG, "--no-evidence")
        serial.append(elapsed)
        elapsed, _ = _run(
            "-m", "pytest", "-q", "--device-config", CONFIG, "--no-evidence", "-n", "4", "--dist", "loadgroup"
        )
        parallel.append(elapsed)
    serial_median, parallel_median = statistics.median(serial), statistics.median(parallel)
    return {
        "serial_seconds": round(serial_median, 2),
        "serial_range": [round(min(serial), 2), round(max(serial), 2)],
        "parallel_seconds": round(parallel_median, 2),
        "parallel_range": [round(min(parallel), 2), round(max(parallel), 2)],
        "speedup": round(serial_median / parallel_median, 2),
        "runs": runs,
        "worker_startup": measure_worker_startup(),
    }


def measure_worker_startup() -> dict:
    """xdist 每个 worker 的固定开销:用不装配组合根的 tests_unit 量,两边的差就是它。

    本轮只有 ~20 秒,4 个 worker 的启动开销吃掉并行的收益 —— 规模变小时这一项占比
    只会更大,所以并行值不值得开,要看这一项占整轮的比例。
    """
    serial, _ = _run("-m", "pytest", "-q", "tests_unit")
    parallel, _ = _run("-m", "pytest", "-q", "tests_unit", "-n", "4", "--dist", "loadgroup")
    return {
        "unit_serial_seconds": round(serial, 2),
        "unit_n4_seconds": round(parallel, 2),
        "extra_seconds_per_3_workers": round(parallel - serial, 2),
    }


# ------------------------------------------------------------------ 瓶颈微基准

def microbench(records: int = 6000, files: int = 264) -> dict:
    """同一批记录,三种落盘形状各写一遍:唯一变量是"文件数"和"每条记录是否重新 open"。"""
    payload = json.dumps({"kind": "frame", "direction": "send", "hex": "01" * 40}, ensure_ascii=False) + "\n"
    workdir = Path(tempfile.mkdtemp(prefix="scale-io-"))
    try:
        single = workdir / "single.jsonl"
        started = time.perf_counter()
        # A 现状:每条记录一次 open/close(evidence.py::_append),但只有一个文件

        for _ in range(records):
            with single.open("a", encoding="utf-8") as stream:
                stream.write(payload)
        per_record_one_file = time.perf_counter() - started

        paths = [workdir / f"dev_{index:03d}.jsonl" for index in range(files)]
        started = time.perf_counter()
        for index in range(records):
            with paths[index % files].open("a", encoding="utf-8") as stream:
                stream.write(payload)
        per_record_many_files = time.perf_counter() - started

        started = time.perf_counter()
        with single.open("a", encoding="utf-8") as stream:
            for _ in range(records):
                stream.write(payload)
        batched_one_file = time.perf_counter() - started

        started = time.perf_counter()
        for path in paths:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(payload * (records // files))
        batched_many_files = time.perf_counter() - started
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "records": records,
        "files": files,
        "per_record_one_file_ms": round(per_record_one_file * 1000, 1),
        "per_record_many_files_ms": round(per_record_many_files * 1000, 1),
        "batched_one_file_ms": round(batched_one_file * 1000, 1),
        "batched_many_files_ms": round(batched_many_files * 1000, 1),
        "file_count_effect_pct": round((per_record_many_files / per_record_one_file - 1) * 100, 1),
        "open_close_effect_x": round(per_record_one_file / batched_one_file, 1),
    }


# ------------------------------------------------------------------ 汇总

def _summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        if " in " in line and ("passed" in line or "failed" in line or "error" in line):
            return line.strip()
    return ""


def main() -> int:
    if not (REPO / CONFIG).is_file():
        raise SystemExit(f"缺少 {CONFIG}:先跑 python tools/gen_scale_manifest.py")
    # 报告里有中文:重定向到文件时 Windows 默认 GBK,统一说 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")

    created: list[Path] = []
    try:
        build = measure_build()
        collect = measure_collect()
        evidence, directory = measure_evidence_round()
        created.append(directory)
        attribution = measure_attribution(directory)
        parallel = measure_parallel()
        io = microbench()
    finally:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)

    report = {
        "1_装配耗时": build,
        "2_收集耗时": collect,
        "3_证据体积": evidence,
        "4_归因输出": attribution,
        "5_并行收益": parallel,
        "io_微基准": io,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
