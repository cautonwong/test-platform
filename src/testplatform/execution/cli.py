"""
一条命令跑完一份测试任务 —— 装成包以后的入口:

    testplatform-run-task <任务文件> [--root <项目目录>] [--evidence-dir <目录>]

退出码 = 这一轮的结论,不是 pytest 的退出码(写死,不随任务文件变):
    0  合格(clean)          —— 含 smoke 任务:验的是通路,两栏没有设备
    1  不合格(failed)        —— 设备越过 reject 判据 / 场景里被测设备那一步不成立 / 产品自己退出
    2  测试无效(invalid)     —— 判定被环境吞掉(断链 / 台架故障);pytest 自己没跑通(收集崩 / 插件崩 /
                              一个用例都没收到)也退 2:那时拿不出可用的结论
    3  未完成(incomplete)    —— 申报外未测 / 本轮没验到设备(acceptance 的 0/0)
    4  平台错误              —— 任务文件写错、产品没就绪、从站起不来、项目目录里没有用例:
                              (报告里那一档是「未完成 + reason=platform_error」,退出码把它单独拎出来,
                              免得现场去改产品)
pytest 自己的退出码照旧写进证据(summary.json / task.json 的 pytest_exit_code),诊断要看它,
但它决定不了进程退出码 —— "报告红、CI 绿" 就是让它决定出来的。

源码布局里 `tools/run_task.py` 是同一个 `main` 的壳(仓库里的任务文件注释都指它)。
"""
from __future__ import annotations

import argparse
import sys

from testplatform.config.paths import PlatformRootError
from testplatform.execution.task import (
    DEFAULT_READY_TIMEOUT,
    EXIT_PLATFORM_ERROR,
    TaskFileError,
    TaskRunError,
    load_task,
    run_task,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一份测试任务:起产品、拉起模拟从站、跑选中的用例、落证据")
    parser.add_argument("task", help="任务文件 YAML")
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "项目目录(用例与清单所在的那一层,也是证据目录的父目录);"
            "省略时按 TESTPLATFORM_ROOT 或当前目录。平台自带的资源(从站脚本 / 寄存器表 / 条款清单)"
            "项目目录里没有就用包内的"
        ),
    )
    parser.add_argument("--evidence-dir", default=None, help="证据目录(缺省 evidence/task-<id>-<时间戳>/)")
    parser.add_argument("--registers", default=None, help="模拟从站的寄存器表(缺省取清单里该设备的 simulated_registers)")
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT,
        help=f"等产品监听端口的超时秒数(缺省 {DEFAULT_READY_TIMEOUT:g})",
    )
    arguments = parser.parse_args(argv)

    try:
        task = load_task(arguments.task, root=arguments.root)
    except (TaskFileError, PlatformRootError) as exc:
        # 两种都是"这一轮起不来",不是产品不合格:资源缺项的消息已经一次说清了,不再叠一层 traceback
        print(f"任务文件 / 平台根有问题:{exc}", file=sys.stderr)
        return EXIT_PLATFORM_ERROR

    print(
        f"任务 {task.id}(产品 {task.product})·设备清单 {task.devices} · 筛选 {task.select}"
        f" · 模拟从站 {list(task.slave_ids)}",
        flush=True,
    )
    try:
        exit_code = run_task(
            task,
            ready_timeout=arguments.ready_timeout,
            registers=arguments.registers,
            evidence_dir=arguments.evidence_dir,
        )
    except TaskRunError as exc:
        print(f"任务没跑起来:{exc}", file=sys.stderr)
        return EXIT_PLATFORM_ERROR
    print(f"任务 {task.id} 结束,退出码 {exit_code}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
