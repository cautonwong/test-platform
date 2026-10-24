"""
发布前内容检查(票 09g)的反向验证:给它一个**脏**轮子,它必须报红。

"脚本存在"不算验收 —— 得证明它会拦。这里现造一个 wheel(zip),往 `_data/` 里塞一个假的
客户文件,断言扫描能点名"包内带了不该随包分发的数据"。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO / "tools"))

import check_release_content  # noqa: E402


def _fake_wheel(root: Path, extra: dict[str, str]) -> Path:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    wheel = dist / "cautonwong_testplatform-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("testplatform/__init__.py", "")
        for name, text in extra.items():
            archive.writestr(name, text)
    return wheel


def test_a_clean_wheel_passes(tmp_path):
    _fake_wheel(tmp_path, {"testplatform/_data/configs/clauses.yaml": "clauses: {}\n"})

    assert check_release_content.scan_package(tmp_path) == []


def test_a_customer_file_smuggled_into_data_is_reported(tmp_path):
    """`_data/` 里塞一份客户的清单 → 必须报红,并指出是哪个文件。"""
    _fake_wheel(
        tmp_path,
        {"testplatform/_data/configs/customer_acme.yaml": "devices:\n  - id: acme_01\n"},
    )

    findings = check_release_content.scan_package(tmp_path)

    assert any("customer_acme.yaml" in finding for finding in findings), findings


def test_a_customer_name_inside_a_shipped_file_is_reported(tmp_path):
    _fake_wheel(
        tmp_path,
        {"testplatform/_data/configs/mock_registers.yaml": "# 客户名称: 某某电力\n"},
    )

    findings = check_release_content.scan_package(tmp_path)

    assert any("客户名称" in finding for finding in findings), findings


def test_npm_does_not_fall_back_to_the_repository_gitignore():
    """仓库根的 `.gitignore` 忽略了 `npm/dist/`,而 dist/ 是发布包必须带的。

    npm 在没有 `.npmignore` 时会回退到 `.gitignore` → wheel 被静默排除(实测过:
    `npm pack --dry-run` 只列出 4 个文件,没有 dist/)。所以 `npm/.npmignore` 必须存在。
    """
    assert (REPO / "npm" / ".npmignore").is_file(), "缺 npm/.npmignore → dist/ 会被 gitignore 排除"


def test_the_packed_artifact_carries_the_fingerprint_that_matches_the_wheel():
    """`npm pack` 里必须有 `dist/wheel.json`,且与 `dist/*.whl` 的 sha256 一致。

    指纹只写在 dist/(与 wheel 同生同灭):写进 package.json 会让每次打包都脏工作树,
    而"仓库里的 package.json 是干净的"本身是可信度的一部分。
    """
    import hashlib
    import json
    import subprocess

    npm = REPO / "npm"
    if not (npm / "dist").is_dir():
        import pytest

        pytest.skip("先跑一次 npm run prepack(这条验的是打包产物)")
    # Windows 上 npm 是 npm.CMD,必须经 shell 起
    tarball = subprocess.run(
        "npm pack --dry-run --ignore-scripts",
        cwd=npm,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # npm 的文件清单走 stderr(npm notice)
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert tarball.returncode == 0, tarball.stderr
    assert "dist/wheel.json" in tarball.stdout, "包里没有指纹文件"
    assert ".whl" in tarball.stdout, "包里没有 wheel"

    fingerprint = json.loads((npm / "dist" / "wheel.json").read_text(encoding="utf-8"))
    wheel = npm / "dist" / fingerprint["file"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == fingerprint["sha256"]
    # 仓库里不追踪 dist/(.gitignore);prepack 也不许改 package.json
    assert "wheel" not in json.loads((npm / "package.json").read_text(encoding="utf-8"))
