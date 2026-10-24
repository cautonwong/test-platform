"""
孤儿用例文件守卫(票 17):带 `@case` 却**既不被收集、又没人按路径点名**的文件 = 陷阱。

为什么这是个真问题:`tests_scenario/bench_restore_cases.py` 的用例必须真失败,所以文件名故意
不是 `test_*`(不进默认收集)—— 08 的目录扫描因此以为它是孤儿,还发现它占着编号
`S_00_00_04_000001_01`。但**它其实在跑**:`tests/test_bench_restore.py` 用嵌套 pytest 按绝对路径
点名那两个函数。

所以这条守卫不是"删掉孤儿",而是把判断标准写死:
    * 文件名符合 pytest 收集模式(`test_*.py` / `*_test.py`)→ 会被收集,不管它
    * 否则(故意不进默认套件的用例文件)→ **必须有别的文件按文件名点名它**,否则它就是没人跑的死代码

目录(tools/catalog.py)看不到这种引用 —— 它扫的是装饰器,不是嵌套 pytest 的命令行。
这一层是补那个盲点的:新增一个"不进收集"的用例文件而忘了接进去,这里会红。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("tests", "tests_platform", "tests_scenario", "tests_unit", "tests_support")
COLLECTED_PATTERNS = ("test_*.py", "*_test.py")

# 目前唯一的"故意不进收集、靠嵌套 pytest 点名跑"的文件;新增同类必须能说清谁在跑它
NAMED_BY_PATH = {
    "tests_scenario/bench_restore_cases.py": "tests/test_bench_restore.py",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in ROOTS:
        files.extend(sorted((REPO / root).rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _case_names(path: Path) -> list[str]:
    """这个文件里带 `@case(...)` 的 `test_*` 函数名(与目录同一个判据:只认 pytest 会收的)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "case":
                names.append(node.name)
                break
    return names


def _is_collected_by_pytest(path: Path) -> bool:
    return any(path.match(pattern) for pattern in COLLECTED_PATTERNS)


def _referenced_by_name(path: Path, others: list[Path]) -> list[str]:
    """哪些文件按文件名点名了它(嵌套 pytest 的写法就是拼一个路径字符串)。"""
    return [
        str(other.relative_to(REPO)).replace("\\", "/")
        for other in others
        if other != path and path.name in other.read_text(encoding="utf-8", errors="ignore")
    ]


def _orphans() -> dict[str, list[str]]:
    files = _python_files()
    orphans: dict[str, list[str]] = {}
    for path in files:
        if _is_collected_by_pytest(path) or not _case_names(path):
            continue
        if _referenced_by_name(path, files):
            continue
        orphans[str(path.relative_to(REPO)).replace("\\", "/")] = _case_names(path)
    return orphans


def test_no_case_file_is_left_without_a_runner():
    """带 @case 却没人跑的文件:要么改名进收集,要么让某处按路径点名它。"""
    orphans = _orphans()

    assert orphans == {}, (
        "这些文件带 @case,但既不进默认收集、也没有任何文件按路径点名它(等于没人跑):\n"
        + "\n".join(f"  {path}: {sorted(names)}" for path, names in sorted(orphans.items()))
        + "\n处置:改名成 test_*.py 让它被收集,或在某个用例里用嵌套 pytest 按路径点名它。"
    )


def test_the_exception_path_cases_are_named_by_a_real_test():
    """票 17 的那一个:它不是孤儿 —— tests/test_bench_restore.py 真的按路径跑它。

    这条同时钉住"谁在跑它":删掉那个嵌套 pytest 调用,这里立刻红(那时它才真的成了死代码)。
    """
    named = REPO / "tests_scenario" / "bench_restore_cases.py"
    runner = REPO / "tests" / "test_bench_restore.py"

    names = _case_names(named)
    assert names, "这个文件里连 @case 都没有了?那票 17 的前提就不成立"

    text = runner.read_text(encoding="utf-8")
    assert "bench_restore_cases.py" in text, "没人按路径点名它了:它现在真的没人跑"
    for name in names:
        assert name in text, f"{name} 没有被任何嵌套 pytest 点名"
