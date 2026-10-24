"""eject 之后 import 必须落在项目里(票 15a 打回那一条)。

**假溯源比不 eject 更坏**:报告写 `platform_source: project` + 一个 project 指纹,跑的却是
site-packages 里的旧 wheel —— 用户以为自己改了、结论按旧平台给。

所以这里用**真 venv、真 wheel、真 eject**:先按真实形态装上(wheel 的发行版名是
`cautonwong-testplatform`,editable 叫 `testplatform`,两个名字不同),再 eject,
断言三件:import 路径、`pip list` 只剩 editable、改一行指纹变(同一 venv 里验)。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from testplatform import eject as eject_module

# 真跑 venv + wheel + `pip install -e`:全局 60 秒是给普通用例的,全量负载下这条会撞线。
pytestmark = [pytest.mark.timeout(300), pytest.mark.slow]

REPO = pathlib.Path(__file__).resolve().parents[1]


def _run(python: pathlib.Path, *args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), *args], cwd=str(cwd), capture_output=True, text=True
    )


def _installed_names(python: pathlib.Path, cwd: pathlib.Path) -> set[str]:
    done = _run(python, "-m", "pip", "list", "--format=json", cwd=cwd)
    assert done.returncode == 0, done.stderr
    return {row["name"].lower() for row in json.loads(done.stdout)}


@pytest.fixture
def wheel_installed_project(tmp_path):
    """真装一遍 wheel 形态:venv 里有个只读的 site-packages/testplatform。"""
    project = tmp_path / "proj"
    project.mkdir()
    python = project / eject_module.STATE_DIR / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    done = _run(pathlib.Path(sys.executable), "-m", "venv", str(python.parents[1]), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    # 从**拷贝**装:在仓库里原地构建会在跑测试的同时把 src/*.egg-info 写进工作树,
    # 并行跑的"构建后仓库必须干净""用例 id 唯一"这些用例会因此变红(实测)。
    source = tmp_path / "repo_copy"
    shutil.copytree(REPO / "src", source / "src")
    shutil.copy(REPO / "pyproject.toml", source / "pyproject.toml")
    done = _run(python, "-m", "pip", "install", "--no-deps", "--quiet", str(source), cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    return project, python


def test_eject_makes_the_project_copy_the_one_that_imports(wheel_installed_project):
    project, python = wheel_installed_project
    # 装完先确认起点是对的:此刻 import 到的在 site-packages(这就是要修掉的形态)
    assert "site-packages" in eject_module.import_path(python).as_posix()

    eject_module.eject(project)

    target = project / eject_module.DEFAULT_DIR
    found = eject_module.import_path(python)
    assert found.is_relative_to(target), f"import 到的是 {found},不是 {target}"
    # 只剩 editable 那个:wheel 的发行版名不同,只卸 testplatform 会把它留在原地
    names = _installed_names(python, project)
    assert "testplatform" in names
    assert "cautonwong-testplatform" not in names
    assert not (eject_module._site_packages(python) / "testplatform").exists()


def test_a_change_in_the_project_copy_changes_the_digest_in_the_same_venv(wheel_installed_project):
    project, python = wheel_installed_project
    record = eject_module.eject(project)
    probe = (
        "from pathlib import Path; from testplatform.config import origin;"
        "print(origin.sha256_of(Path('.')))"
    )
    before = _run(python, "-c", probe, cwd=project)
    assert before.returncode == 0, before.stderr
    assert before.stdout.strip() == record["sha256"]
    assert json.loads((project / ".testplatform" / "ejected.json").read_text(encoding="utf-8"))[
        "sha256"
    ] == record["sha256"]

    edited = project / eject_module.DEFAULT_DIR / "testplatform" / "cli.py"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# 用户改的一行\n", encoding="utf-8")
    after = _run(python, "-c", probe, cwd=project)

    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() != record["sha256"], "改了平台代码,指纹却没变"
