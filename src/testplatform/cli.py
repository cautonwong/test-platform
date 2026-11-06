"""
`testplatform` 这个入口(装成包以后可用)。

    testplatform run <任务文件> [参数...]      转发 testplatform-run-task(同一份参数与退出码)
    testplatform gate <报告目录> [参数...]     转发 execution.gate
    testplatform scaffold driver|protocol|case|simulator [--out 目录] [--name 名字]
    testplatform new-case <名字> [--out 目录]  往已有项目加一条用例
    testplatform new-driver <名字> [--out 目录] 往已有项目加一个驱动
    testplatform new-protocol <名字> [--out 目录] 往已有项目加一个编解码模块
    testplatform cases [--root 项目] [--tag 标签] [--clause 条款] 列用例
    testplatform tasks [--root 项目] [--kind 类型] [--tag 标签] 列任务并校验
    testplatform eject [--dir 目录] [--no-install]
    testplatform --version

它是**投递壳**:生成的东西全是 Python,平台里没有自制的描述语言(ADR 0002)。
`run` 与 `gate` 不复制参数定义,原样转发给已有的 main —— 退出码与帮助只有一份。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from testplatform import __version__, eject as eject_module, scaffold


def main(argv: list[str] | None = None) -> int:
    # run / gate 的参数原样转发:先拦下来,不让外层 argparse 吃掉 --root / --help。
    # 外层 parser 只负责 scaffold / eject / --version 与帮助列表。
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["run"]:
        from testplatform.execution.cli import main as run_main

        return run_main(arguments[1:])
    if arguments[:1] == ["gate"]:
        from testplatform.execution.gate import main as gate_main

        return gate_main(arguments[1:])

    parser = argparse.ArgumentParser(
        prog="testplatform",
        description="平台入口:跑任务 / 门禁 / 脚手架 / eject",
    )
    parser.add_argument("--version", action="version", version=f"testplatform {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "run",
        add_help=False,
        help="跑一个任务(转发 testplatform-run-task,参数与退出码一致)",
    )
    subcommands.add_parser(
        "gate",
        add_help=False,
        help="门禁判定(转发 python -m testplatform.execution.gate)",
    )

    scaffold_parser = subcommands.add_parser(
        "scaffold", help="生成驱动 / 编解码 / 用例 / 模拟器的骨架(生成即可跑)"
    )
    scaffold_parser.add_argument("kind", choices=scaffold.KINDS)
    scaffold_parser.add_argument("--out", default=None)
    scaffold_parser.add_argument("--name", default=None)
    new_case_parser = subcommands.add_parser(
        "new-case", help="往已有项目加一条用例(不重新生成整个项目)"
    )
    new_case_parser.add_argument("name")
    new_case_parser.add_argument("--out", default=".")
    new_case_parser.add_argument("--device", default="dev_01")
    new_case_parser.add_argument("--clause", default="SCAFFOLD-1")
    new_case_parser.add_argument("--id", dest="case_id", default=None)
    new_driver_parser = subcommands.add_parser(
        "new-driver", help="往已有项目加一个驱动骨架(不重新生成整个项目)"
    )
    new_driver_parser.add_argument("name")
    new_driver_parser.add_argument("--out", default=".")
    new_driver_parser.add_argument("--device-type", default=None)
    new_driver_parser.add_argument(
        "--protocol", default="vendor_ascii", help="注册用的规约名(缺省 vendor_ascii,生成即可跑)"
    )
    new_driver_parser.add_argument(
        "--access", default="tcp", help="新规约允许的接入方式,逗号分隔(缺省 tcp)"
    )
    new_protocol_parser = subcommands.add_parser(
        "new-protocol", help="往已有项目加一个编解码模块(纯函数 + 自检)"
    )
    new_protocol_parser.add_argument("name")
    new_protocol_parser.add_argument("--out", default=".")
    new_simulator_parser = subcommands.add_parser(
        "new-simulator", help="往已有项目加一个模拟器传输(注册到指定 access)"
    )
    new_simulator_parser.add_argument("name")
    new_simulator_parser.add_argument("--out", default=".")
    new_simulator_parser.add_argument("--access", default=None, help="缺省 <名字>_sim")
    cases_parser = subcommands.add_parser(
        "cases", help="列出项目里的 @case(可按标签/条款/编号筛;只读源码,不跑用例)"
    )
    cases_parser.add_argument("--root", default=".")
    cases_parser.add_argument("--tests", nargs="+", default=["tests"])
    cases_parser.add_argument("--tag", action="append", default=[])
    cases_parser.add_argument("--clause", action="append", default=[])
    cases_parser.add_argument("--id", default=None)
    cases_parser.add_argument("--json", action="store_true")
    tasks_parser = subcommands.add_parser(
        "tasks", help="列出项目里的任务并逐个加载校验(可按 kind/tag 筛)"
    )
    tasks_parser.add_argument("--root", default=".")
    tasks_parser.add_argument("--tasks", default="tasks")
    tasks_parser.add_argument("--kind", default=None)
    tasks_parser.add_argument("--tag", default=None)
    tasks_parser.add_argument("--device", default=None, help="只看清单里含这台设备的任务")
    tasks_parser.add_argument("--json", action="store_true")
    eject_parser = subcommands.add_parser(
        "eject", help="把平台源码复制进项目并改成可编辑安装(之后平台代码可改)"
    )
    eject_parser.add_argument("--dir", default=eject_module.DEFAULT_DIR)
    eject_parser.add_argument("--no-install", action="store_true", help="只复制,不 pip install -e")
    parsed = parser.parse_args(arguments)

    if parsed.command == "cases":
        import json

        from testplatform.catalog import CaseScanError, scan_cases

        root = pathlib.Path(parsed.root).expanduser().resolve()
        roots = [root / name for name in parsed.tests]
        missing = [str(path) for path in roots if not path.exists()]
        if missing:
            print(f"测试目录不存在: {missing}", file=sys.stderr)
            return 2
        try:
            entries = scan_cases(roots, base=root)
        except CaseScanError as exc:
            print(f"扫描用例失败:{exc}", file=sys.stderr)
            return 2
        filtered = [
            entry
            for entry in entries
            if (not parsed.tag or all(tag in entry.tags for tag in parsed.tag))
            and (not parsed.clause or entry.clause in parsed.clause)
            and (not parsed.id or (entry.id or "").startswith(parsed.id))
        ]
        if parsed.json:
            print(
                json.dumps(
                    [
                        {
                            "id": entry.id,
                            "clause": entry.clause,
                            "tags": list(entry.tags),
                            "file": entry.file,
                            "line": entry.line,
                            "device_types": list(entry.device_types),
                        }
                        for entry in filtered
                    ],
                    ensure_ascii=False,
                )
            )
        else:
            for entry in filtered:
                print(
                    f"{entry.id or '«未解析»'}\t{entry.clause}\t"
                    f"{','.join(entry.tags)}\t{entry.file}:{entry.line}\t"
                    f"{','.join(entry.device_types) or '-'}"
                )
            if not filtered:
                print(f"没有匹配的用例(扫描 {len(entries)} 条)")
        return 0
    if parsed.command == "tasks":
        import json
        from testplatform.cases import parse_select
        from testplatform.execution.task import TaskFileError, load_task

        root = pathlib.Path(parsed.root).expanduser().resolve()
        task_dir = root / parsed.tasks
        if not task_dir.is_dir():
            print(f"任务目录不存在: {task_dir}", file=sys.stderr)
            return 2
        paths = sorted([*task_dir.glob("*.yaml"), *task_dir.glob("*.yml")])
        rows = []
        errors = []
        for path in paths:
            try:
                task = load_task(path, root=root)
            except TaskFileError as exc:
                errors.append({"file": str(path), "error": str(exc)})
                continue
            selectors = parse_select(task.select)
            if parsed.kind and task.kind != parsed.kind:
                continue
            if parsed.tag and not any(
                selector.kind == "tag" and selector.value == parsed.tag
                for selector in selectors
            ):
                continue
            if parsed.device and parsed.device not in {
                spec["id"] for spec in task.manifest.devices
            }:
                continue
            rows.append(
                {
                    "id": task.id,
                    "kind": task.kind,
                    "product": task.product,
                    "select": task.select,
                    "devices": str(task.devices),
                    "slaves": list(task.slave_ids),
                    "file": str(path),
                }
            )
        if parsed.json:
            print(json.dumps({"tasks": rows, "errors": errors}, ensure_ascii=False))
        else:
            for row in rows:
                print(
                    f"{row['id']}\t{row['kind']}\t{row['product']}\t"
                    f"{row['select']}\t{row['devices']}"
                )
            for error in errors:
                print(f"加载失败 {error['file']}: {error['error']}", file=sys.stderr)
            if not rows and not errors:
                print(f"没有任务文件: {task_dir}")
        return 1 if errors else 0
    if parsed.command == "eject":
        record = eject_module.eject(
            pathlib.Path.cwd(), into=parsed.dir, install=not parsed.no_install
        )
        print(f"平台源码已落到 {parsed.dir}/ · sha256 {record['sha256'][:12]}…")
        return 0
    if parsed.command == "scaffold":
        return scaffold.main(
            [parsed.kind]
            + (["--out", parsed.out] if parsed.out else [])
            + (["--name", parsed.name] if parsed.name else [])
        )
    if parsed.command == "new-case":
        try:
            path = scaffold.new_case(
                parsed.name,
                parsed.out,
                device=parsed.device,
                clause=parsed.clause,
                case_id=parsed.case_id,
            )
        except (ValueError, FileExistsError) as exc:
            print(f"生成用例失败:{exc}", file=sys.stderr)
            return 2
        print(f"已生成 {path}")
        print(f"跑它:  python -m pytest tests -q -k {parsed.name}")
        return 0
    if parsed.command == "new-driver":
        try:
            path = scaffold.new_driver(
                parsed.name,
                parsed.out,
                device_type=parsed.device_type,
                protocol=parsed.protocol,
                access=parsed.access,
            )
        except (ValueError, FileExistsError) as exc:
            print(f"生成驱动失败:{exc}", file=sys.stderr)
            return 2
        print(f"已生成 {path}")
        print(
            "下一步: 在 configs/devices.yaml 里加型号和设备;"
            "vendor_ascii 的 READ_COMMAND 或其它规约的 read_raw 按你的设备改。"
        )
        print("跑它:  python -m pytest tests -q")
        return 0
    if parsed.command == "new-protocol":
        try:
            path = scaffold.new_protocol(parsed.name, parsed.out)
        except (ValueError, FileExistsError) as exc:
            print(f"生成编解码失败:{exc}", file=sys.stderr)
            return 2
        print(f"已生成 {path}")
        print(f"自检:  python {path}")
        print("下一步: 在你的驱动里 import 它,把字节解成物理量。")
        return 0
    if parsed.command == "new-simulator":
        try:
            path = scaffold.new_simulator(parsed.name, parsed.out, access=parsed.access)
        except (ValueError, FileExistsError) as exc:
            print(f"生成模拟器失败:{exc}", file=sys.stderr)
            return 2
        print(f"已生成 {path}")
        print("下一步: 在驱动的 register_protocol 里加上这个 access,并把设备的 access 改成它。")
        return 0
    return 2  # pragma: no cover - argparse 已经挡住了


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
