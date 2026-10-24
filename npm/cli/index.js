#!/usr/bin/env node
/**
 * `npx @cautonwong/testplatform <init|run|gate|scaffold> ...`
 *
 * 这个文件只做三件事:认子命令、准备 Python 环境、把参数原样递给已有的 Python 入口。
 * 业务逻辑一行都不在这里(票 09f 的硬约束)。
 */
const path = require("node:path");

const lib = require("./lib");

const USAGE = `用法:npx @cautonwong/testplatform <命令> [参数...]

  init                       准备本项目下的 .testplatform/ 虚拟环境(只做一次,之后复用)
  run    <任务文件> [参数]   跑一个任务(转发 testplatform-run-task)
  gate   <报告目录> [参数]   门禁判定(转发 python -m testplatform.execution.gate)
  scaffold <driver|protocol|case|simulator> [--out DIR] [--name NAME]
                             生成骨架(转发 testplatform scaffold)

平台本体是 Python 包;这条 npx 命令是外壳。
`;

function main(argv, options) {
  const settings = { env: process.env, pkgDir: path.resolve(__dirname, ".."), cwd: process.cwd(), ...options };
  const log = settings.log || ((line) => console.error(line));
  const [command, ...rest] = argv;

  if (!command || command === "--help" || command === "-h" || command === "help") {
    process.stdout.write(USAGE);
    return command ? 0 : 2;
  }
  if (lib.pythonArgs(command, []) === null && command !== "init") {
    process.stderr.write(`不认识的命令: ${command}\n\n${USAGE}`);
    return 2;
  }

  try {
    const python = lib.pythonFor(settings.cwd, settings.env, settings.pkgDir, log);
    if (command === "init") {
      lib.ensureGitignoreHint(settings.cwd, log);
      console.log(`环境就绪:${python}`);
      console.log(`下一步: npx @cautonwong/testplatform run <任务文件> --evidence-dir reports/<任务>`);
      return 0;
    }
    lib.ensureGitignoreHint(settings.cwd, log);
    return lib.forward(python, lib.pythonArgs(command, rest), settings.cwd);
  } catch (error) {
    if (error instanceof lib.ShellError) {
      process.stderr.write(`${error.message}\n`);
      return 3;
    }
    throw error;
  }
}

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}

module.exports = { main, USAGE };
