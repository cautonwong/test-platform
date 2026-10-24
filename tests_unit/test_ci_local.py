"""
`tools/ci_local.py` 的守卫(票 10b):命令必须来自 YAML、跳过必须显式、失败不许被跳过掩盖。

这里全部用**临时工作流**跑:真的去跑仓库那份要几分钟,而且会装东西。仓库那份只做 `--dry-run`
结构检查(能解析、能按 needs 排队、把 CI 专属步骤列出来)。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import ci_local  # noqa: E402


def _workflow(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _run(tmp_path: Path, text: str, *, dry_run: bool = False, capsys=None):
    workflow = ci_local.load(_workflow(tmp_path, text))
    planned = ci_local.plan(workflow, repo=tmp_path)
    result = ci_local.run(planned, repo=tmp_path, dry_run=dry_run)
    return planned, result


# ------------------------------------------------------------ 顺序与命令 ----

def test_jobs_run_in_needs_order_and_commands_come_from_the_yaml(tmp_path):
    """顺序按 needs;命令是 YAML 里那条字符串(这里写文件,后面断言内容)。"""
    _planned, result = _run(
        tmp_path,
        """
        jobs:
          gate:
            needs: [task]
            steps:
              - run: python -c "open('order.txt','a').write('gate\\n')"
          task:
            steps:
              - run: python -c "open('order.txt','a').write('task\\n')"
        """,
    )

    assert result.exit_code == 0
    assert (tmp_path / "order.txt").read_text(encoding="utf-8").splitlines() == ["task", "gate"]


def test_the_workspace_expression_is_replaced_with_the_project_path(tmp_path):
    """`${{ github.workspace }}` 换成本地仓库根 —— 命令别的不许改。"""
    _planned, result = _run(
        tmp_path,
        """
        jobs:
          task:
            steps:
              - run: python -c "import pathlib,sys; pathlib.Path('workspace.txt').write_text(sys.argv[1])" "${{ github.workspace }}"
        """,
    )

    assert result.exit_code == 0
    assert (tmp_path / "workspace.txt").read_text(encoding="utf-8") == str(tmp_path)


# ------------------------------------------------------------ 跳过要显式 ----

def test_container_actions_are_skipped_with_a_reason(capsys, tmp_path):
    """`uses:` 是容器动作:本地不执行,但必须打印原因并进汇总。"""
    planned, result = _run(
        tmp_path,
        """
        jobs:
          task:
            steps:
              - uses: actions/checkout@v4
              - run: python -c "pass"
        """,
    )
    lines = ci_local.summary(result)

    assert result.exit_code == 0
    assert ("task/actions/checkout@v4", "uses: 容器动作,本地不可执行") in result.skipped
    assert any("checkout" in line and "容器动作" in line for line in lines), lines
    assert any(line.startswith("跳过的 step") for line in lines), lines


def test_ci_only_variables_make_a_step_locally_unsupported(tmp_path):
    """用了 RUNNER_TEMP 这类 CI 变量的 step:本地没有同一个值,跑它等于跑另一条命令。"""
    planned, result = _run(
        tmp_path,
        """
        jobs:
          install:
            steps:
              - run: python -m venv "$RUNNER_TEMP/venv"
        """,
    )

    assert result.exit_code == 0, "跳过不算失败,但必须被列出来"
    assert result.skipped and "RUNNER_TEMP" in result.skipped[0][1]
    assert result.executed == []


def test_an_explicit_local_unsupported_marker_is_listed(tmp_path):
    _planned, result = _run(
        tmp_path,
        """
        jobs:
          task:
            steps:
              - run: pip install pytest
                local: unsupported 装依赖会改动本机环境
        """,
    )

    assert result.skipped == [("task/pip install pytest", "unsupported 装依赖会改动本机环境")]


# --------------------------------------------------------- 失败不许被掩盖 ----

def test_a_failing_step_stops_the_run_with_a_nonzero_code(tmp_path):
    _planned, result = _run(
        tmp_path,
        """
        jobs:
          task:
            steps:
              - run: python -c "open('before.txt','w').write('1')"
              - run: python -c "import sys; sys.exit(1)"
              - run: python -c "open('after.txt','w').write('1')"
        """,
    )

    assert result.exit_code != 0
    assert result.failures and result.failures[0][1] == 1
    assert (tmp_path / "before.txt").exists()
    assert not (tmp_path / "after.txt").exists(), "失败之后不该继续跑"
    # **失败的那一步不能出现在跳过清单里**(跳过不能掩盖失败)
    assert all("sys.exit(1)" not in name for name, _reason in result.skipped)


def test_an_unknown_expression_is_skipped_rather_than_guessed(tmp_path):
    _planned, result = _run(
        tmp_path,
        """
        jobs:
          task:
            steps:
              - run: python -c "print('${{ secrets.TOKEN }}')"
        """,
    )

    assert result.executed == []
    assert "secrets.TOKEN" in result.skipped[0][1]


def test_a_needs_cycle_is_a_structural_error(tmp_path):
    workflow = ci_local.load(
        _workflow(
            tmp_path,
            """
            jobs:
              a: {needs: [b], steps: []}
              b: {needs: [a], steps: []}
            """,
        )
    )

    with pytest.raises(ci_local.WorkflowError):
        ci_local.plan(workflow, repo=tmp_path)


# --------------------------------------------------- 仓库里那份真工作流 ----

def test_the_real_workflow_parses_and_orders_jobs_without_running_them():
    """真工作流只做结构检查:命令都在、顺序排得出来、CI 专属步骤被点名。"""
    workflow = ci_local.load(REPO / ".github" / "workflows" / "tasks.yml")
    planned = ci_local.plan(workflow, repo=REPO)
    result = ci_local.run(planned, repo=REPO, dry_run=True)

    assert result.exit_code == 0
    jobs = [step.job for step in planned]
    assert jobs.index("gate") > jobs.index("task-minimal"), "gate 必须排在任务之后"
    # 每个 step 都必须在"执行"或"跳过(有理由)"里,不许无声消失
    assert len(planned) == len(result.executed) + len(result.skipped)
    for name, reason in result.skipped:
        assert reason, f"{name} 跳过了但没写理由"
