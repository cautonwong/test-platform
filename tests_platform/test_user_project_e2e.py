"""
票 10d:QUICKSTART 那条**用户路径**的机器验收。

    建临时目录 → testplatform scaffold <kind> --out <dir>
    → testplatform-run-task <dir>/tasks/<kind>_sample.yaml --root <dir> → exit 0
    → testplatform gate <dir>/reports → exit 0

四个骨架各跑一遍。为什么值得单独一票:仓库自己的 `acceptance_sample.yaml` 在用户项目里跑不起来
(它的用例 import `tests_support/`,那个目录不随包分发)—— 文档改走 scaffold 之后,**没有机器在跑
这条路径**,下一次改动可以让它静默失效。

**必须用装好的形态跑**:一次性 venv + 包内 wheel + console scripts,不是 `PYTHONPATH=src`。
源码树里能跑通什么都证明不了"用户装完能用"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testplatform import scaffold

REPO = Path(__file__).resolve().parents[1]

# 这条用例的第一次 setup 要:打 wheel + 建一次性 venv + 补依赖 + 装 wheel(~60-90 s)。
# 默认套件给每条用例 60 s,所以这里显式放宽 —— 放宽的是**准备环境**,不是被测行为的断言。
pytestmark = pytest.mark.timeout(300)


def _run(command: list[str], cwd: Path, env: dict, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout
    )


def _fail(completed: subprocess.CompletedProcess, *, step: str, kind: str) -> str:
    return (
        f"{kind}: {step} 失败(真实退出码 {completed.returncode})\n"
        f"stdout:\n{completed.stdout[-1500:]}\nstderr:\n{completed.stderr[-1500:]}"
    )


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """一次装好的形态:包内 wheel + 一次性 venv(不联网、不写共享环境)→ console scripts。"""
    workspace = tmp_path_factory.mktemp("installed")
    build_python = sys.executable
    dist = workspace / "dist"
    # 在**仓库副本**里构建:`python -m build` 会在源码目录留下 build/ 与 src/*.egg-info,
    # 那两样会让 importlib.metadata 把平台当成"已安装"(entry point 生效),
    # 于是同一次测试里的子 pytest 会把插件注册两次 —— 绝不能在仓库里就地构建。
    source = workspace / "source"
    shutil.copytree(
        REPO,
        source,
        ignore=shutil.ignore_patterns(".git", ".venv", "evidence", "build", "*.egg-info", "__pycache__", "node_modules"),
    )
    built = _run([build_python, "-m", "build", "--wheel", "--outdir", str(dist)], source, os.environ, 600)
    assert built.returncode == 0, _fail(built, step="python -m build --wheel", kind="wheel")
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"应该正好一个 wheel: {wheels}"

    venv = workspace / "venv"
    # 用 **base** python 建 venv:项目自带的 .venv 曾被别的 worktree 的 `pip install -e` 污染过
    # (`__editable__*.pth` 指向别人的 src),--system-site-packages 会把那份污染一起继承进来。
    # 用当前解释器建 venv(--system-site-packages 才能复用 pytest/pyyaml,不联网)。
    # 它可能继承到被污染的 site-packages —— 所以下面那条"import 到的是新 venv 里那份"是硬断言。
    base_python = sys.executable
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # --system-site-packages:复用 base 里的 pytest / pyyaml,**不联网**装依赖
    created = _run([base_python, "-m", "venv", "--system-site-packages", str(venv)], workspace, os.environ)
    assert created.returncode == 0, _fail(created, step="python -m venv", kind="venv")
    # 依赖(pytest / pyyaml)从当前环境**复制**过去:不联网、也不拿 PYTHONPATH 糊过去。
    # 平台本体仍然只能来自那个 wheel —— 下面那条 import 断言会盯住这一点。
    import site as _site

    target_site = next(Path(item) for item in [str(python.parent.parent / "Lib" / "site-packages")] if item)
    if not target_site.exists():  # POSIX
        target_site = python.parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    # 整个依赖集照抄(不联网)。**不抄 testplatform 本体与任何 .pth** ——
    # 平台必须来自那个 wheel,`__editable__*.pth` 正是 09f 被坑的那种污染源。
    skip_prefixes = ("testplatform", "__editable__", "pip", "setuptools", "_distutils_", "pkg_resources")
    for source_site in map(Path, _site.getsitepackages()):
        for entry in source_site.iterdir():
            name = entry.name
            if name.startswith(skip_prefixes) or name.endswith((".pth", ".egg-link")):
                continue
            destination = target_site / name
            if destination.exists():
                continue
            if entry.is_dir():
                shutil.copytree(entry, destination, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(entry, destination)

    installed_wheel = _run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheels[0])], workspace, os.environ, 600)
    assert installed_wheel.returncode == 0, _fail(installed_wheel, step="pip install <wheel>", kind="wheel")

    # **硬断言**:import 到的必须是新 venv 里装的那份 —— 指回任何 worktree / 仓库 src 都是假绿
    where = _run([str(python), "-c", "import testplatform; print(testplatform.__file__)"], workspace, {**os.environ, "PYTHONPATH": ""})
    assert where.returncode == 0, _fail(where, step="import testplatform", kind="venv")
    module_path = Path(where.stdout.strip()).resolve()
    assert venv.resolve() in module_path.parents, f"import 到的不是新 venv 里那份: {module_path}"
    assert "worktrees" not in str(module_path) and REPO.resolve() not in module_path.parents, (
        f"import 到了源码树里的平台: {module_path}"
    )

    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # 关键:不许靠源码树的 sys.path(否则验到的是源码,不是装进去的那份)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    return {
        "workspace": workspace,
        "python": python,
        "scaffold": str(scripts / ("testplatform.exe" if os.name == "nt" else "testplatform")),
        "run_task": str(scripts / ("testplatform-run-task.exe" if os.name == "nt" else "testplatform-run-task")),
        "gate": str(scripts / ("testplatform.exe" if os.name == "nt" else "testplatform")),
        "env": env,
    }


@pytest.mark.parametrize("kind", scaffold.KINDS)
def test_a_user_can_scaffold_run_and_gate_after_installing(installed, kind):
    """装好的形态下:scaffold → run → gate,三步都 exit 0(失败消息带哪一步 / 哪个骨架 / 真实码)。"""
    project = installed["workspace"] / f"project-{kind}"
    project.mkdir()

    scaffolded = _run([installed["scaffold"], "scaffold", kind, "--out", str(project)], project, installed["env"])
    assert scaffolded.returncode == 0, _fail(scaffolded, step="scaffold", kind=kind)

    task = project / "tasks" / f"{kind}_sample.yaml"
    assert task.is_file(), f"{kind}: scaffold 没有生成 {task}"

    # 证据目录按 CI/QUICKSTART 的形状:`reports/<任务>/`(gate 按 `<目录>/*/summary.json` 找报告)
    evidence = project / "reports" / f"{kind}_sample"
    ran = _run(
        [installed["run_task"], str(task), "--root", str(project), "--evidence-dir", str(evidence)],
        project,
        installed["env"],
        300,
    )
    assert ran.returncode == 0, _fail(ran, step="run-task", kind=kind)

    # 断言不许只看退出码:报告要说"合格",资源要来自**包内**
    summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "clean", f"{kind}: 报告结论是 {summary['verdict']!r}"
    record = json.loads((evidence / "task.json").read_text(encoding="utf-8"))
    layout = record["layout"]
    # 执行器资源(模拟从站脚本、寄存器表)必须来自**包内** —— 这是"它真的用了装的那份"的证据。
    # 条款清单是 project:scaffold 给每个项目生成了一份自己的(用例的 clause 只认项目清单)。
    assert layout["slave_script"] == "package", f"{kind}: 模拟从站不是从包里来的: {layout}"
    assert layout["registers"] == "package", f"{kind}: 寄存器表不是从包里来的: {layout}"
    assert layout["cases"] == "project", f"{kind}: 用例必须来自项目目录: {layout}"


    # 门禁:**文档承诺 6**(QUICKSTART「第一次 gate 是 6」),这里就验 6 —— 改任一侧都会红。
    # 为什么是 6 而不是 0:scaffold 的 product 是平台的**示例**产品,不是 EdgeC 产品;
    # 让它判"验收通过"就是拿一个假产品给新人发合格证(05 第 6 档 / 09b 定 smoke 的理由)。
    gated = _run(
        [
            str(installed["python"]),
            "-c",
            "import sys; from testplatform.execution.gate import main; sys.exit(main(sys.argv[1:]))",
            str(project / "reports"),
        ],
        project,
        installed["env"],
        120,
    )
    assert gated.returncode == 6, _fail(gated, step="gate(期望 6)", kind=kind)
    assert "没有一条验收任务" in gated.stdout, gated.stdout
    assert f"{kind}_sample" in gated.stdout, f"{kind}: gate 要点名任务名"
    assert gated.stdout, f"{kind}: gate 没有输出"


def test_the_installed_platform_does_not_depend_on_the_source_tree(installed):
    """实证"不靠源码树":console script 的 PYTHONPATH 为空时仍然能跑 —— 它用的是装进去的那个包。"""
    completed = _run([installed["scaffold"], "--help"], installed["workspace"], installed["env"])
    assert completed.returncode == 0, _fail(completed, step="testplatform --help", kind="install")
    assert "scaffold" in completed.stdout


def test_building_the_wheel_leaves_the_repository_clean(installed):
    """构建在**仓库副本**里做 —— 构建完原仓库 `git status` 必须干净。

    为什么不就地构建:`python -m build` 会在源码目录留下 `build/` 与 `src/*.egg-info`,
    那两样让 importlib.metadata 把平台当成"已安装"(entry point 生效)→ 同一轮测试里的
    子 pytest 会把插件注册两次(09f 真踩过)。**下一个人别顺手省掉 copytree。**
    """
    status = _run(["git", "status", "--porcelain"], REPO, os.environ)
    assert status.returncode == 0, status.stderr
    # 只看构建该留下的东西(工作树里本来可能有被审的改动,那不是这条要管的)
    polluted = [
        line
        for line in status.stdout.splitlines()
        if any(mark in line for mark in ("build/", ".egg-info", "/dist/", "node_modules"))
    ]
    assert polluted == [], f"构建污染了原仓库: {polluted}"
    assert not (REPO / "build").exists(), "仓库里留下了 build/"
    assert not list((REPO / "src").glob("*.egg-info")), "仓库里留下了 src/*.egg-info"
