"""`_product_file`：命令行里哪一项才是产品文件。

这条判断决定证据里 `env.product_sha256` 是文件摘要还是 `unknown`，判错会让
"跑的是哪个二进制"无从追溯。两个真实的坑都来自产品启动命令行本身：
解释器不算产品文件、`python -c "<源码>"` 的源码项不是路径。
"""

from pathlib import Path

from testplatform.execution.task import _product_file


def test_finds_the_script_and_skips_the_interpreter(tmp_path):
    script = tmp_path / "product.py"
    script.write_text("print('hi')", encoding="utf-8")

    launch = ["python", "--startup-delay", "2", "product.py", "--port", "20001"]

    assert _product_file(launch, tmp_path) == script


def test_relative_arguments_resolve_against_the_project_root(tmp_path):
    script = tmp_path / "bin" / "product"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    assert _product_file(["./bin/product", "--port", "20001"], tmp_path) == script


def test_command_line_without_any_file_has_no_product_file(tmp_path):
    assert _product_file(["python", "-c", "print(1)"], tmp_path) is None


def test_argv_that_cannot_be_stat_ed_is_skipped_not_fatal(tmp_path, monkeypatch):
    """超长 argv 会让 stat 报 ENAMETOOLONG；平台只回答问题「它是不是文件」。"""
    script = tmp_path / "product.py"
    script.write_text("", encoding="utf-8")
    real_is_file = Path.is_file

    def fake_is_file(self):
        if len(str(self)) > 100:
            raise OSError(36, "File name too long")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)

    launch = ["python", "x" * 500, "product.py"]

    assert _product_file(launch, tmp_path) == script
