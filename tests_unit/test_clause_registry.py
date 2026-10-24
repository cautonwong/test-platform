"""
标准条款受控清单(票 07)的守卫:清单自身写得对不对、拼错的条款会不会在收集期停下、聚合按 id 不按文本。

条款的意义在于"能按条款聚合,且拼错当场报错"。所以在收集期校验,而不是在报告阶段才发现对不上 ——
那时结论已经发出去了。自由文本只留 `free:` 一个显式入口:兼容不能是"随便写文本也收",
否则没人会迁移。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from testplatform.cases import Case
from testplatform.clauses import (
    FREE_PREFIX,
    ClauseConfigError,
    default_clauses_path,
    group_by_clause,
    load_clauses,
)
from tests_support.manifest_factory import manifest
from tests_support.nested_pytest import pytest_command

REPO = Path(__file__).resolve().parent.parent
CLAUSES = default_clauses_path()


# ------------------------------------------------------------- 清单本身 ----

def test_every_clause_declares_standard_and_summary():
    """一条条款 = id + 标准 + 编号 + summary。summary 是用法概括,不能当标准原文用。"""
    registry = load_clauses(CLAUSES)
    assert len(registry.ids()) >= 10, f"条款清单只有 {len(registry.ids())} 条,像是被截断了"

    for clause_id in registry.ids():
        clause = registry.get(clause_id)
        assert clause.id == clause_id
        assert clause.standard and clause.summary, clause
        assert clause.label == f"{clause.standard} {clause.number}".strip()
        # summary 不得出现在 label 里:label 只给"标准 + 编号",人读的概括另有字段
        assert clause.summary not in clause.label


def test_an_unknown_id_is_rejected_and_the_available_ids_are_listed():
    """拼错的条款不能静默通过:报错必须能直接改(列出可用 id)。"""
    registry = load_clauses(CLAUSES)
    with pytest.raises(ClauseConfigError) as error:
        registry.resolve("DL/T 645 4.5.2")

    message = str(error.value)
    assert "DL/T 645 4.5.2" in message
    assert "DLT645-4.5.2" in message, f"报错里没给可用 id,改不动:{message}"


def test_free_prefix_is_the_only_way_to_keep_free_text():
    """自由文本必须显式标注:`free: 原文`。裸文本一律当拼错的 id。"""
    registry = load_clauses(CLAUSES)

    assert registry.resolve(f"{FREE_PREFIX} X 1.1") is None
    assert registry.is_free(f"{FREE_PREFIX} X 1.1")
    assert registry.free_text(f"{FREE_PREFIX}  X 1.1 ") == "X 1.1"
    # 只有前缀、没有原文 = 漏写,不算"合法的自由文本"
    assert not registry.is_free(f"{FREE_PREFIX}   ")
    assert not registry.is_free("X 1.1")
    with pytest.raises(ClauseConfigError):
        registry.resolve("X 1.1")


def test_group_by_clause_groups_by_id_and_keeps_free_text_apart():
    """报告与资产目录按条款聚合:同一个 id 下的用例归一组,自由文本不并进任何标准条款。"""
    cases = [
        Case(id="M_00_00_01_000002_09", clause="DLT645-4.5.2"),
        Case(id="M_00_00_01_000002_10", clause="DLT645-4.5.2"),
        Case(id="M_00_00_01_000003_01", clause="GBT34131-5.1.1"),
        Case(id="T_00_00_01_000000_01", clause=f"{FREE_PREFIX} 平台:任务闭环"),
    ]

    grouped = group_by_clause(cases)

    assert grouped["DLT645-4.5.2"] == ("M_00_00_01_000002_09", "M_00_00_01_000002_10")
    assert grouped["GBT34131-5.1.1"] == ("M_00_00_01_000003_01",)
    assert grouped[f"{FREE_PREFIX} 平台:任务闭环"] == ("T_00_00_01_000000_01",)
    assert set(grouped) == {"DLT645-4.5.2", "GBT34131-5.1.1", f"{FREE_PREFIX} 平台:任务闭环"}


@pytest.mark.parametrize(
    "written, expected",
    [
        ({"clauses": {"A-1": {"standard": "S", "number": "1"}}}, "缺字段"),
        ({"clauses": {"A-1": {"standard": "S", "number": "1", "summary": "x", "title": "y"}}}, "不认识的字段"),
        ({"clauses": {"A-1": {"standard": "", "number": "1", "summary": "x"}}}, "standard 不能为空"),
        ({"clauses": {f"{FREE_PREFIX}X": {"standard": "S", "number": "1", "summary": "x"}}}, "不能以"),
        ({"other": {}}, "顶层"),
    ],
)
def test_a_broken_clause_config_names_the_entry(tmp_path, written, expected):
    """清单写坏要在加载期报,并指出是哪一条 —— 静默丢一条条款等于少聚合一批用例。"""
    path = tmp_path / "clauses.yaml"
    path.write_text(yaml.safe_dump(written, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ClauseConfigError) as error:
        load_clauses(path)
    assert expected in str(error.value), str(error.value)


def test_a_duplicate_clause_id_is_a_load_error(tmp_path):
    """重复 id 会被 pyyaml 静默覆盖成后一条:等于悄悄换掉一条条款的定义。"""
    path = tmp_path / "clauses.yaml"
    path.write_text(
        "clauses:\n"
        "  A-1: {standard: S, number: '1', summary: 第一条}\n"
        "  A-1: {standard: S, number: '1', summary: 第二条}\n",
        encoding="utf-8",
    )

    with pytest.raises(ClauseConfigError) as error:
        load_clauses(path)
    assert "重复" in str(error.value)


# --------------------------------------------------- 收集期:拼错就地停 ----

SUITE_CONFTEST = '''
pytest_plugins = ["testplatform.plugin"]
'''


def _collect(tmp_path: Path, clause_written: str):
    """在临时项目里真跑一轮收集:一个用例 + 一份条款清单 + 一份最小的设备清单。"""
    (tmp_path / "conftest.py").write_text(SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "clauses.yaml").write_text(CLAUSES.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(
        yaml.safe_dump(manifest(inverters=1), allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "test_probe.py").write_text(
        "from testplatform.cases import case\n\n\n"
        f'@case(id="M_00_00_01_000090_01", clause={clause_written!r})\n'
        "def test_probe():\n    pass\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)]),
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(
        pytest_command(
            "-q", "--clause-config", "clauses.yaml", "--device-config", "devices.yaml", "test_probe.py"
        ),
        cwd=tmp_path,
        capture_output=True,
        env=env,
        timeout=120,
    )
    return completed.returncode, (completed.stdout + completed.stderr).decode("utf-8", "replace")


def test_a_case_with_an_unregistered_clause_stops_collection(tmp_path):
    """拼错的条款在收集期就停,消息给出可用 id 与 free: 的写法 —— 拿到就能改。"""
    exit_code, output = _collect(tmp_path, "DL/T 645 4.5.2")

    assert exit_code != 0, f"拼错的条款不该被放行:\n{output}"
    assert "不在条款清单里" in output
    assert "DL/T 645 4.5.2" in output
    assert "DLT645-4.5.2" in output, f"没列可用 id,这条报错改不动:\n{output}"
    assert FREE_PREFIX in output, f"没给 free: 的写法,迁移期的人会卡住:\n{output}"


@pytest.mark.parametrize("clause_written", ["DLT645-4.5.2", f"{FREE_PREFIX} 旧写法原文"])
def test_a_registered_id_and_an_explicit_free_text_both_still_collect(tmp_path, clause_written):
    """两个合法写法都得真能跑:校验不能变成"怎么都红"。"""
    exit_code, output = _collect(tmp_path, clause_written)

    assert exit_code == 0, f"合法写法被判红:{output}"
    assert "1 passed" in output, output


# ------------------------------------------------ 迁移漂移守卫(扫源文件) ----

# 收集期校验只看得到被收集的节点;helper 里构造的 Case(以及清单数据里的 clause)绕过它。
# 所以再扫一遍源文件:任何 clause 字面量必须是条款 id 或显式的 free: —— 这一条堵的是
# "新建的 helper 顺手写一句文本" 这条后门,否则兼容会变成永久双轨。
CLAUSE_LITERALS = (
    re.compile(r'clause\s*=\s*"([^"\n]*)"'),
    re.compile(r"clause\s*=\s*'([^'\n]*)'"),
    re.compile(r'"clause"\s*:\s*"([^"\n]*)"'),
    re.compile(r"'clause'\s*:\s*'([^'\n]*)'"),
    re.compile(r'^(\s*clause:\s*")([^"\n]*)(")', re.MULTILINE),
    re.compile(r'^(DEFAULT_CLAUSE\s*=\s*")([^"\n]*)(")', re.MULTILINE),
)
SOURCE_ROOTS = (
    "tests",
    "tests_unit",
    "tests_platform",
    "tests_integration",
    "tests_scenario",
    "tests_support",
    "src",
    "tools",
    "configs",
)
# 政策:free: 只允许出现在平台内部用例(tests_platform/、tests_unit/);业务一致性与场景一律不许。
# 不写数量 —— 数量会腐烂,政策不会。新增 free: 必须改这个集合,那就是一次评审。
FREE_TEXT_ALLOWED = {
    ("tests_platform/test_support_via_chain.py", "free: 平台:装配"),
    ("tests_unit/test_scenario_cleanup.py", "free: X 1.1"),
    ("tests_unit/test_scenario_cleanup.py", "free: X 1.2"),
    ("tests_unit/test_real_transports.py", "free: 平台:传输"),
    ("tests_platform/test_task_loop.py", "free: 平台:任务闭环"),
    ("tests_platform/test_task_mbap.py", "free: 平台:MBAP 端到端"),
    ("tests_platform/test_round_probe_cases.py", "free: 平台:死因分类"),
    ("tests_platform/test_data_injection.py", "free: 平台:数据注入"),
    ("tests_scenario/test_injection_scenario.py", "free: 平台:数据注入"),
    ("tests_platform/test_task_report.py", "free: 平台:测试报告"),
    ("tests_platform/test_evidence_contract.py", "free: 平台:证据"),
    ("tests_platform/test_teardown.py", "free: 平台:清理"),
    ("tests_platform/test_sample_product_ships_with_the_package.py", "free: 平台:分发"),
}
FREE_TEXT_DIRS = ("tests_platform/", "tests_unit/", "tests_scenario/")
# 随包走的那份 configs/ 副本(源在仓库根):它是资源,不是用例,不参与条款字面量扫描。
PACKAGE_DATA_DIR = "_data"


def _clause_literals_in(path: Path):
    text = path.read_text(encoding="utf-8")
    for pattern in CLAUSE_LITERALS:
        for match in pattern.finditer(text):
            value = match.group(2) if match.lastindex == 3 else match.group(1)
            line = text[: match.start()].count("\n") + 1
            yield line, value.strip()


def test_no_bare_free_text_clause_in_sources():
    """源文件里的 clause 字面量只有两种合法写法:条款 id、free: 原文。裸文本一律红。"""
    registry = load_clauses(CLAUSES)
    offenders = []
    free_texts = set()
    for root in SOURCE_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if PACKAGE_DATA_DIR in path.parts:
                continue  # 包内 _data/ 是 configs/ 的随包副本(源在仓库根),不是用例
            if path.suffix not in (".py", ".yaml", ".yml"):
                continue
            if path == CLAUSES or path == Path(__file__).resolve():
                continue  # 清单本身只放 id;本文件里的探针字面量是刻意的反例
            for line, value in _clause_literals_in(path):
                if not value or registry.has(value):
                    continue
                # 模板里的格式化占位符(`clause="{clause_id}"`)不是条款声明:它生成的是变量位置,
                # 不是某一轮证据里的条款。真正的检查在收集期(未注册 id 会当场报错)。
                if "{" in value and "}" in value:
                    continue
                if registry.is_free(value):
                    free_texts.add((str(path.relative_to(REPO)).replace(os.sep, "/"), value))
                    continue
                offenders.append(f"{path.relative_to(REPO)}:{line} clause={value!r}")

    assert not offenders, (
        "这些 clause 既不是条款 id 也不是 free: 原文(改成 configs/clauses.yaml 里的 id,"
        "或在确实没有受控条款时写 free:):\n" + "\n".join(offenders)
    )
    bad_dirs = [
        f"{path}: {value!r}"
        for path, value in free_texts
        if not path.startswith(FREE_TEXT_DIRS)
    ]
    assert not bad_dirs, (
        "free: 只允许出现在平台内部用例(tests_platform/、tests_unit/、tests_scenario/);"
        "业务一致性(tests/)与场景(tests_scenario/)一律不许:\n" + "\n".join(sorted(bad_dirs))
    )
    assert free_texts == FREE_TEXT_ALLOWED, (
        f"free: 的位置变了。政策是「只允许平台内部用例」,新增一处就是一次评审:\n"
        f"  多出来的: {sorted(free_texts - FREE_TEXT_ALLOWED)}\n"
        f"  少掉的: {sorted(FREE_TEXT_ALLOWED - free_texts)}"
    )


def test_a_clause_can_declare_itself_not_applicable(tmp_path):
    """验不了的条款要在清单里明说(带理由),而不是留一条零覆盖让人猜。

    字段可选:大多数条款没有这一项;写了就原样带出(空字符串 = 没写)。
    """
    path = tmp_path / "clauses.yaml"
    path.write_text(
        "clauses:\n"
        "  A-1: {standard: S, number: '1', summary: 正常条款}\n"
        "  A-2:\n"
        "    standard: S\n"
        "    number: '2'\n"
        "    summary: 验不了\n"
        "    not_applicable: 没有条款原文:TODO(来源)\n",
        encoding="utf-8",
    )

    registry = load_clauses(path)

    assert registry.get("A-1").not_applicable == ""
    assert registry.get("A-2").not_applicable == "没有条款原文:TODO(来源)"


def test_an_unknown_clause_field_is_still_rejected(tmp_path):
    """加了 not_applicable 之后,清单仍然只认那四个字段 —— 拼错的字段不能被静默忽略。"""
    path = tmp_path / "clauses.yaml"
    path.write_text(
        "clauses:\n"
        "  A-1: {standard: S, number: '1', summary: x, not_applicabel: 打错了}\n",
        encoding="utf-8",
    )

    with pytest.raises(ClauseConfigError) as error:
        load_clauses(path)
    assert "not_applicabel" in str(error.value)
