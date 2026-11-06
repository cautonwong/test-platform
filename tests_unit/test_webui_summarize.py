"""tests_web 的证据汇总:0 条证据与坏证据都不能算通过。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tests_web" / "summarize.py"


def _run(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_zero_evidence_is_not_a_pass(tmp_path):
    """一条用例都没跑(全部 skip)时,汇总脚本必须红 —— 那是"未完成",不是"全过"。"""
    result = _run(tmp_path)

    assert result.returncode != 0, result.stdout
    assert "未完成" in result.stdout
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["total"] == 0


def test_one_valid_case_is_a_pass(tmp_path):
    (tmp_path / "WUI_00_00_01_000001.json").write_text(
        json.dumps({"caseId": "WUI_00_00_01_000001", "clause": "DLT645-4.5.2", "verdict": "product_ok"}),
        encoding="utf-8",
    )
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "1/1 product_ok" in result.stdout


def test_broken_evidence_is_not_a_pass(tmp_path):
    (tmp_path / "WUI_00_00_01_000001.json").write_text("{not json", encoding="utf-8")
    result = _run(tmp_path)

    assert result.returncode != 0, result.stdout


def test_default_evidence_dir_matches_the_writer(tmp_path):
    """脚本的缺省目录必须就是 webui_helpers 写证据的目录,否则 README 的命令永远找不到证据。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0, result.stdout
    assert "evidence/webui-e2e" in result.stdout
