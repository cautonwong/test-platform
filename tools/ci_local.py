"""
把 CI 跑的那条路,尽量在本地按同一个顺序走一遍。

    python tools/ci_local.py                 # 跑 .github/workflows/tasks.yml 里可本地跑的 step
    python tools/ci_local.py --dry-run       # 只解析 + 排队,不执行

为什么要有它:工作流里的 `run:` 是**真的会被 CI 执行**的那些命令。用另一套"本地等价命令"去验
等于验了个仿制品;这个工具直接从 YAML 里读命令、按 `needs` 的拓扑序执行,所以本地跑的
和 CI 跑的是同一条字符串。

三条规矩:
    * 命令只能来自 YAML,这里不重新发明任何命令
    * 每个 `uses:` step 明确打印"跳过 + 原因",并汇总到末尾(**不许静默**)
    * 标了 `local: unsupported <原因>` 的 step 同样跳过并如实列出 —— 它表示"这条只在 CI 有意义"
      (装依赖、传 artifact 之类),不是"本地验过了"

退出码:0 = 执行过的 step 全成功;1 = 有 step 失败(立刻停);2 = 工作流结构读不懂。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = REPO / ".github" / "workflows" / "tasks.yml"
EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")
# CI 专用变量:本地没有(或者本地有也不是同一个意思)。用了它们的 step 一律跳过并说明 ——
# 拿一个本地值顶上去,跑的就成了另一条命令。
CI_ONLY_VARS = ("RUNNER_TEMP", "RUNNER_OS", "RUNNER_ARCH", "GITHUB_WORKSPACE", "GITHUB_ENV", "GITHUB_OUTPUT")


@dataclass
class Planned:
    """一个要按顺序处理的 step(或整个 job 被标成不可本地跑)。"""

    job: str
    index: int
    name: str
    command: str | None = None
    skip_reason: str | None = None
    local_note: str | None = None


@dataclass
class Result:
    executed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, int]] = field(default_factory=list)
    exit_code: int = 0


class WorkflowError(ValueError):
    """工作流结构读不懂(没有 jobs / needs 指向不存在的 job / 循环依赖)。"""


def load(path: str | Path = DEFAULT_WORKFLOW) -> Mapping[str, Any]:
    workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(workflow, Mapping) or not isinstance(workflow.get("jobs"), Mapping):
        raise WorkflowError(f"{path}: 没有 jobs")
    return workflow


def order(jobs: Mapping[str, Any]) -> list[str]:
    """按 needs 的拓扑序排出 job 顺序(同层保持 YAML 里的先后)。"""
    seen: dict[str, int] = {}

    def visit(name: str, chain: tuple[str, ...] = ()) -> list[str]:
        if name in chain:
            raise WorkflowError(f"needs 出现循环依赖: {' → '.join(chain + (name,))}")
        if name in seen:
            return []
        seen[name] = 1
        job = jobs.get(name)
        if job is None:
            raise WorkflowError(f"needs 指向不存在的 job: {name!r}")
        ordered: list[str] = []
        for dependency in job.get("needs") or []:
            ordered.extend(visit(dependency, chain + (name,)))
        ordered.append(name)
        return ordered

    result: list[str] = []
    for name in jobs:
        result.extend(visit(name))
    return result


def plan(workflow: Mapping[str, Any], *, repo: Path = REPO) -> list[Planned]:
    """把工作流摊平成"按顺序做这些事"的清单(不执行任何东西)。"""
    jobs = workflow["jobs"]
    steps: list[Planned] = []
    for job_name in order(jobs):
        job = jobs[job_name] or {}
        job_note = _local_note(job)
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, Mapping):
                continue
            name = f"{job_name}/{step.get('name') or step.get('uses') or step.get('run', '')[:40]}"
            note = _local_note(step) or job_note
            command = step.get("run")
            if note:
                steps.append(Planned(job_name, index, name, local_note=note))
            elif command is None:
                steps.append(
                    Planned(job_name, index, name, skip_reason="uses: 容器动作,本地不可执行")
                )
            elif [name for name in CI_ONLY_VARS if name in str(command)]:
                used = [name for name in CI_ONLY_VARS if name in str(command)]
                steps.append(
                    Planned(
                        job_name,
                        index,
                        name,
                        local_note=f"命令用了 CI 专用变量 {used}",
                    )
                )
            elif _expressions(str(command), repo).unsupported:
                steps.append(
                    Planned(
                        job_name,
                        index,
                        name,
                        local_note=f"命令里有本地不支持的表达式 {_expressions(str(command), repo).unsupported}",
                    )
                )
            else:
                steps.append(Planned(job_name, index, name, command=str(command)))
    return steps


def run(planned: list[Planned], *, repo: Path = REPO, dry_run: bool = False) -> Result:
    result = Result()
    for step in planned:
        if step.skip_reason or step.local_note:
            reason = step.local_note or step.skip_reason or ""
            print(f"跳过 {step.name}:{reason}")
            result.skipped.append((step.name, reason))
            continue
        assert step.command is not None
        command = _expressions(step.command, repo).rendered
        print(f"执行 {step.name}:{command}")
        if dry_run:
            result.executed.append(step.name)
            continue
        completed = subprocess.run(
            command,
            shell=True,
            cwd=repo,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        result.executed.append(step.name)
        if completed.returncode != 0:
            result.failures.append((step.name, completed.returncode))
            result.exit_code = 1
            break
    return result


def summary(result: Result) -> list[str]:
    lines = [
        "",
        f"执行了 {len(result.executed)} 个 step,跳过 {len(result.skipped)} 个,"
        f"退出码 {result.exit_code}",
    ]
    if result.skipped:
        lines.append("跳过的 step(不是通过):")
        lines += [f"  - {name}: {reason}" for name, reason in result.skipped]
    if result.failures:
        lines.append("失败的 step:")
        lines += [f"  - {name}: 退出码 {code}" for name, code in result.failures]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 CI 工作流的顺序在本地跑一遍(命令从 YAML 里读)")
    parser.add_argument("--workflow", default=str(DEFAULT_WORKFLOW))
    parser.add_argument("--dry-run", action="store_true", help="只解析与排队,不执行")
    arguments = parser.parse_args(argv)

    try:
        workflow = load(arguments.workflow)
        planned = plan(workflow)
    except (WorkflowError, OSError, yaml.YAMLError) as exc:
        print(f"工作流读不懂:{exc}")
        return 2

    result = run(planned, dry_run=arguments.dry_run)
    for line in summary(result):
        print(line)
    return result.exit_code


# ---------------------------------------------------------------- 内部 ----


@dataclass(frozen=True)
class _Rendered:
    rendered: str
    unsupported: list[str]


def _expressions(command: str, repo: Path) -> _Rendered:
    """把 GitHub 表达式换成本地的值;换不了的收集起来,由调用方决定"跳过并说明"。"""
    unsupported: list[str] = []

    def replace(match: re.Match) -> str:
        expression = match.group(1).strip()
        if expression in ("github.workspace", "github.workspace && ''"):
            return str(repo)
        unsupported.append(expression)
        return match.group(0)

    return _Rendered(EXPRESSION.sub(replace, command), unsupported)


def _local_note(entry: Mapping[str, Any]) -> str | None:
    """`local: unsupported <原因>` = 这条只在 CI 有意义。"""
    value = entry.get("local")
    if not value:
        return None
    text = str(value).strip()
    return text if text else "标记了 local 但没写原因"


if __name__ == "__main__":
    raise SystemExit(main())
