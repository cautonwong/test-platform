"""
任务报告(票 04)—— 一个任务跑完,断言"这一轮到底过没过",以及过没过之外的三件事。

一个任务目录里写两份,同一份结论:
    report.md     人读:四档结论、两栏通过率与分母、不合格项(设备 / 条款 / 实测 / 边界 / 超出多少)、
                  场景失败的归属、未测两栏、产品退出与未执行分开陈述
    summary.json  机器读:同一份结论 + runtime / env(字段同 task.json 的 runtime 三项与 env 四项),
                  失败与未测的分类清单照原样带出,门禁(票 05)只读这一份
                  再加一项 pytest_exit_code:pytest 自己的退出码,诊断用,不进判据

四档按优先级判,先命中先算:
    ① 不合格   有被测设备不合格(读数越过 reject 判据 / 场景里被测设备的一步不成立),
               或产品自己退出(product_exit 的 how=self_exit)
    ② 测试无效 没有不合格,但有判定被环境吞掉:断链 / 台架故障 / 场景只有周边设备的步骤不成立
    ③ 未完成   没有①②,但存在申报外未测(没跑到 / 被叫停 / 起不来),或有平台错误
    ④ 合格     命中 > 0 条,两栏 100%,申报外未测 0 条
命中 0 条不算合格:一条都没选中时两栏都是 0/0,"100%" 是空分母造出来的绿。
设备级 0/0 还有第二种来路:这一轮一台设备都没验(只跑了场景用例)。它有两种读法,分叉靠任务的 kind
(见 execution/task.py):
    kind: acceptance(缺省)  没验到设备就不算验过 → ④ 不成立,落③,reason=no_device_coverage
    kind: smoke             验的是通路不是设备 → 允许 ④,但首行必须写成「合格(冒烟:本轮未验设备)」
冒烟只是把首行写清楚,不是把设备那一栏的缺口涂绿。
平台错误不设第五档:起不来时全部用例记未执行,结论落③,结论行与 summary.json 带
reason=platform_error,门禁据此指向平台,而不是产品。

两栏通过率的粒度不同,分母都写出来:
    设备级  N/M 台    按设备参数化的用例覆盖到的设备(收集期就落在 run-scope 的 devices 里);
                    场景里不成立的步骤归场景那一栏,不在这里重复扣一次
    场景级  N/M 条    一个 S_ 节点算 1 条,不按参与设备展开、不按条款展开
任务结论取两栏最差:一栏没满,任务就算没合格。

未测分两栏,理由不许自由发挥:
    申报内(灰)  理由只在四类里:平台不支持 / 设备缺能力 / 人工步骤 / 已知缺陷跳过。
                用例作者写下的 skip 理由按这几类的写法识别;识别不出来的一律算申报外 ——
                一句"因为所以"就能把未测涂成灰色的地方,和合格只差一次眨眼。
    申报外(红)  没跑到 / 被平台叫停未执行 / 失败但没落证据 / 理由不在四类里。
                有任意一条,结论就不能是合格。

这一层只读:证据在任务目录里已经有了(每设备 JSONL、scenario-*.json、run-scope.json、
platform-errors.jsonl、task.json),分类复用 reporting.attribution 的口径,报告是它们的投影,
不是第二份真相源,也不改任何既有行为。

死因的 how 取值(self_exit / platform_killed)是票 03 定下的 wire 契约(见 execution/death.py),
这里按字面读,不 import 执行层 —— 报告不该反向依赖谁跑出来的证据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testplatform.clauses import ClauseRegistry, load_clauses
from testplatform.reporting.attribution import (
    SCENARIO_PREFIX,
    Attribution,
    attribute,
)
from testplatform.reporting.evidence import ROUND_STATUS_FILE

REPORT_FILE = "report.md"
SUMMARY_FILE = "summary.json"
# 执行器写的那一份任务事实(见 execution/task.py)。报告只读它,所以这里不复用那里的常量:
# reporting/ 认识 execution/ 会变成双向依赖。
TASK_FILE = "task.json"

# 场景用例的编号前缀:一个 S_ 节点算一条场景,不按参与设备展开。
SCENARIO_CASE_PREFIX = "S_"

# 死因的 how 字面量(wire 契约,见 execution/death.py):产品自己退出 = 产品不合格。
SELF_EXIT = "self_exit"

# 任务的 kind 字面量(wire 契约,见 execution/task.py 的 task.json):不写就是 acceptance。
KIND_ACCEPTANCE = "acceptance"
KIND_SMOKE = "smoke"

VERDICT_FAILED = "failed"
VERDICT_INVALID = "invalid"
VERDICT_INCOMPLETE = "incomplete"
VERDICT_QUALIFIED = "clean"

REASON_PLATFORM_ERROR = "platform_error"
# 验收任务一台设备都没验到:没验到不等于通过。
REASON_NO_DEVICE_COVERAGE = "no_device_coverage"

PLATFORM_UNSUPPORTED = "平台不支持"
DEVICE_CAPABILITY = "设备缺能力"
MANUAL_STEPS = "人工步骤"
KNOWN_DEFECT = "已知缺陷跳过"
DECLARED_REASONS = (
    PLATFORM_UNSUPPORTED,
    DEVICE_CAPABILITY,
    MANUAL_STEPS,
    KNOWN_DEFECT,
)

# skip 理由 → 申报内的哪一类。按写法认,不按设备认;认不出来的是申报外。
_SKIP_MARKERS = (
    ("待人工确认", MANUAL_STEPS),
    ("未声明能力", DEVICE_CAPABILITY),
    ("没有型号", DEVICE_CAPABILITY),
    ("不在这份清单里", DEVICE_CAPABILITY),
    ("清单里没有", PLATFORM_UNSUPPORTED),
    ("real_hardware", PLATFORM_UNSUPPORTED),
    ("模拟器模式", PLATFORM_UNSUPPORTED),
    ("已知缺陷", KNOWN_DEFECT),
)

_VERDICT_TEXT = {
    VERDICT_FAILED: "不合格",
    VERDICT_INVALID: "测试无效",
    VERDICT_INCOMPLETE: "未完成",
    VERDICT_QUALIFIED: "合格",
}

_NOTES = {
    VERDICT_FAILED: (
        "有被测设备不合格:读数越过 reject 级判据 / 场景里被测设备的一步不成立 / 产品自己退出"
    ),
    VERDICT_INVALID: (
        "没有不合格,但有判定被环境吞掉:断链 / 台架故障 / 场景只有周边设备的步骤不成立 —— 与产品无关"
    ),
    VERDICT_INCOMPLETE: "没有不合格也没有无效判定,但存在申报外未测或平台错误:不能判合格",
    VERDICT_QUALIFIED: "两栏 100% 且申报外未测 0 条",
}
_PLATFORM_ERROR_NOTE = "产品起不来 / 任务配置错:平台错误,不是产品不合格"
_NO_DEVICE_COVERAGE_NOTE = (
    "设备级 0/0:这一轮一台设备都没验 —— 验收任务不能拿没验到的设备算合格"
    "(验通路的任务把 kind 写成 smoke,并在首行写明)"
)


@dataclass
class Report:
    """一轮任务的报告:结论 + 两栏通过率 + 分门别类的清单。"""

    directory: Path
    task_id: str | None
    kind: str
    product: str | None
    select: str | None
    verdict: str
    note: str
    reason: str | None
    devices_passed: int
    devices_total: int
    scenarios_passed: int
    scenarios_total: int
    collected: int
    deselected: int
    failures: list = field(default_factory=list)
    scenarios: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    platform_errors: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    declared_untested: list = field(default_factory=list)
    outside_untested: list = field(default_factory=list)
    devices_without_readings: list = field(default_factory=list)
    out_of_scope_devices: list = field(default_factory=list)
    product_exit: list = field(default_factory=list)
    task_error: str | None = None
    runtime: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    # pytest 自己的退出码(诊断用):它不进判据,四个结论一律由证据判。
    pytest_exit_code: int | None = None

    def as_record(self) -> dict:
        """机器可读的那一份:字段与 attribution.json / scenario-*.json 同一风格。"""
        return {
            "task": self.task_id,
            "kind": self.kind,
            "product": self.product,
            "select": self.select,
            "verdict": self.verdict,
            "pytest_exit_code": self.pytest_exit_code,
            "note": self.note,
            "reason": self.reason,
            "columns": {
                "devices": {"passed": self.devices_passed, "total": self.devices_total},
                "scenarios": {"passed": self.scenarios_passed, "total": self.scenarios_total},
            },
            "denominator": {"collected": self.collected, "deselected": self.deselected},
            "untested": {
                "declared": self.declared_untested,
                "outside": self.outside_untested,
            },
            "failures": self.failures,
            "scenarios": self.scenarios,
            "invalid": self.invalid,
            "platform_errors": self.platform_errors,
            "evidence_gaps": self.gaps,
            "devices_without_readings": self.devices_without_readings,
            "out_of_scope_devices": self.out_of_scope_devices,
            "product_exit": self.product_exit,
            "task_error": self.task_error,
            "runtime": self.runtime,
            "env": self.env,
        }


def build(directory: str | Path) -> Report:
    """读一个任务目录,给出四档结论。不写任何判定:分类口径来自 attribution。"""
    directory = Path(directory)
    attribution = attribute(directory)
    task = _json(directory / TASK_FILE)
    scope = _json(directory / "run-scope.json")
    scenarios = _scenario_records(directory)

    collected_entries = [
        entry for entry in scope.get("collected") or () if isinstance(entry, dict)
    ]
    executed = set(scope.get("executed") or ())
    # 判据只用范围事实:执行过的、以及范围里记了失败的。两者都不读,就没有别的来源。
    failed_nodeids = {
        record.get("nodeid")
        for key in ("failed", "errored")
        for record in scope.get(key) or ()
        if isinstance(record, dict)
    }
    nodes_by_device: dict[str, list[str]] = {}
    for entry in collected_entries:
        for device in entry.get("devices") or ():
            nodes_by_device.setdefault(device, []).append(entry.get("nodeid"))
    failed_devices = {
        failure["device"]
        for failure in attribution.device_failures
        if failure["kind"] != "scenario_step" and failure.get("device") in nodes_by_device
    }
    devices_passed = sum(
        1
        for device, nodeids in nodes_by_device.items()
        if device not in failed_devices and any(node in executed for node in nodeids)
    )
    scenario_cases = [
        entry.get("case")
        for entry in collected_entries
        if str(entry.get("case") or "").startswith(SCENARIO_CASE_PREFIX)
    ]
    passed_scenarios = {row["case"] for row in scenarios if row["verdict"] == "passed"}
    scenarios_passed = sum(1 for case_id in scenario_cases if case_id in passed_scenarios)

    declared, outside, gaps = _untested(attribution, executed, failed_nodeids)
    # 事件没送到 sink(票 13a2):也是"证据缺了一块",进同一个 family,`missing` 是条数
    gaps.extend(_event_emit_gaps(directory))
    product_exit = [
        event
        for event in task.get("process_events") or ()
        if event.get("kind") == "product_exit"
    ]
    outside += [
        {
            "kind": "unexecuted",
            "count": event.get("count"),
            "reason": f"平台叫停本轮,没跑到(reason={event.get('reason')})",
            "ref": f"{TASK_FILE}#process_events",
        }
        for event in task.get("process_events") or ()
        if event.get("kind") == "unexecuted"
    ]
    task_error = task.get("error")
    devices_missing = devices_passed < len(nodes_by_device)
    scenarios_missing = scenarios_passed < len(scenario_cases)
    kind = str(task.get("kind") or KIND_ACCEPTANCE)
    # 设备级 0/0:这一轮一台设备都没验。冒烟任务验的是通路,允许合格;坑在于首行得写明。
    uncovered = not nodes_by_device
    no_device_coverage = uncovered and kind != KIND_SMOKE

    verdict = _verdict(
        attribution=attribution,
        product_exit=product_exit,
        outside=outside,
        gaps=gaps,
        task_error=task_error,
        devices_missing=devices_missing,
        scenarios_missing=scenarios_missing,
        selected=len(collected_entries),
        no_device_coverage=no_device_coverage,
    )
    if task_error:
        reason = REASON_PLATFORM_ERROR
    elif verdict == VERDICT_INCOMPLETE and no_device_coverage:
        reason = REASON_NO_DEVICE_COVERAGE
    else:
        reason = None
    return Report(
        directory=directory,
        task_id=task.get("task"),
        kind=kind,
        product=task.get("product"),
        select=task.get("select"),
        verdict=verdict,
        note=_note(verdict, reason, len(nodes_by_device), len(scenario_cases)),
        reason=reason,
        devices_passed=devices_passed,
        devices_total=len(nodes_by_device),
        scenarios_passed=scenarios_passed,
        scenarios_total=len(scenario_cases),
        collected=len(collected_entries),
        deselected=len(scope.get("deselected") or ()),
        failures=attribution.device_failures,
        scenarios=scenarios,
        invalid=attribution.invalid_tests,
        platform_errors=attribution.platform_errors,
        gaps=gaps,
        declared_untested=declared,
        outside_untested=outside,
        # 没有读数的设备两种来路:用例没跑到(not_run_devices)与跑到了却没读数(no_readings),
        # 归因已经分好,这里只把名单合起来 —— 一台设备只在其中一边。
        devices_without_readings=sorted(
            set(attribution.not_run_devices)
            | {
                entry["device"]
                for entry in attribution.no_conclusion
                if entry["kind"] == "no_readings"
            }
        ),
        out_of_scope_devices=attribution.out_of_scope,
        product_exit=product_exit,
        task_error=task_error,
        runtime=task.get("runtime") or {},
        env=task.get("env") or {},
        pytest_exit_code=task.get("pytest_exit_code"),
    )


def write_report(directory: str | Path) -> Path:
    """写 report.md 与 summary.json 到任务目录,返回人读报告路径。只读证据,不改证据。"""
    directory = Path(directory)
    report = build(directory)
    path = directory / REPORT_FILE
    path.write_text("\n".join(render(report, load_clauses())) + "\n", encoding="utf-8")
    (directory / SUMMARY_FILE).write_text(
        json.dumps(report.as_record(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _note(verdict: str, reason: str | None, devices_total: int, scenarios_total: int) -> str:
    """结论后那句话。"两栏 100%" 只在两栏里真有条目时才说得出口:0/0 上的 100% 是空分母。"""
    if reason == REASON_PLATFORM_ERROR:
        return _PLATFORM_ERROR_NOTE
    if reason == REASON_NO_DEVICE_COVERAGE:
        return _NO_DEVICE_COVERAGE_NOTE
    if verdict == VERDICT_QUALIFIED and not devices_total:
        if scenarios_total:
            return "冒烟任务验的是通路,不是设备:设备级 0/0 按冒烟放行"
        return "这一轮没有按设备展开的用例,也没有场景用例:申报外未测 0 条"
    return _NOTES[verdict]


def _verdict(
    *,
    attribution: Attribution,
    product_exit: list,
    outside: list,
    gaps: list,
    task_error: str | None,
    devices_missing: bool,
    scenarios_missing: bool,
    selected: int,
    no_device_coverage: bool,
) -> str:
    """四档,先命中先算。① 连"产品自己退出"一起算:那是产品不合格,不是测试无效。"""
    if attribution.device_failures or any(
        event.get("how") == SELF_EXIT for event in product_exit
    ):
        return VERDICT_FAILED
    if attribution.invalid_tests:
        return VERDICT_INVALID
    if (
        outside
        or gaps
        or attribution.platform_errors
        or task_error
        or devices_missing
        or scenarios_missing
        or selected == 0
        or no_device_coverage
    ):
        return VERDICT_INCOMPLETE
    return VERDICT_QUALIFIED


def _untested(
    attribution: Attribution, executed: set, failed_nodeids: set
) -> tuple[list, list, list]:
    """未测分三堆:申报内(灰)、申报外(红)、范围与证据的缺口。

    跳过条目里的测试无效已被 attribution 挪进 invalid_tests,这里不会再见到它们;
    人工步骤的 manual_step 记录与它那条 skip 说的是同一件事,计数只认 skip 那条。

    "没跑到"以 run-scope 的 executed 为准:归因那边靠"证据里有没有这条用例的身份"判断,
    对不写用例身份的用例(平台自己的闭环用例)会把"跑过了"读成"没跑到"。

    "这台设备没有读数"不是一件事:选中的用例本来就不读数(跑到了、过了)与可能真漏测,
    缺口结论不同。分不分得出只看 run-scope 的 executed 与 failed/errored,不看那一行为什么没读数。
    """
    executed = executed or set()
    declared: list[dict] = []
    outside: list[dict] = []
    gaps: list[dict] = [
        dict(entry, kind=entry.get("kind") or "evidence_corrupt")
        for entry in attribution.evidence_corrupt
    ]

    for entry in attribution.no_conclusion:
        kind = entry["kind"]
        if kind == "deselected":
            continue  # 不在本轮范围:没被筛中的不叫未测
        if kind == "manual_step":
            continue  # 与它那条 skip 重复,理由与已执行步数在 skip 那条里
        if kind == "skipped":
            written = entry.get("reason") or ""
            row = {"case": entry.get("case"), "nodeid": entry.get("nodeid"), "ref": entry["ref"]}
            category = _declared_reason(written)
            if category is None:
                outside.append(
                    dict(row, kind="skip", reason=f"理由不在申报的四类里:{written or '没写理由'}")
                )
            else:
                declared.append(dict(row, kind="skip", reason=category, declared=written))
            continue
        if kind == "not_run":
            for item in entry.get("items") or ():
                if item.get("nodeid") in executed:
                    continue  # 跑过了,只是没写用例身份:不算未测
                outside.append(
                    {
                        "kind": "not_run",
                        "case": item.get("case"),
                        "nodeid": item.get("nodeid"),
                        "reason": "收集到了,证据里没有它:没跑到",
                        "ref": entry["ref"],
                    }
                )
            continue
        if kind == "no_readings" and _readings_were_not_in_the_deal(
            entry, executed, failed_nodeids
        ):
            continue  # 设备仍在 devices_without_readings 里露名,只是这一条不拉低结论
        gaps.append(dict(entry, kind=kind))
    for entry in attribution.unevidenced_failures:
        outside.append(
            {
                "kind": "failed_without_evidence",
                "case": entry.get("case"),
                "nodeid": entry.get("nodeid"),
                "reason": f"pytest 报了失败,证据里没有结论:{entry.get('reason')}",
                "ref": entry["ref"],
            }
        )
    return declared, outside, gaps


def _readings_were_not_in_the_deal(entry: dict, executed: set, failed_nodeids: set) -> bool:
    """这台设备的用例都执行过、且没有失败 → 它们本来就不读数,不算证据缺口。

    只否掉分得清的那一档:任一条用例没进 executed,或者进了 executed 却失败了
    (中途崩、写证据之前就断了),缺口照旧拉低结论 —— 宁可红。
    """
    nodeids = entry.get("case_nodeids") or ()
    return bool(nodeids) and all(
        nodeid in executed and nodeid not in failed_nodeids for nodeid in nodeids
    )


def _declared_reason(written: str) -> str | None:
    """这条 skip 的理由落在申报四类的哪一类;认不出来 = 申报外(None)。"""
    for marker, category in _SKIP_MARKERS:
        if marker in written:
            return category
    return None


def _scenario_records(directory: Path) -> list[dict]:
    """每个场景一份结论连同它不成立的步骤:归属靠这些步骤落在谁身上,不靠一句"场景失败"。"""
    rows = []
    for path in sorted(directory.glob(f"{SCENARIO_PREFIX}*.json")):
        data = _json(path)
        if not data:
            continue
        case = data.get("case") if isinstance(data.get("case"), dict) else {}
        steps = [step for step in data.get("steps") or () if isinstance(step, dict)]
        rows.append(
            {
                "scenario": data.get("scenario") or path.stem[len(SCENARIO_PREFIX) :],
                "case": case.get("id"),
                "clause": case.get("clause"),
                "verdict": data.get("verdict"),
                "cleanup_failed": bool(data.get("cleanup_failed")),
                "failed_steps": [
                    {"index": index, **step}
                    for index, step in enumerate(steps)
                    if not step.get("passed")
                ],
                "ref": path.name,
            }
        )
    return rows


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------ 人读报告 ----


def render(report: Report, registry: ClauseRegistry) -> list[str]:
    """人读的那一份:结论第一行,然后两栏通过率、分母、分门别类的清单。"""
    lines = [
        _headline(report),
        f"任务 {report.task_id or '未知'} · 产品 {report.product or '未知'} · 筛选 {report.select or '无'}",
        f"通过率: 设备级 {report.devices_passed}/{report.devices_total} 台"
        f" · 场景级 {report.scenarios_passed}/{report.scenarios_total} 条(任务结论取两栏最差)",
        f"分母: select ∩ 适用矩阵 命中 {report.collected} 条"
        f"(不含被 --select / 适用矩阵筛掉的 {report.deselected} 条)",
        "",
    ]
    lines += _render_failures(report.failures, registry)
    lines += _render_scenarios(report, registry)
    lines += _render_invalid(report.invalid, registry)
    lines += _render_untested(report)
    if report.devices_without_readings:
        lines.append(
            f"另有 {len(report.devices_without_readings)} 台设备没有读数"
            "(名单见 summary.json 的 devices_without_readings)"
        )
    if report.out_of_scope_devices:
        lines.append(
            f"另有 {len(report.out_of_scope_devices)} 台设备不在本轮范围内"
            "(名单见 summary.json 的 out_of_scope_devices)"
        )
    if report.devices_without_readings or report.out_of_scope_devices:
        lines.append("")
    lines += _render_product_exit(report.product_exit)
    lines += _render_platform_errors(report.platform_errors)
    lines += _render_gaps(report.gaps)
    return lines


def _headline(report: Report) -> str:
    reason = f"(reason: {report.reason})" if report.reason else ""
    return (
        f"结论: {_VERDICT_TEXT[report.verdict]}{_smoke_marker(report)}{reason}"
        f" —— {report.note}{_platform_marker(report)}"
    )


def _platform_marker(report: Report) -> str:
    """首行末尾写平台来源(票 15a)。

    `project` = 平台在用户项目里、**用户能改它** —— 这一轮的结论可靠性取决于改了什么,
    报告是唯一能承载这条事实的地方。`package` 也写:不差的字段不该时有时无。
    """
    source = (report.env or {}).get("platform_source")
    return f" · platform_source: {source or 'unknown'}"


def _smoke_marker(report: Report) -> str:
    """冒烟任务 + 设备级 0/0 时记明这是冒烟:单写"合格"等于说这一轮验过设备。"""
    if report.kind == KIND_SMOKE and report.devices_total == 0:
        return "(冒烟:本轮未验设备)"
    return ""


def _render_failures(entries: list, registry: ClauseRegistry) -> list[str]:
    lines = [f"[不合格] {len(entries)} 条(设备 id · 实测值 · 边界 · 超出多少)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        if entry["kind"] in ("criterion", "equals", "step_failed"):
            lines.append(
                f"  {entry['device']}  {_measured_text(entry)}"
                f"  条款 {_clause_text(registry, _case_field(entry, 'clause'))}"
                f"  用例 {_case_field(entry, 'id') or '未声明'}"
                f"  证据 {entry['ref']}"
            )
        else:
            detail = entry.get("detail") or {}
            lines.append(
                f"  {entry.get('device')}  场景步骤「{entry.get('action')}」不成立 {detail}"
                f"  条款 {_clause_text(registry, _case_field(entry, 'clause'))}"
                f"  证据 {entry['ref']}"
            )
    return lines + [""]


def _measured_text(entry: dict) -> str:
    """实测值、边界、超出多少:三个数分开写。等值违约没有边界,只写期望值。"""
    quantity = entry.get("quantity")
    value = entry.get("value")
    if value is None:
        return entry.get("text") or str(quantity)
    measured = f"{quantity} 实测 {value}"
    if entry.get("expected") is not None:
        return f"{measured},期望 {entry['expected']}"
    limit = entry.get("limit")
    exceeded = entry.get("exceeded_by")
    if limit is None:
        return measured
    return f"{measured},边界 {limit},超出 {exceeded}"


def _render_scenarios(report: Report, registry: ClauseRegistry) -> list[str]:
    """场景失败标归属:设备 / 台架 / 平台,一条一行,不合并成"场景失败"。"""
    platform_cases = {
        entry["case"]["id"]
        for entry in report.platform_errors
        if isinstance(entry.get("case"), dict) and entry["case"].get("id")
    }
    rows = [row for row in report.scenarios if row["verdict"] != "passed"]
    lines = [f"[场景归属] {len(rows)} 条(设备 / 台架 / 平台,不合并成一句「场景失败」)"]
    if not rows:
        return lines + ["  无", ""]
    for row in rows:
        owner = _scenario_owner(row, platform_cases)
        steps = ", ".join(
            f"{step.get('device')}「{step.get('action')}」" for step in row["failed_steps"]
        ) or "无(没有步骤就判了不通过:用例在写出步骤之前中断,或收尾失败)"
        cleanup = " 收尾失败" if row["cleanup_failed"] else ""
        lines.append(
            f"  [场景归属] {owner}  场景 {row['scenario']}  判定 {row['verdict']}"
            f"  不成立步骤 {steps}{cleanup}"
            f"  条款 {_clause_text(registry, row['clause'])}"
            f"  证据 {row['ref']}#steps"
        )
    return lines + [""]


def _scenario_owner(row: dict, platform_cases: set[str]) -> str:
    """归属按结论算:同一用例上有平台错误 → 平台;invalid = 只有周边设备的步骤不成立 → 台架;
    其余 failed 是被测设备的步骤不成立 → 设备。"""
    if row["case"] and row["case"] in platform_cases:
        return "平台"
    if row["verdict"] == "invalid":
        return "台架"
    return "设备"


def _render_invalid(entries: list, registry: ClauseRegistry) -> list[str]:
    lines = [f"[测试无效] {len(entries)} 条(断链 / 台架故障:判定被环境吞掉,不是设备不合格)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        if entry["kind"] == "link_lost":
            lines.append(
                f"  {entry.get('device')}  {entry.get('operation')} 阶段断链"
                f"({entry.get('failure')}): {entry.get('message')}  证据 {entry['ref']}"
            )
        elif entry["kind"] == "scenario_invalid":
            lines.append(
                f"  场景 {entry['scenario']}  判 invalid(只有周边设备的步骤不成立)"
                f"  条款 {_clause_text(registry, _case_field(entry, 'clause'))}  证据 {entry['ref']}"
            )
        else:
            lines.append(
                f"  用例 {_case_field(entry, 'id') or '未声明'}"
                f"  条款 {_clause_text(registry, _case_field(entry, 'clause'))}"
                f"  原因 {entry.get('message')}  证据 {entry['ref']}"
            )
    return lines + [""]


def _render_untested(report: Report) -> list[str]:
    """未测两栏:申报内(灰)理由只有四类;申报外(红)有一条就不能判合格。"""
    declared = report.declared_untested
    lines = [
        f"[未测·申报内(灰)] {len(declared)} 条 —— 理由只用四类:{' / '.join(DECLARED_REASONS)}"
    ]
    if declared:
        for row in declared:
            lines.append(
                f"  {row['reason']}  {row.get('case') or row.get('nodeid')}"
                f"  申报原文 {row.get('declared')}  证据 {row['ref']}"
            )
    else:
        lines.append("  无")
    outside = report.outside_untested
    lines.append(
        f"[未测·申报外(红)] {len(outside)} 条 —— 有一条,结论就不能是合格"
    )
    if outside:
        for row in outside:
            lines.append(_outside_text(row))
    else:
        lines.append("  无")
    return lines + [""]


def _outside_text(row: dict) -> str:
    if row.get("count") is not None:
        who = f"未执行 {row['count']} 条"
    elif row.get("case"):
        who = row["case"]
    elif row.get("device"):
        who = f"设备 {row['device']}"
    else:
        who = row.get("nodeid") or "未指明"
    return f"  {who}  {row['reason']}  证据 {row['ref']}"


def _render_product_exit(events: list) -> list[str]:
    """产品退出单独陈述:它和"未执行 N 条"是两条记录,合成一句就只能读到"出事了"。"""
    if not events:
        return []
    lines = [f"[产品退出] {len(events)} 次"]
    for event in events:
        signal = event.get("signal")
        lines.append(
            f"  产品退出(how={event.get('how')}, code={event.get('code')},"
            f" signal={signal if signal is not None else '无'})"
            + (" —— 产品自己退出 = 产品不合格" if event.get("how") == SELF_EXIT else
               " —— 平台发起的终止,不产生结论")
        )
    return lines + [""]


def _render_platform_errors(entries: list) -> list[str]:
    lines = [f"[平台错误] {len(entries)} 条(聚合根不变量被违反:平台自身有 bug)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        lines.append(
            f"  设备 {entry.get('device') or '未标注'}  {entry.get('error')}: {entry.get('message')}"
            f"  证据 {entry['ref']}"
        )
    return lines + [""]


def _event_emit_gaps(directory) -> list[dict]:
    """这一轮有没有事件没送到 sink(票 13a2)。

    来源是 round-status.json:`missing` 是条数 —— "丢了 3 条 event" 比 "可能丢了" 有用,
    而报告是复盘的人真正会读的那一份(控制台里一行 ERROR 不算"可见")。
    """
    try:
        status = json.loads((Path(directory) / ROUND_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        {
            "kind": "event_emit_failed",
            "device": entry.get("device"),
            "missing": int(entry.get("missing") or 0),
            "reason": entry.get("reason"),
            "ref": ROUND_STATUS_FILE,
        }
        for entry in status.get("event_emit_failed") or []
    ]


def _render_gaps(entries: list) -> list[str]:
    if not entries:
        return []
    lines = [f"[范围与证据缺口] {len(entries)} 条(结论不能建立在这些证据上)"]
    for entry in entries:
        device = f"设备 {entry['device']}  " if entry.get("device") else ""
        lines.append(f"  {device}{entry.get('reason') or entry.get('kind')}  证据 {entry['ref']}")
    return lines + [""]


def _case_field(entry: dict, key: str) -> Any:
    case = entry.get("case")
    if isinstance(case, dict):
        return case.get(key)
    return None


def _clause_text(registry: ClauseRegistry, reference: Any) -> str:
    """条款只渲染清单里的两个权威字段(standard + number)与条款 id;summary 不进人读报告。

    自由文本(free: <原文>)照原文显示:它不是标准条款,套一个括号只会看着像。
    """
    if not reference:
        return "未声明"
    written = str(reference)
    if registry.has(written):
        return f"{written}({registry.get(written).label})"
    if registry.is_free(written):
        return registry.free_text(written)
    return written
