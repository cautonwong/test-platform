"""
证据契约:代码**实际发出**的字段,与 `docs/how-to/read-evidence.md` **说**的字段,必须对得上。

证据是"跑完之后判定是谁的问题"的唯一凭据。字段只在代码里 = 用户读不到;
只在文档里 = 文档撒谎。两种都让结论不可复现,所以两侧都要对着源码断言。

为什么用 AST 而不是正则:`kind` 散在 48 个 dict 字面量里,正则漏一处就等于没验,
而我们分不清"真的没有"和"正则没匹配上"。AST 认的是语法结构,漏不了。

三张小表都是**冻结的**,改动必须有人跟着改这里:
  * `EMISSIONS` —— 源码侧发射点表(文件::函数 / kind / 字段)。总数一变就红:
    新增记录、加减字段不可能静默通过。
  * `DOC_CLAIMS` / `DOC_FILES` / `DOC_EVERY_RECORD` —— 文档那张表的机读形态,
    文档改一个字没同步这里也要红。
  * `DOCUMENTED` / `TO_DOC` / `INTERNAL` —— 每个发射点的交代。差异账里的每一条都写清为什么;
    白名单不是免死金牌,加一条就是一次评审。`TO_DOC` 是**待补文档**的红榜,不是豁免。

**范围**:`src/testplatform/{execution,reporting}/` 里带 `kind` 的 dict 字面量
(证据记录按 `kind` 区分,这是文档自己定的口径),外加 `config/paths.py` 的 layout 与
`reporting/evidence.py::record` 这层包装。`catalog/` 的 `kind` 是清单结构缺口(`where` / `detail`),
不是证据记录;`_data/` 是示例产品,都不在这份契约里(见 `test_kind_dicts_outside_the_surface_are_not_evidence`)。

**不在本文件范围**:嵌套载荷(`reading.criteria` / `evaluation.breaches`)只对账到父记录的键;
`Session.record()` 统一注入的 `at` / `device` 由 `«每条记录外层»` 那一条兜住。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from testplatform.cases import case
from testplatform.config.paths import PACKAGE_RESOURCES

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "src/testplatform"
DOC = REPO / "docs/how-to/read-evidence.md"

# 证据/报告的两个有界上下文(见 docs/agents/collaboration.md §7)。
SURFACE = ("execution", "reporting", "config")
# 证据面之外也有带 kind 的 dict,但那不是证据记录。每条写清它是什么。
NOT_EVIDENCE = {
    "catalog": "清单结构缺口(kind / where / detail):说的是清单自己缺什么,不是一轮测试的证据",
    "_data": "示例产品(package 内那份):产品侧自报的报文记录,不是平台的证据层",
}
# 文档那张表说的是 `<设备id>.jsonl` 的记录,它由 evidence.py 发 —— 所以字段逐一对账只在
# 这个文件里做。别处的同名 kind(如 attribution 的 link_lost)是另一份文件里的投影。
JSONL_FILE = "reporting/evidence.py"
DOC_HEADING = "## 证据目录长什么样"

# `kind` 不是字面量的记录、以及根本不带 kind 的记录:标签是这张表起的名字,不是源码里的值。
NAMED = {
    ("config/paths.py", "sources"): "«layout»",
    ("execution/death.py", "attempt_exit"): "«abandoned_product»",
    ("execution/task.py", "_environment"): "«env»",
    ("reporting/attribution.py", "as_record"): "«attribution.json»",
    ("reporting/evidence.py", "record"): "«每条记录外层»",
    ("reporting/scenario.py", "_record_of"): "«scenario-*.json»",
}
# `kind` 的值取自变量:清单不主张它会是什么。
ECHO = "«kind 非字面量»"


@dataclass(frozen=True)
class Point:
    """一个发射点:哪个函数、发的是哪种记录、这条记录上有哪些键。"""

    where: str  # 文件::函数
    kind: str
    fields: str  # 源码里的键,空格分隔(顺序不参与比较)
    # 有闭集的字段:字段 → 值域。每个值都得能在**本文件**的字符串字面量里找到(文档串不算)。
    closed: dict = field(default_factory=dict)

    def keys(self) -> frozenset:
        return frozenset(self.fields.split())


EMISSIONS: tuple[Point, ...] = (
    Point("config/paths.py::sources", "«layout»", "cases"),
    Point(
        "execution/death.py::exit_event",
        "product_exit",
        "kind how code signal at",
        {"how": ("self_exit", "platform_killed")},
    ),
    Point(
        "execution/death.py::attempt_exit",
        "«abandoned_product»",
        "how code signal",
        {"how": ("platform_killed", "unverified")},
    ),
    Point("execution/death.py::unexecuted_event", "unexecuted", "kind count reason"),
    Point(
        "execution/task.py::run_task",
        ECHO,
        "task kind product devices select product_port port_source port_range port_attempts"
        " slave_ports slave_addresses slaves launch readiness layout pytest_exit_code"
        " product_exit_code error started_at runtime env process_events",
    ),
    Point(
        "execution/task.py::run_task",
        "port_retry",
        "kind attempt reason port at",
        {"reason": ("端口他人不可用", "产品 stderr 自报 bind 失败")},
    ),
    Point("execution/task.py::_restart_product", "product_restart", "kind at reason"),
    Point(
        "execution/task.py::_environment",
        "«env»",
        "commit platform_version product_sha256 simulator_version"
        " platform_source platform_sha256",
    ),
    Point(
        "reporting/attribution.py::as_record",
        "«attribution.json»",
        "verdict note scope ran round_status device_failures invalid_tests platform_errors"
        " evidence_corrupt unrecognized unevidenced_failures no_conclusion"
        " out_of_scope_devices not_run_devices device_scope",
    ),
    Point(
        "reporting/attribution.py::attribute",
        "no_readings",
        "kind device case_nodeids executed_nodeids ref reason",
    ),
    Point("reporting/attribution.py::_read_device_file", "no_verdict", "kind device case ref reason"),
    Point(
        "reporting/attribution.py::_read_device_file",
        "link_lost",
        "kind device operation failure message at ref",
    ),
    Point(
        "reporting/attribution.py::_read_device_file",
        "manual_step",
        "kind device case action expect confirmed executed_steps ref",
    ),
    Point(
        "reporting/attribution.py::_read_device_file",
        "evidence_gap",
        "kind device missing window reason ref",
    ),
    Point(
        "reporting/attribution.py::_read_device_file",
        "step_failed",
        "kind device case quantity value expected text ref",
    ),
    Point("reporting/attribution.py::_read_device_file", ECHO, "kind device ref"),
    Point(
        "reporting/attribution.py::_criterion_failure",
        "equals",
        "kind device case quantity value expected text ref",
    ),
    Point(
        "reporting/attribution.py::_criterion_failure",
        "criterion",
        "kind device case quantity value limit exceeded_by text ref",
    ),
    Point(
        "reporting/attribution.py::_read_scenarios",
        "scenario_invalid",
        "kind scenario case failed_steps ref",
    ),
    Point(
        "reporting/attribution.py::_read_scenarios",
        "scenario_step",
        "kind device action detail case ref",
    ),
    Point("reporting/attribution.py::_read_round_status", "no_round_status", "kind ref reason"),
    Point("reporting/attribution.py::_read_scope", "scope_missing", "kind ref reason"),
    Point("reporting/attribution.py::_read_scope", "deselected", "kind case nodeid reason ref"),
    Point(
        "reporting/attribution.py::_read_scope",
        "skipped_case",
        "kind case nodeid operation message ref",
    ),
    Point("reporting/attribution.py::_read_scope", "skipped", "kind case nodeid reason cause ref"),
    Point("reporting/attribution.py::_read_scope", "not_run", "kind count items reason ref"),
    Point("reporting/attribution.py::_unevidenced_failures", ECHO, "kind nodeid case reason ref"),
    Point("reporting/evidence.py::record", "«每条记录外层»", "at device **"),
    Point(
        "reporting/evidence.py::record_injection",
        "injection",
        "kind device key value hex mode at",
    ),
    Point(
        "reporting/report.py::_event_emit_gaps",
        "event_emit_failed",
        "kind device missing reason ref",
    ),
    Point(
        "reporting/evidence.py::record_link_lost",
        "link_lost",
        "kind operation failure message",
    ),
    Point(
        "reporting/evidence.py::record_manual_step",
        "manual_step",
        "kind case action expect confirmed executed_steps",
    ),
    Point(
        "reporting/evidence.py::record_bench_setup",
        "bench_setup",
        "kind case bench setpoints applied",
    ),
    Point(
        "reporting/evidence.py::record_step_failure",
        "step_failed",
        "kind case key value expected text",
    ),
    Point(
        "reporting/evidence.py::record_platform_error",
        "platform_error",
        "at kind device case nodeid error message",
    ),
    Point("reporting/evidence.py::record_round", "evidence_gap", "kind missing window note"),
    Point("reporting/evidence.py::record_round", "event", "kind case events"),
    Point(
        "reporting/evidence.py::record_round",
        "reading",
        "kind mode case criteria readings evaluation events",
    ),
    Point("reporting/evidence.py::report_degraded_block", "batch_fallback", "kind **"),
    Point("reporting/evidence.py::report_write", "write", "kind **"),
    Point("reporting/evidence.py::send", "frame", "kind direction hex"),
    Point("reporting/evidence.py::receive", "frame", "kind direction hex"),
    Point(
        "reporting/evidence.py::_record_failure",
        "frame",
        "kind direction error **",
        {"error": ("timeout", "closed", "disconnect")},
    ),
    Point(
        "reporting/evidence.py::_breach_of",
        "equals",
        "kind quantity value expected severity text",
    ),
    Point(
        "reporting/report.py::as_record",
        ECHO,
        "task kind product select verdict pytest_exit_code note reason columns denominator"
        " untested failures scenarios invalid platform_errors evidence_gaps"
        " devices_without_readings out_of_scope_devices product_exit task_error runtime env",
        {"verdict": ("failed", "invalid", "incomplete", "clean")},
    ),
    Point("reporting/report.py::build", "unexecuted", "kind count reason ref"),
    Point("reporting/report.py::_untested", "not_run", "kind case nodeid reason ref"),
    Point(
        "reporting/report.py::_untested",
        "failed_without_evidence",
        "kind case nodeid reason ref",
    ),
    Point(
        "reporting/scenario.py::_record_of",
        "«scenario-*.json»",
        "scenario case verdict note participants reading_keys steps cleanup cleanup_failed"
        " evidence",
    ),
)

# 发射点总数。加减一个发射点必须同时改这张表 —— 这就是"新字段不许静默通过"那道闸。
EMISSION_COUNT = 49
# task.json 的 layout:五样包内资源 + 用例(用例只可能来自项目目录)。
LAYOUT_SOURCES = ("pytest_ini", "slave_script", "registers", "clauses", "sample_product",
                  "cases")


# ---------------------------------------------------------------- 文档侧(冻结)

# `docs/how-to/read-evidence.md` 那张 kind 表的机读形态:kind → (字段, {字段: 值域})。
DOC_CLAIMS = {
    "frame": (("error",), {"error": ("timeout", "closed", "bad_crc")}),
    "reading": (("mode",), {"mode": ("real", "simulator")}),
    "event": ((), {}),
    "link_lost": ((), {}),
    "evidence_gap": (("missing",), {}),
}
DOC_FILES = (
    "<设备id>.jsonl",
    "run-scope.json",
    "round-status.json",
    "scenario-<用例名>.json",
    "attribution.json",
)
DOC_EVERY_RECORD = ("at",)

# 文档 `task.json` 一节(128715b)的字段表:第一列点名的字段。改文档就要改这里。
TASK_DOC_FIELDS = (
    "kind",
    "launch",
    "slave_ports",
    "slave_addresses",
    "port_source",
    "port_range",
    "port_attempts",
    "env",
    "layout",
    "process_events",
    "pytest_exit_code",
)
# 文档 `process_events` 那段 json 示例:kind → 发这条记录的发射点。
PROCESS_EVENT_KINDS = {
    "product_exit": ("execution/death.py::exit_event", "product_exit"),
    "port_retry": ("execution/task.py::run_task", "port_retry"),
    "product_restart": ("execution/task.py::_restart_product", "product_restart"),
    "unexecuted": ("execution/death.py::unexecuted_event", "unexecuted"),
}


# ---------------------------------------------------------------- 每个发射点的交代

DOCUMENTED = {
    # 文档的 kind 表 + 「每条记录都带 at」覆盖的 jsonl 记录
    (f"{JSONL_FILE}::record", "«每条记录外层»"),
    (f"{JSONL_FILE}::record_link_lost", "link_lost"),
    (f"{JSONL_FILE}::record_round", "reading"),
    (f"{JSONL_FILE}::record_round", "event"),
    (f"{JSONL_FILE}::record_round", "evidence_gap"),
    (f"{JSONL_FILE}::send", "frame"),
    (f"{JSONL_FILE}::receive", "frame"),
    (f"{JSONL_FILE}::_record_failure", "frame"),
    # reading 的「求值结论」
    (f"{JSONL_FILE}::_breach_of", "equals"),
    # 文档列了 attribution.json 与它那四类 + 没结论一档,条目就是那几类的载荷
    ("reporting/attribution.py::as_record", "«attribution.json»"),
    ("reporting/attribution.py::attribute", "no_readings"),
    ("reporting/attribution.py::_read_device_file", "no_verdict"),
    ("reporting/attribution.py::_read_device_file", "link_lost"),
    ("reporting/attribution.py::_read_device_file", "manual_step"),
    ("reporting/attribution.py::_read_device_file", "evidence_gap"),
    ("reporting/attribution.py::_read_device_file", "step_failed"),
    ("reporting/attribution.py::_read_device_file", ECHO),
    ("reporting/attribution.py::_criterion_failure", "equals"),
    ("reporting/attribution.py::_criterion_failure", "criterion"),
    ("reporting/attribution.py::_read_scenarios", "scenario_invalid"),
    ("reporting/attribution.py::_read_scenarios", "scenario_step"),
    ("reporting/attribution.py::_read_round_status", "no_round_status"),
    ("reporting/attribution.py::_read_scope", "scope_missing"),
    ("reporting/attribution.py::_read_scope", "deselected"),
    ("reporting/attribution.py::_read_scope", "skipped_case"),
    ("reporting/attribution.py::_read_scope", "skipped"),
    ("reporting/attribution.py::_read_scope", "not_run"),
    ("reporting/attribution.py::_unevidenced_failures", ECHO),
    # 文档列了 scenario-<用例名>.json 与它装的东西(参与方 / 读数键 / 每步结果 / 场景结论)
    ("reporting/scenario.py::_record_of", "«scenario-*.json»"),
    # 文档的 `task.json` 一节(128715b 补的):这一节写的字段由
    # test_task_json_section_names_emitted_fields / test_process_event_examples_match_the_source 逐条对账
    ("execution/task.py::run_task", ECHO),
    ("execution/task.py::run_task", "port_retry"),
    ("execution/task.py::_restart_product", "product_restart"),
    ("execution/task.py::_environment", "«env»"),
    ("config/paths.py::sources", "«layout»"),
    ("execution/death.py::exit_event", "product_exit"),
    ("execution/death.py::unexecuted_event", "unexecuted"),
}

# 「代码有 / 文档无」但用户会读的:**红线**,leader 按这一栏补
# docs/how-to/read-evidence.md。每一条都要写清「为什么还没写进文档」——
# 剩下的条目数不是指标,剩下的理由才是。
REPORT_JSON = (
    "文档没有 report.json 一节:verdict / columns / denominator / evidence_gaps / untested"
    " 都在它里面, report.md 只是它的渲染 —— 这一节比 task.json 更常被工具读,但报告首行"
    " 已经是人读的结论,所以延后"
)
JSONL_KINDS = (
    "文档的 kind 表只列五种;这批 kind 要么进归因四类(step_failed / platform_error /"
    " manual_step 会在报告里被渲染),要么只能 grep(injection / bench_setup)。"
    "补这五行要连带说清「哪些 kind 归因不看」,没写"
)
TO_DOC = {
    ("execution/death.py::attempt_exit", "«abandoned_product»"): (
        "只差一半:`unverified` 这个值文档在 product_exit 那条 bullet 里解释了,但没点名它属于"
        " `port_retry.abandoned_product` —— 读者会以为 product_exit.how 可能是 unverified"
        "(实际 product_exit.how 只有 self_exit / platform_killed)。该补的是一句:"
        "「`port_retry` 里可能还带一条 `abandoned_product`(被放弃那次尝试的事实),"
        "它的 how 只有 platform_killed / unverified」"
    ),
    ("reporting/report.py::as_record", ECHO): REPORT_JSON,
    ("reporting/report.py::build", "unexecuted"): REPORT_JSON,
    ("reporting/report.py::_untested", "not_run"): REPORT_JSON,
    ("reporting/report.py::_untested", "failed_without_evidence"): REPORT_JSON,
    ("reporting/evidence.py::record_injection", "injection"): JSONL_KINDS,
    ("reporting/evidence.py::record_manual_step", "manual_step"): JSONL_KINDS,
    ("reporting/evidence.py::record_bench_setup", "bench_setup"): JSONL_KINDS,
    ("reporting/evidence.py::record_step_failure", "step_failed"): JSONL_KINDS,
    ("reporting/evidence.py::record_platform_error", "platform_error"): JSONL_KINDS,
}

# 「平台内部字段」:文档不必写。判据只有一个 —— **这个字段会不会出现在用户能看到的
# 错误消息或报告首行里?会就不是内部字段。** 下面两条都过了一遍:report.py 只渲染
# product_exit / unexecuted,attribution 把它们放在 QUIET_KINDS 里只计数、不渲染,
# 所以只有手动 grep jsonl 才看得到。
INTERNAL = {
    ("reporting/report.py::_event_emit_gaps", "event_emit_failed"): (
        "报告里 evidence_gaps 的新成员(票 13a2):字段是 kind/device/missing/reason/ref,"
        "missing 是条数。它进 summary.json 与 report.md 的缺口一节;上游 round-status.json "
        "里的同一份事实由 evidence.record_event_emit_failure 写"
    ),
    ("reporting/evidence.py::report_write", "write"): (
        "驱动层写动作的回读明细:报告与归因都不渲染它(QUIET_KINDS),结论由 reading 判,"
        "只有排查「写下去没生效」时手动 grep jsonl 才看得到"
    ),
    ("reporting/evidence.py::report_degraded_block", "batch_fallback"): (
        "块读降级(合并读被拒、拆开逐点重试)的驱动层事实:同样在 QUIET_KINDS 里,不渲染、"
        "不影响结论；需要时 grep jsonl"
    ),
}

# 「文档有 / 代码从不发」与「值域不一致」:文档在撒谎,必须由 leader 改文档。
# 每条的 why 说清差在哪 —— 不许写"待定"。
DOC_GAPS = {
    ("frame", "error"): (
        "文档说失败带 error 且列举 timeout / closed / bad_crc;代码只发 timeout / closed /"
        " disconnect,`bad_crc` 从不发射 —— 用户照文档 grep bad_crc 永远 grep 不到"
    ),
    ("reading", "mode"): (
        "文档说 mode 只有 real / simulator;代码不校验这个值(config/manifest.py 只按它分支、"
        "不报错),清单里写错一个字母会原样落进证据 —— 要么代码补校验,要么文档别说死"
    ),
}


# ---------------------------------------------------------------- 源码侧(AST 扫)

_MODULE_CONSTANTS: dict[tuple[str, str], str] = {}


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """模块级字符串常量:`PROCESS_EVENTS = "process_events"`,变量键要靠它解析成字段名。"""
    found = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def _reachable_strings(tree: ast.Module) -> frozenset:
    """这份文件里能当字段值用的字符串字面量,**不含文档串**。

    文档串里就写着 timeout / unverified 这些词:把文档串算进来,改坏一个枚举值照样能在
    注释里"找到",断言就永远绿。
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or ()
            if body and isinstance(body[0], ast.Expr):
                first = body[0].value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    docstrings.add(id(body[0]))
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


def _fields(node: ast.Dict, where: str) -> list[str]:
    """一条 dict 字面量的键。`**x` 记成 `**`;变量键解析成模块常量的值。"""
    names = []
    for key in node.keys:
        if key is None:
            names.append("**")
        elif isinstance(key, ast.Constant):
            names.append(str(key.value))
        elif isinstance(key, ast.Name):
            names.append(_MODULE_CONSTANTS.get((where, key.id), key.id))
        else:
            names.append("<expr>")
    return names


def _dicts(node: ast.AST, function: str, out: list) -> None:
    """递归收 (最近的函数名, dict 字面量):"发射点"就是"哪个函数发的"。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _dicts(child, child.name, out)
            continue
        if isinstance(child, ast.Dict):
            out.append((function, child))
        _dicts(child, function, out)


def _kind_of(node: ast.Dict) -> str | None:
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "kind":
            return str(value.value) if isinstance(value, ast.Constant) else ECHO
    return None


def _scan() -> tuple[list[Point], dict[str, frozenset]]:
    """扫一遍源码:发射点清单 + 每个文件的字符串字面量(值域用)。"""
    points: list[Point] = []
    strings: dict[str, frozenset] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        where = path.relative_to(SOURCE).as_posix()
        if where.split("/")[0] not in SURFACE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings[where] = _reachable_strings(tree)
        for name, value in _module_strings(tree).items():
            _MODULE_CONSTANTS[(where, name)] = value
        found: list[tuple[str, ast.Dict]] = []
        _dicts(tree, "<module>", found)
        labelled: set[str] = set()
        for function, node in found:
            kind = _kind_of(node)
            if kind is None:
                kind = NAMED.get((where, function))
                if kind is None or function in labelled:
                    continue
                labelled.add(function)
            points.append(Point(f"{where}::{function}", kind, " ".join(_fields(node, where))))
    return points, strings


SCANNED, STRINGS = _scan()
ALL_STRINGS = frozenset().union(*STRINGS.values())
# 值域声明只在冻结清单里(EMISSIONS),不在扫描结果里:这条映射把两边接上。
# 接错了的后果很具体:07 会把「源码有闭集」误读成「源码没有闭集声明」,值域对比就变成空比。
CLOSED = {(point.where, point.kind): point.closed for point in EMISSIONS}
BY_POINT = {(point.where, point.kind): point for point in SCANNED}


def _diff_message(expected, actual) -> str:
    """差异要说清"多了哪一条",不是只报个数字 —— 只报数字等于让人去猜。"""
    mine = {(point.where, point.kind): point for point in expected}
    real = {(point.where, point.kind): point for point in actual}
    lines = [f"发射点总数:清单 {len(expected)} 个,源码 {len(actual)} 个"]
    for key in sorted(set(real) - set(mine)):
        lines.append(f"+1 {key[0]} 「{key[1]}」—— 源码有,清单没有(加进 EMISSIONS)")
    for key in sorted(set(mine) - set(real)):
        lines.append(f"-1 {key[0]} 「{key[1]}」—— 清单有,源码没有(搬家了就改清单)")
    for key in sorted(set(mine) & set(real)):
        for name in sorted(real[key].keys() - mine[key].keys()):
            lines.append(f'+1 {key[0]} 「{key[1]}」的字段 "{name}" —— 源码发,清单没写')
        for name in sorted(mine[key].keys() - real[key].keys()):
            lines.append(f'-1 {key[0]} 「{key[1]}」的字段 "{name}" —— 清单写,源码不发')
    return "\n".join(lines)


@case(id="T_00_00_07_000000_01", clause="free: 平台:证据", tags=["证据", "契约"])
def test_emitted_fields_are_exactly_the_frozen_list():
    """源码发的字段与清单逐条相等:加减一个字段都必须有人改清单。"""
    assert len(EMISSIONS) == EMISSION_COUNT, (
        f"清单里有 {len(EMISSIONS)} 条,EMISSION_COUNT 写着 {EMISSION_COUNT}:总数自己漂了"
    )
    assert {(point.where, point.kind) for point in EMISSIONS} == {
        (point.where, point.kind) for point in SCANNED
    }, _diff_message(EMISSIONS, SCANNED)
    for point in EMISSIONS:
        real = BY_POINT[(point.where, point.kind)]
        assert point.keys() == real.keys(), _diff_message((point,), [real])


@case(id="T_00_00_07_000000_02", clause="free: 平台:证据", tags=["证据", "契约"])
def test_closed_domains_are_reachable_in_their_own_file():
    """有闭集的字段:清单写的每个值都得在**本文件**的字面量里找得到。

    少一个枚举值(改名、删分支)在这里红 —— 用户按值 grep 证据会 grep 不到。
    """
    for point in EMISSIONS:
        universe = STRINGS[point.where.split("::")[0]]
        for name, values in point.closed.items():
            missing = sorted(value for value in values if value not in universe)
            assert not missing, (
                f"{point.where} 的 {name} 少了 {missing}:清单说这些值会发,"
                f"但这份文件的字面量里找不到(文档串不算)"
            )


@case(id="T_00_00_07_000000_03", clause="free: 平台:证据", tags=["证据", "契约"])
def test_task_json_layout_lists_every_resource_source():
    """layout 六样来源:五样包内资源 + 用例。少一样,「装完能用」就分不出是谁的问题。"""
    real = sorted(PACKAGE_RESOURCES) + ["cases"]
    assert sorted(LAYOUT_SOURCES) == sorted(real), (
        f"task.json 的 layout 与 PACKAGE_RESOURCES 对不上:清单 {sorted(LAYOUT_SOURCES)},"
        f"代码 {real}"
    )


@case(id="T_00_00_07_000000_04", clause="free: 平台:证据", tags=["证据", "契约"])
def test_kind_dicts_outside_the_surface_are_not_evidence():
    """范围守卫:证据面之外也有带 kind 的 dict(清单缺口 / 示例产品),它们不是证据记录。

    谁在证据面外新造一个带 kind 的 dict,这里红 —— 要么它其实是证据(搬进来),
    要么它是别的东西(写进这条白名单,一句为什么)。
    """
    outside = set()
    for path in sorted(SOURCE.rglob("*.py")):
        where = path.relative_to(SOURCE).as_posix()
        if where.split("/")[0] in SURFACE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[tuple[str, ast.Dict]] = []
        _dicts(tree, "<module>", found)
        if any(_kind_of(node) for _, node in found):
            outside.add(where.split("/")[0])
    assert outside == set(NOT_EVIDENCE), (
        f"证据面外新出现带 kind 的 dict:{sorted(outside - set(NOT_EVIDENCE))};"
        f"清单里多出来的:{sorted(set(NOT_EVIDENCE) - outside)}"
    )


# ---------------------------------------------------------------- 文档侧

def _doc_section() -> list[str]:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index(DOC_HEADING)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            return lines[start:index]
    return lines[start:]


def _claims(text: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """一句话里的 `字段`,以及 `字段`(甲 / 乙) 这种值域声明(值不带反引号也算)。"""
    parts = text.split("`")
    spans, offset = [], 0
    for index, part in enumerate(parts):
        if index % 2:
            spans.append((offset, part))
        offset += len(part) + 1
    names, domains, consumed = [], {}, -1
    for position, span in spans:
        if position < consumed:
            continue
        tail = text[position + len(span) + 1 :]
        if tail.startswith("(") and ")" in tail:
            inner = tail[1 : tail.index(")")]
            domains[span] = tuple(value.strip().strip("`") for value in inner.split("/"))
            consumed = position + len(span) + 1 + tail.index(")") + 1
        names.append(span)
    return tuple(dict.fromkeys(names)), domains


def _doc_files(section: list[str]) -> tuple[str, ...]:
    return tuple(
        line.split("#")[0].lstrip("├└─ ").split()[0]
        for line in section
        if line.startswith(("├──", "└──"))
    )


def _doc_kind_rows(section: list[str]) -> dict:
    rows = {}
    for line in section:
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows[cells[0].strip("`")] = _claims(cells[1])
    return rows


def _doc_every_record(section: list[str]) -> tuple[str, ...]:
    for line in section:
        if "每条记录都带" in line:
            return _claims(line)[0]
    return ()


@case(id="T_00_00_07_000000_05", clause="free: 平台:证据", tags=["证据", "契约"])
def test_doc_tables_are_what_this_file_expects():
    """文档那张表必须与这里冻结的形态一致:文档改了(哪怕删一行),这里就跟着红。"""
    section = _doc_section()
    assert _doc_files(section) == DOC_FILES, (
        f"证据目录清单与文档对不上(删/加了一行?):文档 {_doc_files(section)},冻结 {DOC_FILES}"
    )
    assert _doc_kind_rows(section) == DOC_CLAIMS, (
        f"kind 表与文档对不上:文档 {_doc_kind_rows(section)},冻结的 {DOC_CLAIMS}"
    )
    assert _doc_every_record(section) == DOC_EVERY_RECORD, (
        f"「每条记录都带 X」与文档对不上:文档 {_doc_every_record(section)}"
    )


@case(id="T_00_00_07_000000_06", clause="free: 平台:证据", tags=["证据", "契约"])
def test_doc_claims_are_emitted_by_the_source():
    """文档说的每个 kind / 字段,源码都必须真的发 —— 否则文档在撒谎。"""
    section = _doc_section()
    jsonl = {point.kind: point for point in SCANNED if point.where.startswith(JSONL_FILE)}
    sentences = []
    for kind, (names, _) in _doc_kind_rows(section).items():
        point = jsonl.get(kind)
        if point is None:
            sentences.append(f"文档有 / 代码从不发:kind 「{kind}」")
            continue
        for name in names:
            if name not in point.keys():
                sentences.append(f"文档有 / 代码从不发:{kind} 的字段 「{name}」")
    wrapper = BY_POINT[(f"{JSONL_FILE}::record", "«每条记录外层»")]
    for name in _doc_every_record(section):
        if name not in wrapper.keys():
            sentences.append(f"「每条记录都带 {name}」但包装层不发 「{name}」")
    assert not sentences, "文档写了代码不发的字段:\n" + "\n".join(sentences)


@case(id="T_00_00_07_000000_07", clause="free: 平台:证据", tags=["证据", "契约"])
def test_doc_value_domains_match_the_source():
    """文档给的值域与源码的值域**集合对比**:差集非空 = 差异,差异必须记在账上。"""
    jsonl = [point for point in SCANNED if point.where.startswith(JSONL_FILE)]
    seen, details = {}, {}
    for kind, (_, domains) in _doc_kind_rows(_doc_section()).items():
        mine = [point for point in jsonl if point.kind == kind]
        if not mine:
            continue  # 整个 kind 缺了由 06 报,这里不重复
        for name, values in domains.items():
            emitted = [point for point in mine if name in point.keys()]
            if not emitted:
                continue
            source = set()
            for point in emitted:
                source.update(CLOSED.get((point.where, point.kind), {}).get(name, ()))
            if not source:
                seen[(kind, name)] = sorted(values)
                details[(kind, name)] = f"文档给了值域 {list(values)},源码没有闭集声明"
                continue
            only_doc = [value for value in values if value not in source]
            only_code = sorted(source - set(values))
            if only_doc or only_code:
                seen[(kind, name)] = sorted(values)
                details[(kind, name)] = f"文档独有 {only_doc},源码独有 {only_code}"
    assert seen, "值域断言一条也没跑到:契约没验等于没写"
    unexpected = [details[key] for key in seen if key not in DOC_GAPS]
    assert not unexpected, "文档与源码的值域不一致,账上却没有这一条:\n" + "\n".join(unexpected)
    stale = [f"{kind}.{name}:{why}" for (kind, name), why in DOC_GAPS.items() if (kind, name) not in seen]
    assert not stale, "账上记着某个值域差异,但这次没复现 —— 修好了就把账删掉:\n" + "\n".join(stale)


@case(id="T_00_00_07_000000_08", clause="free: 平台:证据", tags=["证据", "契约"])
def test_every_emission_point_is_classified():
    """每个发射点都要交代:文档写了 / 待补文档 / 内部字段。新记录不许静默溜过。"""
    classified = set(DOCUMENTED) | set(TO_DOC) | set(INTERNAL)
    unclassified = [
        (point.where, point.kind) for point in SCANNED if (point.where, point.kind) not in classified
    ]
    assert not unclassified, "这些发射点没交代过(文档写了?待补?内部字段?):\n" + "\n".join(
        f"  {where} 「{kind}」" for where, kind in unclassified
    )
    surplus = sorted(key for key in classified if not any((point.where, point.kind) == key for point in SCANNED))
    assert not surplus, f"这些条目在源码里找不到发射点了: {surplus}"
    for point, why in INTERNAL.items():
        assert why.strip(), f"{point} 标成内部字段却没写为什么"
    for point, why in TO_DOC.items():
        assert why.strip(), f"{point} 进了待补文档却没写该补什么"


# ---------------------------------------------------------------- 文档的 task.json 一节

def _section(prefix: str) -> list[str]:
    """从以 prefix 开头的那行到下一个同级或更高级标题为止。"""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(prefix))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    section = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        section.append(line)
    return section


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _task_doc_fields() -> tuple[str, ...]:
    """`task.json` 一节字段表第一列点名的字段(第二节以后是 process_events / env 三态,不算)。"""
    names = []
    for line in _section("## `task.json`"):
        if line.startswith("###"):
            break
        if line.startswith("| `"):
            names.extend(_claims(_table_cells(line)[0])[0])
    return tuple(dict.fromkeys(names))


def _process_event_examples() -> list[dict[str, str]]:
    """那四条 json 示例:取每条里的键与值(不去 json.loads —— 示例里有 `…` 占位,不是合法 JSON)。"""
    examples = []
    for line in _section("### `process_events`"):
        body = line.strip()
        if not (body.startswith('{"kind"') and body.endswith("}")):
            continue
        pairs = {}
        for chunk in body[1:-1].split(","):
            key, _, value = chunk.partition(":")
            if not value:
                continue  # `"…"` 这种占位
            pairs[key.strip().strip('"')] = value.strip().strip('"')
        examples.append(pairs)
    return examples


@case(id="T_00_00_07_000000_09", clause="free: 平台:证据", tags=["证据", "契约"])
def test_task_json_section_names_emitted_fields():
    """文档 `task.json` 一节点名的字段,必须都是 task.json 里真的有的键。"""
    named = _task_doc_fields()
    assert named == TASK_DOC_FIELDS, (
        f"task.json 一节的字段表变了:文档 {named},冻结 {TASK_DOC_FIELDS}"
        "(改了文档就要同步这里,否则新字段没人验)"
    )
    keys = BY_POINT[("execution/task.py::run_task", ECHO)].keys()
    missing = [name for name in named if name not in keys]
    assert not missing, (
        f"文档点名了 task.json 里没有的字段: {missing};"
        f"task.json 实际有 {sorted(keys)}"
    )


@case(id="T_00_00_07_000000_10", clause="free: 平台:证据", tags=["证据", "契约"])
def test_process_event_examples_match_the_source():
    """`process_events` 那四条示例:kind 要发、键要在、值域要对得上(集合相等)。"""
    examples = _process_event_examples()
    kinds = [example["kind"] for example in examples]
    assert sorted(kinds) == sorted(PROCESS_EVENT_KINDS), (
        f"文档里的 process_events 种类变了:文档 {kinds},冻结 {sorted(PROCESS_EVENT_KINDS)}"
    )
    problems = []
    for example in examples:
        point = BY_POINT[PROCESS_EVENT_KINDS[example["kind"]]]
        for name, value in example.items():
            if name not in point.keys():
                problems.append(f"{example['kind']}.{name}:文档写了,{point.where} 不发")
                continue
            if "|" not in value:
                continue  # 单个示例值(0 / null / …)不是值域声明
            documented = value.split("|")
            source = CLOSED.get((point.where, point.kind), {}).get(name)
            if source is None:
                # 值域定义在别的文件里(unexecuted.reason 在 process_control.py):只要求能找得到
                lost = [item for item in documented if item not in ALL_STRINGS]
                if lost:
                    problems.append(f"{example['kind']}.{name}:这些值源码里找不到 {lost}")
                continue
            only_doc = [item for item in documented if item not in source]
            only_code = sorted(set(source) - set(documented))
            if only_doc or only_code:
                problems.append(
                    f"{example['kind']}.{name}:文档独有 {only_doc},源码独有 {only_code}"
                )
    assert not problems, "process_events 示例与源码对不上:\n" + "\n".join(problems)
