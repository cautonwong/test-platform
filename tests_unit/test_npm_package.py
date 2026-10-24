"""
`npx` 壳(票 09f)的机械验收,从 Python 这一侧看。

JS 自己的断言在 `npm/test/cli.test.js`(node --test);这里保证**主套件**也能拦住:
包的形状、没有 postinstall、prepack 真能产出带版本号的 wheel、以及 node 测试真的跑绿。
没装 node / python-build 时跳过并说明原因 —— 跳过不是通过,报错时才要人去看。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NPM = REPO / "npm"


def _package() -> dict:
    return json.loads((NPM / "package.json").read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml 里没有 version")


# ------------------------------------------------------------- 包的形状 ----

def test_the_npm_package_is_shaped_like_a_shell():
    package = _package()

    assert package["name"] == "@cautonwong/testplatform"
    assert package["version"] == _pyproject_version(), "npm 版本必须和轮子一致"
    assert "cli/" in package["files"] and "dist/" in package["files"]
    assert package["bin"]["testplatform"] == "cli/index.js"
    # 没有 postinstall:安装时什么都不做,第一次跑命令才建环境
    assert "postinstall" not in package["scripts"]
    assert "postinstall" not in package, "不许有顶层 postinstall"


def test_the_subcommands_are_forwarded_not_reimplemented():
    """四个子命令在 JS 里只允许出现"转发参数"这一层:一次 grep 就能看出没重写业务逻辑。"""
    cli = (NPM / "cli" / "lib.js").read_text(encoding="utf-8")

    for entry in ("testplatform.execution.cli", "testplatform.execution.gate", "testplatform.cli"):
        assert entry in cli, f"{entry} 没被转发"
    # 业务名词(判据 / 证据 / 条款)不该出现在壳里
    for word in ("判据", "证据", "clause", "evidence"):
        assert word not in cli, f"壳里出现了业务概念 {word!r}:那说明它在重写逻辑"


# --------------------------------------------------------------- prepack ----

def test_prepack_builds_a_wheel_named_after_the_version(tmp_path):
    """`npm run prepack` 真产出 `dist/*.whl`,文件名带版本号 —— 否则用户装到的是别的代码。"""
    pytest.importorskip("build", reason="没有 python -m build,跳过打包(安装它才能验)")
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("本机没有 npm")

    completed = subprocess.run(
        [npm, "run", "prepack"],
        cwd=NPM,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "TESTPLATFORM_BUILD_PYTHON": sys.executable},
        timeout=600,
    )

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    wheels = list((NPM / "dist").glob("*.whl"))
    assert len(wheels) == 1, f"dist/ 里应该正好一个 wheel: {wheels}"
    assert _package()["version"] in wheels[0].name, wheels[0].name
    # build 的残留会让 importlib.metadata 把平台当成"已安装"(entry point 生效),
    # 同一轮 pytest 里插件就被注册两次 —— prepack 必须自己清干净
    assert not (REPO / "build").exists(), "prepack 留下了 build/"
    assert not list((REPO / "src").glob("*.egg-info")), "prepack 留下了 src/*.egg-info"


# --------------------------------------------------- JS 侧测试真的在跑 ----

def test_the_node_suite_passes():
    """`node --test npm/test/` 必须绿:转发参数、三类失败消息、没有 postinstall 都在那里。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 node")

    completed = subprocess.run(
        [node, "--test", str(NPM / "test" / "cli.test.js")],
        cwd=NPM,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHON": sys.executable},
        timeout=300,
    )

    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-2000:]
    assert "# fail 0" in completed.stdout
