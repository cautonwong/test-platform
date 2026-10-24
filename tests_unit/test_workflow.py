"""
CI 接线的机械校验(票 10):工作流结构不靠人看,靠这组断言。

守的是五种"CI 假绿"—— 它们共同的特征是:本地全绿、CI 永远绿、其实什么都没跑。
    ① 任务加了,工作流里没有对应 job(等于没跑)
    ② gate 的 needs 漏一个 job(那个任务的结论不进判定)
    ③ gate 步骤被 `if: always()` / `continue-on-error` 包起来(红也绿)
    ④ 任务 job 不上传产物(gate 拿不到报告,只能报"没跑过"或干脆看不到)
    ⑤ gate 不取回别人 job 的报告(它只能判自己 job 里那点东西)
    ⑥ 任务 job 不显式传 `--root`(靠"当前目录恰好是 workspace"碰巧过)
    ⑦ gate 的退出码被包成"只警告"(尤其第 6 档"没有验收任务"—— 把它当成功就是永久绿)
    ⑧ 入库的资产目录没人校验(`catalog/catalog.json` 是生成物:没人跑 --verify 它一定会漂)
    ⑨ 装成包以后的路没人走(wheel → 一次性 venv → 正例/负例):源码形态全绿,装完照样可能缺文件
    ⑩ `requires-python` 的下界没有 job 在跑(3.10 兼容就只是一句声明)

检查器是纯函数(`violations`):真实工作流必须为空;故意删一条 needs / 加一句掩盖,
同一组断言必须报出来 —— 这就是"删掉任一规则必红"的变异证明。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
TASKS = REPO / "configs" / "tasks"

RUN_TASK = "tools/run_task.py"
RUN_GATE = "tools/gate.py"
RUN_CATALOG = "tools/catalog.py"
UPLOAD = "actions/upload-artifact"
DOWNLOAD = "actions/download-artifact"
MASKING_ATTRIBUTES = ("continue-on-error",)
# CI 的工作目录表达式:--root 必须显式指向它,不靠「当前目录恰好是 workspace」
WORKSPACE = "${{ github.workspace }}"

# ⑨ 装成包以后的那条路(票 09c):打 wheel → 一次性 venv → 正例/负例
BUILD_WHEEL = "python -m build"
INSTALLED_CLI = "testplatform-run-task"
PROJECT_PRODUCT = "tests_support/fake_product.py"
PACKAGED_PRODUCT = "_data/tests_support/fake_product.py"
NEGATIVE_EXIT = "-eq 4"
NEGATIVE_MESSAGE = "包内缺 tests_support/fake_product.py"
# ⑩ 下界
FULL_SUITE = "python -m pytest"
LOWER_BOUND = "3.10"


def _workflows() -> dict[str, dict]:
    loaded = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        loaded[path.name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded


def _steps(job: Mapping[str, Any]) -> list[dict]:
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


def _runs(step: Mapping[str, Any]) -> str:
    return str(step.get("run") or "")


def _task_jobs(jobs: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        name: job
        for name, job in jobs.items()
        if any(RUN_TASK in _runs(step) for step in _steps(job))
    }


def _gate_jobs(jobs: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        name: job
        for name, job in jobs.items()
        if any(RUN_GATE in _runs(step) for step in _steps(job))
    }


def _all_jobs(
    workflows: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    return [
        (name, job)
        for workflow in workflows.values()
        for name, job in (workflow.get("jobs") or {}).items()
    ]


def _setup_python_versions(job: Mapping[str, Any]) -> set[str]:
    """这个 job 声明了哪些 Python 版本(去引号;YAML 不加引号的 3.10 会变成浮点 3.1)。"""
    return {
        str((step.get("with") or {}).get("python-version")).strip("'\"")
        for step in _steps(job)
        if str(step.get("uses") or "").startswith("actions/setup-python")
    }


def _installed_run_problems(workflows: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """⑨ 「装成包以后还能跑」：打 wheel → 一次性 venv 装 → 正例(产品从包里回落)+ 负例。

    负例必须同时断两件事:退出码 == 4 且消息含「包内缺 <那份文件>」。只断「非 0」时,
    路径写错 / YAML 写错 / 缺别的文件都会让这条负例"绿"—— 它就成了永不适用的装饰。
    """
    wheel_jobs = [
        (name, job)
        for name, job in _all_jobs(workflows)
        if any(BUILD_WHEEL in _runs(step) for step in _steps(job))
    ]
    if not wheel_jobs:
        return [f"没有 job 打 wheel(`{BUILD_WHEEL}`):装成包以后的样子从来没在 CI 上走过"]

    problems: list[str] = []
    for name, job in wheel_jobs:
        steps = _steps(job)
        runs = [_runs(step) for step in steps]
        if not any("venv" in run for run in runs) or not any(
            "pip install" in run and ".whl" in run for run in runs
        ):
            problems.append(f"job {name} 打了 wheel 却没装进一次性 venv:没装上,验的还是源码")
        if job.get("continue-on-error"):
            problems.append(f"job {name} 带了 continue-on-error:装坏了也会绿")

        positive = next((index for index, run in enumerate(runs) if INSTALLED_CLI in run), None)
        if positive is None:
            problems.append(f"job {name} 没跑装完以后的入口(`{INSTALLED_CLI}`):验的不是装好的那份")
        if not any(
            PROJECT_PRODUCT in run and PACKAGED_PRODUCT not in run and "rm " in run
            for run in runs
        ):
            problems.append(
                f"job {name} 的正例没删项目目录里那份 {PROJECT_PRODUCT}:"
                f"产品不会回落到包里,这条正例什么都没验"
            )
        if positive is not None:
            for swallow in ("|| true", "; true", "|| :", "|| exit 0"):
                if swallow in runs[positive]:
                    problems.append(f"job {name} 的正例里有 {swallow!r}:失败被吞掉了")

        negative = next(
            (
                index
                for index, run in enumerate(runs)
                if PACKAGED_PRODUCT in run and "rm " in run
            ),
            None,
        )
        if negative is None:
            problems.append(f"job {name} 没有负例:没从 site-packages 删掉 {PACKAGED_PRODUCT}")
        else:
            run = runs[negative]
            if "rc=$?" not in run:
                problems.append(f"job {name} 的负例没原样留住退出码(`rc=$?`):经管道拿到的是 tee 的")
            if NEGATIVE_EXIT not in run:
                problems.append(
                    f"job {name} 的负例缺 `{NEGATIVE_EXIT}`:只断「非 0」时,"
                    f"路径错 / YAML 错 / 缺别的文件也能让它绿"
                )
            if NEGATIVE_MESSAGE not in run:
                problems.append(
                    f"job {name} 的负例没断消息含「{NEGATIVE_MESSAGE}」:exit 4 可能因为任何别的原因"
                )
            if "| tee" in run:
                problems.append(f"job {name} 的负例用管道拿退出码:那是 tee 的,不是任务的")
            if positive is not None and negative < positive:
                problems.append(f"job {name} 的负例排在正例之前:装坏了的 venv 之后没法再验正例")
            if any("venv" in _runs(step) for step in steps[negative + 1 :]):
                problems.append(f"job {name} 在负例之后又用了那个 venv:它已经是「装坏了」的状态")

        for index in (positive, negative):
            if index is None:
                continue
            step = steps[index]
            if step.get("if") or step.get("continue-on-error"):
                problems.append(
                    f"job {name} 的装卸验证步骤 {step.get('name')!r} 带了 if/continue-on-error:红会变成绿"
                )

    return problems


def _lower_bound_problems(workflows: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """⑩ `requires-python` 的下界必须有 job 真跑,而且跑的是全量。"""
    on_lower = [
        (name, job)
        for name, job in _all_jobs(workflows)
        if LOWER_BOUND in _setup_python_versions(job)
    ]
    if not on_lower:
        return [f"没有 job 在 Python {LOWER_BOUND} 上跑(requires-python 的下界只是声明,没人跑过)"]

    problems: list[str] = []
    for name, job in on_lower:
        full = [step for step in _steps(job) if FULL_SUITE in _runs(step)]
        if not full:
            problems.append(f"Python {LOWER_BOUND} job {name} 没跑全量(`{FULL_SUITE}`)")
        for step in full:
            if step.get("if") or step.get("continue-on-error"):
                problems.append(
                    f"Python {LOWER_BOUND} job {name} 的全量被条件化/掩盖了:{step.get('name')!r}"
                )
        if job.get("continue-on-error"):
            problems.append(f"Python {LOWER_BOUND} job {name} 带了 continue-on-error")
    return problems


def violations(workflows: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """把一条工作流集合里所有"会变成假绿"的结构问题都列出来。空 = 合规。"""
    problems: list[str] = []
    task_files = {path.name: path for path in sorted(TASKS.glob("*.y*ml"))}
    task_jobs: dict[str, Mapping[str, Any]] = {}
    gate_jobs: dict[str, Mapping[str, Any]] = {}

    for path, workflow in workflows.items():
        jobs = workflow.get("jobs") or {}
        if not isinstance(jobs, Mapping) or not jobs:
            problems.append(f"{path}: 没有 jobs")
            continue
        task_jobs.update(_task_jobs(jobs))
        gate_jobs.update(_gate_jobs(jobs))

    # ① 每个任务文件都有 job;反过来,job 不许指向不存在的任务
    covered = set()
    for name, job in task_jobs.items():
        for step in _steps(job):
            for task_name in task_files:
                if f"configs/tasks/{task_name}" in _runs(step):
                    covered.add(task_name)
    for task_name in sorted(set(task_files) - covered):
        problems.append(
            f"任务 configs/tasks/{task_name} 没有任何 job 跑它(加了任务却没接线 = 它永远不会跑)"
        )
    for task_name in sorted(covered - set(task_files)):
        problems.append(f"有 job 跑 configs/tasks/{task_name},但这个文件不存在")

    # ② gate 的 needs 覆盖全部任务 job,而且只许有一个 gate
    if not gate_jobs:
        problems.append("没有 gate job(没有任何 job 跑 tools/gate.py)")
    elif len(gate_jobs) > 1:
        problems.append(f"gate job 不止一个: {sorted(gate_jobs)}(结论必须唯一)")
    for gate_name, gate in gate_jobs.items():
        needs = set(gate.get("needs") or [])
        missing = sorted(set(task_jobs) - needs)
        if missing:
            problems.append(
                f"gate job {gate_name} 的 needs 漏了任务 job {missing}:"
                f"那些任务的结论不会进判定(假绿)"
            )

        # ③ gate 步骤不许被掩盖
        gate_steps = [step for step in _steps(gate) if RUN_GATE in _runs(step)]
        for step in gate_steps:
            if "if" in step:
                problems.append(
                    f"gate 步骤带了 if={step['if']!r}:红会被改成绿(门禁必须无条件跑)"
                )
            for attribute in MASKING_ATTRIBUTES:
                if step.get(attribute):
                    problems.append(f"gate 步骤带了 {attribute}:{step[attribute]!r}")
            command = _runs(step)
            for swallow in ("|| true", "; true", "|| :", "|| exit 0"):
                if swallow in command:
                    problems.append(f"gate 命令里有 {swallow!r}:退出码被吞掉了")
        for step in _steps(gate):
            if step.get("continue-on-error"):
                problems.append(f"gate job 的步骤带了 continue-on-error: {step.get('name')!r}")
        if gate.get("continue-on-error"):
            problems.append("gate job 整体带了 continue-on-error:门禁红会被改成黄")

        # ⑤ gate 必须先取回各任务的报告
        download_index = next(
            (
                index
                for index, step in enumerate(_steps(gate))
                if str(step.get("uses") or "").startswith(DOWNLOAD)
            ),
            None,
        )
        gate_index = next(
            (
                index
                for index, step in enumerate(_steps(gate))
                if RUN_GATE in _runs(step)
            ),
            None,
        )
        if download_index is None:
            problems.append("gate job 没有取回任务 job 的报告(download-artifact)")
        elif gate_index is not None and download_index > gate_index:
            problems.append("gate 在取回报告之前就跑了门禁")

    # ⑧ 资产目录必须有人校验:它入库,但没人跑 --verify 就一定会漂(15 加 tag 漂过一次)
    verify_steps = [
        step
        for workflow in workflows.values()
        for job in (workflow.get("jobs") or {}).values()
        for step in _steps(job)
        if RUN_CATALOG in _runs(step)
    ]
    if not verify_steps:
        problems.append("没有 job 校验资产目录(缺 `python tools/catalog.py --verify`)")
    for step in verify_steps:
        if "--verify" not in _runs(step):
            problems.append(f"catalog 步骤没有 --verify: {_runs(step)!r}")
        if step.get("continue-on-error"):
            problems.append("catalog 校验步骤带了 continue-on-error:漂了也不会红")
        if "if" in step:
            problems.append(f"catalog 校验步骤带了 if:{step['if']!r}(条件性校验等于没校验)")

    # ④ 每个任务 job 都上传产物
    for name, job in task_jobs.items():
        if not any(str(step.get("uses") or "").startswith(UPLOAD) for step in _steps(job)):
            problems.append(f"任务 job {name} 没有上传产物(artifact)")

    # ⑥ 任务 job 必须显式给 --root,而且必须给 workspace 表达式:
    #    09 之后 --root 的语义是「项目目录」。不写它,探测落到当前目录 —— CI 里当前目录恰好是
    #    workspace,今天能过、换个 job 配置就过不了。这类「碰巧对」必须用断言钉住。
    for name, job in task_jobs.items():
        for step in _steps(job):
            command = _runs(step)
            if RUN_TASK not in command:
                continue
            if "--root" not in command:
                problems.append(
                    f"任务 job {name} 没有 --root:CI 里「当前目录恰好是 workspace」能过,"
                    f"换个 job 配置就过不了"
                )
            elif WORKSPACE not in command:
                problems.append(
                    f"任务 job {name} 的 --root 不是 {WORKSPACE}:显式值才不受「当前目录」影响"
                )

    # 任务 job 必须把证据落到 gate 能找的地方(否则 summary.json 散在时间戳目录里)
    for name, job in task_jobs.items():
        for step in _steps(job):
            if RUN_TASK in _runs(step) and "--evidence-dir" not in _runs(step):
                problems.append(
                    f"任务 job {name} 没有 --evidence-dir:报告会落进 evidence/<时间戳>/,gate 找不到"
                )

    # ⑨⑩ 装成包以后的路 + 版本下界:两条都不在"任务 job"里,但同样会变成"永远绿"
    problems.extend(_installed_run_problems(workflows))
    problems.extend(_lower_bound_problems(workflows))

    return problems


# ------------------------------------------------------------- 真实工作流 ----

def test_the_committed_workflows_are_compliant():
    problems = violations(_workflows())
    assert problems == [], "\n".join(problems)


def test_there_is_at_least_one_workflow_and_it_triggers_on_push_and_pr():
    workflows = _workflows()
    assert workflows, "没有任何 .github/workflows/*.yml"

    # `on:` 在 YAML 1.1 里会被解析成布尔 True(PyYAML 的坑),两种都认
    triggers = set()
    for workflow in workflows.values():
        written = workflow.get("on", workflow.get(True))
        if isinstance(written, str):
            triggers.add(written)
        elif isinstance(written, Mapping):
            triggers.update(written)
        elif isinstance(written, list):
            triggers.update(written)
    assert {"push", "pull_request"} <= triggers, f"漏了触发器: {sorted(triggers)}"


# --------------------------------------------- 变异:结构一坏就必须红 ----

def _first_workflow() -> dict:
    name, workflow = next(iter(_workflows().items()))
    return copy.deepcopy(workflow)


def test_dropping_one_needs_entry_is_caught():
    """② 的变异:gate 少 need 一个任务 job —— 那个任务的结论就静默不参与判定。"""
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)
    dropped = gate["needs"].pop()

    problems = violations({"mutated.yml": workflow})

    assert any("needs 漏了" in problem for problem in problems), problems
    assert any(dropped in problem for problem in problems), problems


def test_an_if_on_the_gate_step_is_caught():
    """③ 的变异:`if: always()` 让门禁在失败时也照跑并给出绿。"""
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)
    gate_step = next(step for step in gate["steps"] if RUN_GATE in str(step.get("run") or ""))
    gate_step["if"] = "always()"

    problems = violations({"mutated.yml": workflow})

    assert any("带了 if" in problem for problem in problems), problems


def test_continue_on_error_on_the_gate_is_caught():
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)
    gate_step = next(step for step in gate["steps"] if RUN_GATE in str(step.get("run") or ""))
    gate_step["continue-on-error"] = True

    problems = violations({"mutated.yml": workflow})

    assert any("continue-on-error" in problem for problem in problems), problems


def test_a_swallowed_exit_code_is_caught():
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)
    gate_step = next(step for step in gate["steps"] if RUN_GATE in str(step.get("run") or ""))
    gate_step["run"] = str(gate_step["run"]) + " || true"

    problems = violations({"mutated.yml": workflow})

    assert any("退出码被吞" in problem for problem in problems), problems


def test_removing_a_task_job_is_caught():
    """① 的变异:任务文件还在,但工作流里没有 job 跑它。"""
    workflow = _first_workflow()
    task_job_name = next(name for name, job in workflow["jobs"].items() if "gate" not in name)
    del workflow["jobs"][task_job_name]

    problems = violations({"mutated.yml": workflow})

    assert any("没有任何 job 跑它" in problem for problem in problems), problems


def test_missing_root_on_a_task_job_is_caught():
    """⑥ 的变异:去掉 --root —— CI 里当前目录恰好是 workspace,于是"碰巧"还能过。"""
    workflow = _first_workflow()
    task_job = next(job for name, job in workflow["jobs"].items() if "gate" not in name)
    step = next(step for step in task_job["steps"] if RUN_TASK in _runs(step))
    step["run"] = str(step["run"]).replace("--root ${{ github.workspace }} ", "")

    problems = violations({"mutated.yml": workflow})

    assert any("没有 --root" in problem for problem in problems), problems


def test_a_root_that_is_not_the_workspace_is_caught():
    """显式了但写得含糊(--root .)同样不可接受:那还是靠当前目录。"""
    workflow = _first_workflow()
    task_job = next(job for name, job in workflow["jobs"].items() if "gate" not in name)
    step = next(step for step in task_job["steps"] if RUN_TASK in _runs(step))
    step["run"] = str(step["run"]).replace("${{ github.workspace }}", ".")

    problems = violations({"mutated.yml": workflow})

    assert any("不是 ${{ github.workspace }}" in problem for problem in problems), problems


def test_the_gate_is_never_turned_into_a_warning():
    """gate 的退出码唯一:0 合格 / 1 不合格 / 2 无效 / 3 未完成 / 4 平台错误 / 5 没跑过 /
    6 没有验收任务。把其中任何一档(尤其 6)当"只警告"就是把红灯改成灯不亮。

    这条读的是**真实工作流**:gate job 及其每一步都不许 continue-on-error,
    gate 命令也不许吞退出码。
    """
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)

    assert not gate.get("continue-on-error"), "gate job 不许 continue-on-error"
    for step in gate["steps"]:
        assert not step.get("continue-on-error"), f"gate 步骤不许 continue-on-error: {step}"
    gate_step = next(step for step in gate["steps"] if RUN_GATE in str(step.get("run") or ""))
    assert "if" not in gate_step, f"gate 步骤不许带 if:{gate_step['if']!r}"
    for swallow in ("|| true", "; true", "|| :", "|| exit 0", "|| exit 6"):
        assert swallow not in str(gate_step["run"]), f"gate 命令不许吞退出码:{swallow}"


def test_the_catalog_is_verified_in_ci():
    """入库的生成物必须有人校验 —— 否则 15 那次"加了 tag 没重新生成"会一直重复。"""
    runs = [
        _runs(step)
        for workflow in _workflows().values()
        for job in (workflow.get("jobs") or {}).values()
        for step in _steps(job)
    ]

    assert any(RUN_CATALOG in run and "--verify" in run for run in runs), (
        "工作流里没有 `python tools/catalog.py --verify`"
    )


def test_removing_the_catalog_verify_step_is_caught():
    """⑧ 的变异:删掉那一步 → 目录漂了也没人知道。"""
    workflow = _first_workflow()
    for job in workflow["jobs"].values():
        job["steps"] = [
            step
            for step in job.get("steps") or []
            if RUN_CATALOG not in _runs(step)
        ]

    problems = violations({"mutated.yml": workflow})

    assert any("没有 job 校验资产目录" in problem for problem in problems), problems


def test_a_conditional_catalog_verify_is_caught():
    """带 if 的校验 = 条件性校验:有的 PR 上不跑,漂了也绿。"""
    workflow = _first_workflow()
    for job in workflow["jobs"].values():
        for step in _steps(job):
            if RUN_CATALOG in _runs(step):
                step["if"] = "github.event_name == 'push'"

    problems = violations({"mutated.yml": workflow})

    assert any("条件性校验" in problem for problem in problems), problems


def test_removing_an_upload_step_is_caught():
    """④ 的变异:任务 job 不上传产物 —— gate 拿不到报告。"""
    workflow = _first_workflow()
    task_job = next(job for name, job in workflow["jobs"].items() if "gate" not in name)
    task_job["steps"] = [
        step for step in task_job["steps"] if not str(step.get("uses") or "").startswith(UPLOAD)
    ]

    problems = violations({"mutated.yml": workflow})

    assert any("没有上传产物" in problem for problem in problems), problems


def test_removing_the_download_step_is_caught():
    """⑤ 的变异:gate 不去取别人的报告,它只看得到自己 job 里的东西。"""
    workflow = _first_workflow()
    gate = next(job for name, job in workflow["jobs"].items() if "gate" in name)
    gate["steps"] = [
        step for step in gate["steps"] if not str(step.get("uses") or "").startswith(DOWNLOAD)
    ]

    problems = violations({"mutated.yml": workflow})

    assert any("没有取回" in problem for problem in problems), problems


# ------------------------- ⑨ 装成包以后的路必须在 CI 上走一遍(票 09c)----

def _job_with(workflow: dict, marker: str) -> dict:
    return next(
        job
        for job in workflow["jobs"].values()
        if any(marker in _runs(step) for step in _steps(job))
    )


def test_the_install_job_runs_the_wheel_and_a_two_sided_negative_case():
    """真实工作流:打 wheel → 一次性 venv 装 → 正例(项目目录没有 tests_support/)+ 负例。"""
    job = _job_with(_first_workflow(), BUILD_WHEEL)
    runs = [_runs(step) for step in _steps(job)]

    assert any("venv" in run and "pip install" in run and ".whl" in run for run in runs), runs
    assert any(INSTALLED_CLI in run for run in runs), "正例没跑装完以后的入口"
    negative = next(run for run in runs if PACKAGED_PRODUCT in run and "rm " in run)
    assert NEGATIVE_EXIT in negative and NEGATIVE_MESSAGE in negative, negative


def test_removing_the_install_job_is_caught():
    """⑨ 的变异:装机那条路没了 —— 源码形态全绿,装完缺文件谁也不知道。"""
    workflow = _first_workflow()
    name = next(
        name
        for name, job in workflow["jobs"].items()
        if any(BUILD_WHEEL in _runs(step) for step in _steps(job))
    )
    del workflow["jobs"][name]

    problems = violations({"mutated.yml": workflow})

    assert any("没有 job 打 wheel" in problem for problem in problems), problems


def test_a_negative_case_that_only_checks_non_zero_is_caught():
    """⑨ 的变异:负例只断「非 0」—— 路径错 / YAML 错 / 缺别的文件也非 0。"""
    workflow = _first_workflow()
    step = next(
        step for step in _job_with(workflow, PACKAGED_PRODUCT)["steps"] if PACKAGED_PRODUCT in _runs(step)
    )
    step["run"] = str(step["run"]).replace("-ne 4", "-ne 0").replace(NEGATIVE_EXIT, "-ne 0")

    problems = violations({"mutated.yml": workflow})

    assert any("只断「非 0」" in problem for problem in problems), problems


def test_a_negative_case_without_the_message_assertion_is_caught():
    """⑨ 的变异:exit 4 可能因为任何原因,不断消息就分不清「包内缺」还是别的平台错误。"""
    workflow = _first_workflow()
    step = next(
        step for step in _job_with(workflow, PACKAGED_PRODUCT)["steps"] if PACKAGED_PRODUCT in _runs(step)
    )
    step["run"] = "\n".join(
        line for line in str(step["run"]).splitlines() if NEGATIVE_MESSAGE not in line
    )

    problems = violations({"mutated.yml": workflow})

    assert any("没断消息含" in problem for problem in problems), problems


def test_using_the_broken_venv_after_the_negative_case_is_caught():
    """⑨ 的变异:负例之后又用那个 venv —— 它已经是「装坏了」的状态,后面跑什么都说明不了。"""
    workflow = _first_workflow()
    job = _job_with(workflow, PACKAGED_PRODUCT)
    index = next(
        index for index, step in enumerate(job["steps"]) if PACKAGED_PRODUCT in _runs(step)
    )
    job["steps"].append({"name": "再跑一次", "run": '"$RUNNER_TEMP/venv-install/bin/python" -m pytest'})

    problems = violations({"mutated.yml": workflow})

    assert any("负例之后又用了那个 venv" in problem for problem in problems), problems


# ------------------------------------ ⑩ 下界必须有 job 在跑(票 09c)----

def test_the_python_lower_bound_has_a_job_running_the_full_suite():
    workflow = _first_workflow()
    job = next(job for job in workflow["jobs"].values() if LOWER_BOUND in _setup_python_versions(job))
    runs = [_runs(step) for step in _steps(job)]

    assert any(FULL_SUITE in run for run in runs), runs


def test_removing_the_lower_bound_job_is_caught():
    """⑩ 的变异:没有 job 跑 3.10 —— `requires-python` 的下界就只是一句声明。"""
    workflow = _first_workflow()
    name = next(
        name
        for name, job in workflow["jobs"].items()
        if LOWER_BOUND in _setup_python_versions(job)
    )
    del workflow["jobs"][name]

    problems = violations({"mutated.yml": workflow})

    assert any(f"没有 job 在 Python {LOWER_BOUND}" in problem for problem in problems), problems


def test_an_unquoted_lower_bound_that_yaml_reads_as_a_float_is_caught():
    """⑩ 的变异:`python-version: 3.10` 不加引号 → YAML 读成 3.1,setup-python 去找 3.1。"""
    workflow = _first_workflow()
    job = next(job for job in workflow["jobs"].values() if LOWER_BOUND in _setup_python_versions(job))
    step = next(
        step
        for step in job["steps"]
        if str(step.get("uses") or "").startswith("actions/setup-python")
    )
    step["with"]["python-version"] = 3.1

    problems = violations({"mutated.yml": workflow})

    assert any(f"没有 job 在 Python {LOWER_BOUND}" in problem for problem in problems), problems


# --------------------------------------------------- 发布加固(票 09g) ----

def test_publish_only_happens_in_ci_with_provenance():
    """发布只有一条路:CI 的 publish job,带 id-token: write + --provenance。

    本地不许有发布路径(没有 npm script、没有 tools 脚本里调 npm publish)——
    否则一次手滑就把带客户数据的包推上去了。
    """
    workflow = _first_workflow()
    publish_jobs = {
        name: job
        for name, job in workflow["jobs"].items()
        if any("npm publish" in _runs(step) for step in _steps(job))
    }

    assert list(publish_jobs) == ["publish"], f"发布 job 只许有一个且叫 publish: {sorted(publish_jobs)}"
    publish = publish_jobs["publish"]
    assert (publish.get("permissions") or {}).get("id-token") == "write", "provenance 需要 id-token: write"
    gate_step = next(step for step in _steps(publish) if "npm publish" in _runs(step))
    assert "--provenance" in str(gate_step["run"]), "发布必须带 --provenance"
    assert publish.get("needs"), "发布必须挂在门禁之后"
    assert str(publish.get("if", "")).startswith("startsWith(github.ref"), "发布只许由 tag 触发"

    # 仓库里不许有本地发布入口
    npm_package = json.loads((REPO / "npm" / "package.json").read_text(encoding="utf-8"))
    for name, command in (npm_package.get("scripts") or {}).items():
        assert "publish" not in command, f"npm script {name!r} 里有发布命令"
    for path in (REPO / "tools").glob("*.py"):
        assert "npm publish" not in path.read_text(encoding="utf-8"), f"{path.name} 里有发布命令"


def test_the_wheel_hash_is_recorded_and_checked_before_install():
    """prepack 把 sha256 写进 package.json,装之前比对 —— 包被动过就当面停。"""
    prepack = (REPO / "npm" / "cli" / "prepack.js").read_text(encoding="utf-8")
    lib = (REPO / "npm" / "cli" / "lib.js").read_text(encoding="utf-8")

    assert "sha256" in prepack and "wheel.json" in prepack, "prepack 把指纹写进 dist/wheel.json"
    assert "verifyWheel(wheel)" in lib, "装之前必须校验 wheel 指纹"
    assert "wheelTampered" in lib, "指纹不一致要有专门的错误类型"
