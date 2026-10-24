"""
文档校验(票 17c):README 与 `docs/**/*.md` 里指向仓库内的路径必须真的存在。

为什么值得一条守卫:文档里的路径是最容易腐烂的一类引用 —— 文件改名、用例挪位置、
`tools/` 里删掉一个脚本,正文全都还写着。人不会每天点一遍,CI 会。

只校验**仓库内**路径(相对链接、`docs/...`、`tools/*.py`、`python -m pytest <路径>`);
外部 URL 一律不联网校验 —— 那既慢又不稳,而且外部站点失效不该拦住我们提交。

"还没实现的命令"(09b 的脚手架)必须在同一段里带 `TODO(09b)` 标记:
    * 带标记的段落跳过路径校验
    * **没标记的**未来命令 = 失败(文档不许写一个不存在的命令,却装作它能跑)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

SCHEMES = ("http://", "https://", "mailto:", "ftp://", "javascript:")
SKIP_MARKER = "TODO(09b)"

LINK = "link"
DOC_PATH = "docs-path"
TOOL_PATH = "tool-path"
PYTEST_PATH = "pytest-path"
CONFIG_PATH = "config-path"
TASK_PATH = "task-path"


@dataclass(frozen=True)
class Finding:
    """一条"指向不存在的东西"的引用。`block` 用来判断它所在段落有没有 TODO(09b) 标记。"""

    where: str  # 文件:行号
    kind: str
    target: str
    block: str = ""

    def as_line(self) -> str:
        return f"{self.where}: {_KINDS[self.kind]} {self.target!r} 不存在"


_KINDS = {
    LINK: "链接目标",
    DOC_PATH: "docs 路径",
    TOOL_PATH: "工具脚本",
    PYTEST_PATH: "pytest 路径",
    CONFIG_PATH: "配置清单",
    TASK_PATH: "任务文件",
}

# `tasks/<id>.yaml` 这种带占位符/示例性质的路径:文件不存在是正常的,但必须看得出来是模板 ——
# 要么路径里带 `<...>` 占位符,要么同一段写着"示例/模板",要么它就在围栏代码块里。
TEMPLATE_WORDS = ("示例", "模板", "样例", "template")


def documents(root: str | Path) -> dict[str, str]:
    """要校验的文档:README + docs 下所有 markdown(路径用 POSIX,跨平台一致)。"""
    repo = Path(root)
    found: dict[str, str] = {}
    readme = repo / "README.md"
    if readme.is_file():
        found["README.md"] = readme.read_text(encoding="utf-8")
    for path in sorted((repo / "docs").rglob("*.md")):
        found[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
    return found


def violations(docs: Mapping[str, str], root: str | Path) -> list[str]:
    """所有"指向仓库内不存在的路径"的地方。空 = 文档与仓库一致。"""
    repo = Path(root)
    problems: list[str] = []
    for name, text in sorted(docs.items()):
        for finding in findings(name, text, repo):
            marked = SKIP_MARKER in finding.block
            if not marked:
                problems.append(finding.as_line())
        problems.extend(_marked_blocks_without_commands(name, text, repo))
    return problems


def findings(name: str, text: str, root: Path) -> list[Finding]:
    """这份文档里所有"仓库内路径"的引用,逐条判断它存不存在。"""
    lines = text.splitlines()
    found: list[Finding] = []
    for index, line in enumerate(lines, start=1):
        block = _block_of(lines, index - 1)
        for target in _links(line):
            if not _resolvable(target, name, root):
                found.append(Finding(f"{name}:{index}", LINK, target, block))
        for token in _code_spans(line):
            if _looks_like_docs_path(token) and not _exists(token, name, root):
                found.append(Finding(f"{name}:{index}", DOC_PATH, token, block))
            elif _looks_like_tool_path(token) and not _exists(token, name, root):
                found.append(Finding(f"{name}:{index}", TOOL_PATH, token, block))
        in_fence = _in_fence(lines, index - 1)
        # 围栏里的命令是裸写的(没有反引号):两种都收,否则 ``` 块里的配置/任务路径永远查不到
        tokens = list(_code_spans(line)) + (line.split() if in_fence else [])
        for token in tokens:
            token = token.strip("`\"',.;:")
            if _looks_like_config_path(token) and not _exists(token, name, root):
                found.append(Finding(f"{name}:{index}", CONFIG_PATH, token, block))
            elif _looks_like_task_path(token) and not _exists(token, name, root):
                if "<" in token or in_fence or _says_template(block):
                    continue  # 模板形态:不存在是正常的
                found.append(Finding(f"{name}:{index}", TASK_PATH, token, block))
        for token in _pytest_paths(line):
            if not _exists(token, name, root):
                found.append(Finding(f"{name}:{index}", PYTEST_PATH, token, block))
        # 围栏代码块里直接写的命令(没有反引号)
        if _in_fence(lines, index - 1) and "pytest" in line:
            for token in _pytest_paths_from_words(line):
                if not _exists(token, name, root):
                    found.append(Finding(f"{name}:{index}", PYTEST_PATH, token, block))
    return found


# ---------------------------------------------------------------- 解析 ----

def _links(line: str) -> Iterator[str]:
    """markdown 链接的目标(`[文字](目标)`,顺带认 `<目标>` 与 `目标 "标题"`)。"""
    index = 0
    while True:
        start = line.find("](", index)
        if start < 0:
            return
        end = line.find(")", start)
        if end < 0:
            return
        target = line[start + 2 : end].split(" ")[0].strip().strip("<>").strip()
        index = end
        if target and not _is_external(target) and not target.startswith("#"):
            yield target.split("#")[0]


def _code_spans(line: str) -> Iterator[str]:
    """反引号里的东西(行内代码)。文档里的路径几乎都写在反引号里。"""
    parts = line.split("`")
    return iter(parts[1::2])


def _pytest_paths(line: str) -> Iterator[str]:
    """`python -m pytest <路径>` 里给到具体文件/目录的那些参数。"""
    for span in _code_spans(line):
        if "pytest" not in span:
            continue
        words = span.split()
        if "pytest" not in words:
            continue
        for word in words[words.index("pytest") + 1 :]:
            if word.startswith("-") or word.startswith("#"):
                break
            cleaned = _strip_path(word)
            if "/" in cleaned or cleaned.endswith(".py"):
                yield cleaned


def _in_fence(lines: list[str], index: int) -> bool:
    return _fence_bounds(lines, index) is not None


def _pytest_paths_from_words(line: str) -> Iterator[str]:
    """一行裸命令(围栏里)里的 pytest 路径参数。"""
    words = line.strip().split()
    if "pytest" not in words:
        return iter(())
    found = []
    for word in words[words.index("pytest") + 1 :]:
        if word.startswith("-") or word.startswith("#"):
            break
        cleaned = _strip_path(word)
        if "/" in cleaned or cleaned.endswith(".py"):
            found.append(cleaned)
    return iter(found)


def _strip_path(word: str) -> str:
    """路径 token 后面常跟括号说明(如 `plugin.py(entry point)`):括号之后不算路径。"""
    for separator in ("(", "（", ")", "）"):
        word = word.split(separator)[0]
    return word.split("::")[0].strip("\"'`").rstrip(",.;:")


def _looks_like_docs_path(token: str) -> bool:
    first = token.strip().split()[0] if token.strip() else ""
    return first.startswith("docs/") and "*" not in first


def _looks_like_config_path(token: str) -> bool:
    first = token.strip().split()[0] if token.strip() else ""
    return first.startswith("configs/") and first.endswith(".yaml")


def _looks_like_task_path(token: str) -> bool:
    first = token.strip().split()[0] if token.strip() else ""
    return first.startswith("tasks/") and first.endswith(".yaml")


def _says_template(block: str) -> bool:
    return any(word in block for word in TEMPLATE_WORDS)


def _looks_like_tool_path(token: str) -> bool:
    first = token.strip().split()[0] if token.strip() else ""
    return first.startswith("tools/") and first.endswith(".py")


def _is_external(target: str) -> bool:
    return target.startswith(SCHEMES)


def _exists(target: str, name: str, root: Path) -> bool:
    """仓库内路径:先按仓库根解析,再按文档所在目录解析 —— 两种写法文档里都有。

    两种放宽(都是文档里真实存在的写法,不是偷懒):
      * 通配/省略号写法(`docs/**`、`docs/adr/0001`)按**前缀**去匹配已有文件
      * `tools/x.py --参数`:只校验脚本本身存在,参数不看
    """
    cleaned = target.strip().strip("`\"'")
    if not cleaned or "*" in cleaned:
        return True
    head = cleaned.split()[0].split("::")[0].rstrip(",.;:")
    for base in (root, (root / name).parent):
        if _is_file_or_dir(base / head):
            return True
        if Path(head).suffix == "":  # `docs/adr/0001` 这种省略后缀的指称
            if any(_is_file_or_dir(path) for path in (base / "docs").glob(f"**/{head.split('/')[-1]}*")):
                return True
    return False


def _resolvable(target: str, name: str, root: Path) -> bool:
    return _exists(target, name, root)


def _is_file_or_dir(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # pragma: no cover - 路径里带非法字符时当作不存在
        return False


# ------------------------------------------------------------ 段落标记 ----

def _block_of(lines: list[str], index: int) -> str:
    """这一行所在的“段”：上一块 + 本块 + 下一块。

    “上/下一块”是有意的：`TODO(09b):脚手架落地后换成 npx` 通常写在代码块**上一行**，
    中间隔着空行；只看同一段会把标记和它要豁免的命令拆开。
    """
    fence = _fence_bounds(lines, index)
    if fence is not None:
        start, end = fence
        return "\n".join(lines[start : end + 1])
    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    parts = [
        _previous_block(lines, start),
        "\n".join(lines[start : end + 1]),
        _next_block(lines, end + 1),
    ]
    return "\n".join(part for part in parts if part)


def _previous_block(lines: list[str], before: int) -> str:
    """`before` 之前的最后一块（跳过空行）；在围栏里就把整段围栏带上。"""
    position = before - 1
    while position >= 0 and not lines[position].strip():
        position -= 1
    if position < 0:
        return ""
    end = position
    while position > 0 and lines[position - 1].strip():
        position -= 1
    fence = _fence_bounds(lines, end)
    if fence is not None and fence[0] < position:
        return "\n".join(lines[fence[0] : fence[1] + 1])
    return "\n".join(lines[position : end + 1])


def _next_block(lines: list[str], after: int) -> str:
    """`after` 之后的第一块（跳过空行；遇到围栏就整块带上）。"""
    position = after
    while position < len(lines) and not lines[position].strip():
        position += 1
    if position >= len(lines):
        return ""
    fence = _fence_bounds(lines, position)
    if fence is not None:
        start, end = fence
        return "\n".join(lines[start : end + 1])
    end = position
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[position : end + 1])


def _fence_bounds(lines: list[str], index: int):
    """这一行是否落在 ``` 围栏里;在的话返回围栏的起止下标。"""
    open_at = None
    for position in range(index + 1):
        if lines[position].lstrip().startswith("```"):
            open_at = position if open_at is None else None
    if open_at is None:
        return None
    for position in range(open_at + 1, len(lines)):
        if lines[position].lstrip().startswith("```"):
            return open_at, position
    return open_at, len(lines) - 1


def _marked_blocks_without_commands(name: str, text: str, root: Path) -> list[str]:
    """标了 TODO(09b) 的段落必须真的含一条命令/路径 —— 空标记只是噪音。"""
    problems: list[str] = []
    lines = text.splitlines()
    seen: set[int] = set()
    for index, line in enumerate(lines):
        if SKIP_MARKER not in line:
            continue
        block = _block_of(lines, index)
        if id(block) in seen or not block.strip():
            continue
        seen.add(id(block))
        has_command = any(span.strip() for span in _code_spans(block))
        if not has_command:
            problems.append(
                f"{name}:{index + 1}: 标了 {SKIP_MARKER} 但这一段里没有反引号命令/路径"
                f"(空标记只是噪音)"
            )
    return problems


def _root_of(docs: Mapping[str, str], fallback: Path) -> Path:
    """给测试用:文档集合对应的根(默认就是仓库根)。"""
    return fallback


# ------------------------------------------------------------- 真实文档 ----

REPO = Path(__file__).resolve().parents[1]


def test_every_in_repo_path_in_the_docs_resolves():
    """README 与 docs 里指向仓库内的路径必须真的存在 —— 文件改名/挪位置时这条会红。"""
    problems = violations(documents(REPO), REPO)

    assert problems == [], "文档里有指向不存在的路径:\n" + "\n".join(problems)


def test_external_urls_are_not_fetched():
    """外部链接不联网校验:站点失效不该拦住我们提交,也不该让测试依赖网络。"""
    docs = {"README.md": "见 [上游](https://example.invalid/nothing-here) 与 <https://example.invalid/x>"}

    assert violations(docs, REPO) == []


# --------------------------------------------------------- 每一类引用 ----

def test_a_broken_relative_link_is_reported():
    docs = {"docs/how-to/README.md": "| 看这个 | [`nope.md`](nope.md) |"}

    problems = violations(docs, REPO)

    assert any("nope.md" in problem and "链接目标" in problem for problem in problems), problems


def test_a_broken_docs_path_is_reported():
    docs = {"README.md": "细节见 `docs/how-to/nope.md`。"}

    problems = violations(docs, REPO)

    assert any("docs/how-to/nope.md" in problem for problem in problems), problems


def test_a_missing_tool_script_is_reported():
    docs = {"docs/status.md": "跑 `tools/no_such_tool.py` 即可。"}

    problems = violations(docs, REPO)

    assert any("tools/no_such_tool.py" in problem and "工具脚本" in problem for problem in problems), problems


def test_a_missing_pytest_path_is_reported():
    docs = {"docs/WRITING_TESTS.md": "```\npython -m pytest tests/no_such_test.py\n```"}

    problems = violations(docs, REPO)

    assert any("tests/no_such_test.py" in problem for problem in problems), problems


def test_an_existing_path_is_accepted_from_the_repo_root_and_from_the_doc_dir():
    """两种写法文档里都有:按仓库根写(`configs/...`),或按同目录相对写(链接)。"""
    docs = {
        "README.md": "`configs/devices.yaml` 与 `docs/how-to/add-a-device.md`",
        "docs/how-to/README.md": "| [加一台设备](add-a-device.md) |",
    }

    assert violations(docs, REPO) == []


# ------------------------------------------------- 未来命令必须带标记 ----

def test_a_future_command_without_the_marker_is_a_failure():
    """文档不许写一个还不存在的命令却装作它能跑 —— 要么实现它,要么标 TODO(09b)。"""
    docs = {"README.md": "未来会这样跑:\n\n`npx edgec-test run task.yaml` -> `tools/future_runner.py`"}

    problems = violations(docs, REPO)

    assert any("tools/future_runner.py" in problem for problem in problems), problems


def test_a_future_command_with_the_marker_is_excused():
    docs = {
        "README.md": "未来会这样跑(TODO(09b):脚手架落地后换成 npx):\n\n"
        "`npx edgec-test run task.yaml` -> `tools/future_runner.py`"
    }

    assert violations(docs, REPO) == []


def test_a_marker_without_any_command_is_noise():
    """标了 TODO(09b) 但这一段里没有命令/路径:标记本身没意义,报出来。"""
    docs = {"README.md": "TODO(09b):这一段先留个位置,等脚手架。"}

    problems = violations(docs, REPO)

    assert any("没有反引号命令/路径" in problem for problem in problems), problems


def test_an_empty_catalog_of_documents_is_vacuously_clean():
    assert violations({}, REPO) == []


# --------------------------------------------- 配置清单与任务文件(票 17d) ----

def test_a_missing_config_reference_is_reported():
    docs = {"docs/status.md": "清单在 `configs/no_such_devices.yaml`。"}

    problems = violations(docs, REPO)

    assert any("configs/no_such_devices.yaml" in problem and "配置清单" in problem for problem in problems), problems


def test_a_concrete_task_file_in_prose_is_reported():
    """正文里点名一个具体任务文件,而它不存在 —— 那就是"写了一个不存在的任务"没人管。"""
    docs = {"docs/how-to/x.md": "直接跑 `tasks/my-first.yaml` 就行。"}

    problems = violations(docs, REPO)

    assert any("tasks/my-first.yaml" in problem and "任务文件" in problem for problem in problems), problems


def test_a_task_file_inside_a_fence_is_treated_as_template():
    """围栏代码块里的任务是模板/示例写法(读者会自己建一个),不算"不存在的文件"。"""
    docs = {
        "docs/QUICKSTART.md": "```\npython tools/run_task.py tasks/my-first.yaml --root . --evidence-dir reports/my-first\n```"
    }

    assert violations(docs, REPO) == []


def test_a_task_template_with_a_placeholder_or_the_word_is_accepted():
    docs = {
        "README.md": "写一份 `tasks/<id>.yaml`。",
        "docs/x.md": "示例:`tasks/hvac-demo.yaml`(可以照抄)。",
        "docs/y.md": "模板任务名 `tasks/whatever.yaml`。",
    }

    assert violations(docs, REPO) == []
