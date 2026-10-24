"""
`testplatform` 这个入口(装成包以后可用)。

    testplatform scaffold driver|protocol|case|simulator [--out 目录] [--name 名字]

它是**投递壳**:生成的东西全是 Python,平台里没有自制的描述语言(ADR 0002)。
跑任务仍然走 `testplatform-run-task`。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from testplatform import eject as eject_module, scaffold


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="testplatform", description="平台入口:脚手架 / 跑任务(跑任务请用 testplatform-run-task)"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    scaffold_parser = subcommands.add_parser(
        "scaffold", help="生成驱动 / 编解码 / 用例 / 模拟器的骨架(生成即可跑)"
    )
    scaffold_parser.add_argument("kind", choices=scaffold.KINDS)
    scaffold_parser.add_argument("--out", default=None)
    scaffold_parser.add_argument("--name", default=None)
    eject_parser = subcommands.add_parser(
        "eject", help="把平台源码复制进项目并改成可编辑安装(之后平台代码可改)"
    )
    eject_parser.add_argument("--dir", default=eject_module.DEFAULT_DIR)
    eject_parser.add_argument("--no-install", action="store_true", help="只复制,不 pip install -e")
    arguments = parser.parse_args(argv)

    if arguments.command == "eject":
        record = eject_module.eject(
            pathlib.Path.cwd(), into=arguments.dir, install=not arguments.no_install
        )
        print(f"平台源码已落到 {arguments.dir}/ · sha256 {record['sha256'][:12]}…")
        return 0
    if arguments.command == "scaffold":
        return scaffold.main(
            [arguments.kind]
            + (["--out", arguments.out] if arguments.out else [])
            + (["--name", arguments.name] if arguments.name else [])
        )
    return 2  # pragma: no cover - argparse 已经挡住了


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
