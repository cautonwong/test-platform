"""统一入口:testplatform run / gate / --version 转发到已有 main,退出码不另写一套。"""
import os
import re
import subprocess
import sys
from pathlib import Path

from testplatform import __version__

REPO = Path(__file__).resolve().parents[1]
PYTHONPATH = str(REPO / "src")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "testplatform.cli", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": PYTHONPATH},
        timeout=120,
    )


def test_version_is_printed_and_matches_pyproject():
    result = _run("--version")

    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
    declared = re.search(
        r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    assert declared and declared.group(1) == __version__


def test_help_lists_the_unified_commands():
    result = _run("--help")

    assert result.returncode == 0, result.stderr
    for command in ("run", "gate", "scaffold", "eject"):
        assert command in result.stdout


def test_run_forwards_to_the_task_entry_and_keeps_its_exit_code(tmp_path):
    result = _run("run", str(tmp_path / "missing.yaml"))

    assert result.returncode == 4, result.stdout + result.stderr
    assert "任务文件" in result.stdout + result.stderr


def test_gate_forwards_to_the_gate_entry_and_keeps_its_exit_code(tmp_path):
    result = _run("gate", str(tmp_path))

    assert result.returncode == 5, result.stdout + result.stderr


def test_run_help_comes_from_the_task_entry():
    result = _run("run", "--help")

    assert result.returncode == 0, result.stderr
    assert "--evidence-dir" in result.stdout


def test_cases_lists_the_project_cases_and_filters_by_tag(tmp_path):
    from testplatform import scaffold

    project = scaffold.scaffold("case", tmp_path / "project")
    listed = _run("cases", "--root", str(project))
    assert listed.returncode == 0, listed.stderr
    assert "T_00_00_90_000001_01" in listed.stdout
    assert "SCAFFOLD-1" in listed.stdout

    tagged = _run("cases", "--root", str(project), "--tag", "样板")
    assert tagged.returncode == 0, tagged.stderr
    assert "T_00_00_90_000001_01" in tagged.stdout

    missing = _run("cases", "--root", str(project), "--tag", "没有这个标签")
    assert missing.returncode == 0, missing.stderr
    assert "没有匹配的用例" in missing.stdout


def test_cases_reports_a_syntax_error_instead_of_dropping_the_file(tmp_path):
    from testplatform import scaffold

    project = scaffold.scaffold("case", tmp_path / "project")
    (project / "tests" / "test_bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    failed = _run("cases", "--root", str(project))

    assert failed.returncode == 2, failed.stdout + failed.stderr
    assert "test_bad.py" in failed.stderr


def test_tasks_lists_and_validates_the_project_tasks(tmp_path):
    from testplatform import scaffold

    project = scaffold.scaffold("case", tmp_path / "project")
    listed = _run("tasks", "--root", str(project))
    assert listed.returncode == 0, listed.stderr
    assert "case_sample" in listed.stdout
    assert "smoke" in listed.stdout

    (project / "tasks" / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    broken = _run("tasks", "--root", str(project))
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert "加载失败" in broken.stderr


def test_tasks_filters_by_kind(tmp_path):
    from testplatform import scaffold

    project = scaffold.scaffold("case", tmp_path / "project")
    listed = _run("tasks", "--root", str(project), "--kind", "acceptance")
    assert listed.returncode == 0, listed.stderr
    assert "case_sample" not in listed.stdout
