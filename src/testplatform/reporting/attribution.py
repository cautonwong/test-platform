"""
一轮的失败归因入口 —— 读一遍一个证据目录,回答四个问题:
    哪些设备不合格    读数越过了 reject 级判据,或场景里有被测设备的一步不成立
    哪些测试无效      周边设备断链 / 台架复位失败 / 场景被判 invalid:结论本身不成立,不是设备不合格
    哪些是平台 bug    聚合根不变量被违反(platform-errors.jsonl)
    哪些没有有效结论  被 --select 筛掉 / 被跳过 / 没跑到 / 证据文件是空的 —— 一律不算通过

还有一类不允许漏:pytest 报了失败、证据里却没有结论 —— 用例在写证据之前就崩了(setup 里或用例开头),
一条读数都没写,归因既不能说它合格,也找不到"不合格"的证据。它单列一栏「未落证据的失败」,
且这一栏非空时不许说 clean:归因层的存在意义就是报告只断言证据支持的东西。

"没有有效结论"又分两档,显示上不许合并:

    不在本轮范围  设备根本没被任何用例点到(只跑了几条场景用例时的其余设备)。只给一行计数:
                  规模下逐条列会把真问题浸掉
    没跑到        用例点了这台设备,但这条用例没进 run-scope 的 executed(跳过 / 被 -x 停下 /
                  会话被打断);这一轮一条用例都没执行时所有设备都归这一档。同样只给一行计数 ——
                  "一条用例都没跑"时逐台列 264 行,说的其实全是同一件事
    跑到了却没读数  设备的用例进了 executed,证据里却一条读数都没有。逐台列出(真漏测时就靠
                      这一档),并且说得出是哪一种:用例失败在写证据之前(见「未落证据的失败」),
                      还是选中的用例本来就不读数 —— 两种的下一步不同

设备范围在收集期就知道(按设备参数化的用例带 devices),场景用例从 scenario-*.json 的
participants 取。两边都交不出设备时归因说"范围未知",仍逐台列出:宁可留着噪声,
也不能把空读数设备说成"不在范围"。

"这一轮跑了多少条"只有 run-scope.json 的 executed 说得清:collected − skipped 会把
`pytest -x` 中途停下、会话被打断时没跑到的用例算成跑过。executed 缺失(旧证据目录)
时就说"执行条数未知",不拿 collected − skipped 冒充。机器可读那侧的 ran.executed
与终端规模行是同一个数,不会出现两处互相矛盾的"跑了 N 条"。

四类分开,一条都不许并:台架问题算到设备头上是这个项目一直在防的错。
证据文件本身坏掉另算 evidence_corrupt —— 责任方是写证据这一侧,不能算到设备头上,
也不能算成平台 bug。“坏”的含义有三种:行解析不了(不是合法 JSON / 缺 kind、device)、
归因入口自己读不懂的 JSON 文件,以及 evidence_gap —— 写证据时两轮之间的读数增量超出
history 窗口,最旧的几条已被裁掉、不在证据里。缺的条数必须报出来(“缺 5 条”与“缺 5000 条”
不是同一件事),一条“有损坏”盖过去就又把静默找回来了。

能解析、只是**归因还不认识这种 kind**的记录不算坏:单独汇总成一条提示
(未识别的记录 N 类 × M 条),不参与结论。否则每加一种记录类型都要改归因,
“证据里多了一种记录”会变成“整轮证据坏了”—— 排障方向直接跑偏。
归因不产生读数、不判合格的 kind(frame / event / write / batch_fallback)就只计入记录数。

人工步骤(manual_step)是另一档,不能只计入记录数:它不产生读数、也不判合格,但同样
**不算通过**。含人工步骤的条款在 pytest 里是 SKIPPED —— 但它是先跑完前面的自动步骤
才停的,所以证据里既有那些步骤的写入与读数记录、也有 confirmed: null 的人工占位。
归因必须自己把这类条款归进「没有有效结论」并在规模行里单列“待人工确认 N 条”,
而每条占位还要说得出“已执行的 N 步全部通过、还有 M 步待确认”(executed_steps 与同条款的
占位条数)。否则一旦有人把 skip 当“这轮没事”读过去,那 17% 的人工步骤就静默消失了。
自动步骤失败另算(step_failed):真故障不许被“等人确认”盖掉,它归「设备不合格」并点名哪一步。

这里只读证据,不写任何判定:证据在 evidence/<会话>/ 下已经有了(每台被测设备一份 JSONL、
每个场景一份 scenario-<名>.json、这一轮一份 round-status.json、范围一份 run-scope.json、
平台自己崩了时一份 platform-errors.jsonl),归因是它们的投影,不是第二份真相源。

每一条都带 ref,能指回具体的证据文件行(<设备>.jsonl:12)或 JSON 字段
(scenario-<名>.json#steps[1]),让人自己核对,而不是再听一遍解释。

机器可读的那份写回同一个目录的 attribution.json,字段与 round-status.json /
scenario-*.json 同一风格:verdict + note + 分门别类的列表。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from testplatform.composition.root import TEST_INVALID_PREFIX  # “测试无效:”只在这里定义一次
from testplatform.reporting.evidence import (
    PLATFORM_ERRORS_FILE,
    ROUND_STATUS_FILE,
    RUN_SCOPE_FILE,
    SETPOINT_RESTORE_OPERATION,
)

ATTRIBUTION_FILE = "attribution.json"
SCENARIO_PREFIX = "scenario-"

# 这些 kind 不产生读数、不判合格,归因只把它们计入“这台设备被碰过”。
# 新加一种这类记录(比如以后的“设备状态变化”)只需要把它加进这张表。
# bench_setup 是条款的台架前置下发了什么:它本身不构成任何结论(判定在随后的 reading)。
# 前置没发下去的那一侧不落成 bench_setup,而是落成 link_lost(operation: apply_three_phase)
# 进“测试无效” —— 两种现场就在这一层分开。
QUIET_KINDS = ("frame", "event", "write", "batch_fallback", "bench_setup")

_VERDICT_NOTES = {
    "platform-error": "有聚合根不变量被违反:平台自身有 bug,与被测设备无关",
    "evidence-corrupt": "证据文件本身坏掉:责任在写证据这一侧,不能算到设备头上",
    "failed": "有被测设备不合格:读数越过 reject 级判据,或场景里有被测设备的一步不成立",
    "invalid": "有测试无效:周边设备故障(断链 / 复位失败 / 台架前置没发下去),或场景只由周边设备的那一步不成立 —— 结论不成立,不是设备不合格",
    "failed-without-evidence": (
        "pytest 报了失败但证据里没有结论:说明失败发生在写证据之前 —— "
        "既不能读成通过,也不能读成设备不合格"
    ),
    "incomplete": "这一轮没有跑到任何用例:不能读成通过",
    "clean": "没有失败项:这一轮跑到的用例都没有设备不合格、测试无效、平台错误",
}

# 没有有效结论的几种来路:被筛掉、被跳过、没跑到、证据是空的。
# 归因里它们各有各的名字,不许合并成一句"跳过" —— "为什么没跑"决定了下一步该找谁。
_NO_CONCLUSION_REASONS = {
    "deselected": "--select / 适用矩阵筛掉",
    "skipped": "被跳过",
    "not_run": "收集到了,但证据里没有它的读数(可能没跑到,也可能这条用例本来就不读数)",
    "no_readings": (
        "这一轮这台设备上没有任何读数记录:执行条数未知(旧证据没有 executed),"
        "或者 run-scope 里没有落在它身上的用例 —— 判不出用例是没跑到还是本来就不读数"
    ),
    "no_readings_case_silent": (
        "这一轮这台设备的用例执行了,却没留下一条读数:选中的用例本来就不读数(正常,不是问题)"
    ),
    "no_readings_case_failed": (
        "这一轮这台设备的用例执行了,却没留下一条读数:它失败在写证据之前"
        "(见「未落证据的失败」)"
    ),
    "no_verdict": "这条读数有判据集却没有求值结论",
    "no_round_status": "会话没跑完:没有 round-status.json",
    "scope_missing": "范围缺失:不知道被筛掉/被跳过了哪些用例",
}


@dataclass
class Attribution:
    """一轮的归因:四类 + 证据损坏 + 这一轮的规模。"""

    directory: Path
    verdict: str
    note: str
    scope: dict
    ran: dict
    device_failures: list = field(default_factory=list)
    invalid_tests: list = field(default_factory=list)
    platform_errors: list = field(default_factory=list)
    evidence_corrupt: list = field(default_factory=list)
    unrecognized: list = field(default_factory=list)
    unevidenced_failures: list = field(default_factory=list)
    no_conclusion: list = field(default_factory=list)
    out_of_scope: list = field(default_factory=list)
    not_run_devices: list = field(default_factory=list)
    device_scope: list | None = None
    round_status: dict | None = None

    def as_record(self) -> dict:
        """机器可读的样子:字段与 round-status.json / scenario-*.json 同一风格。"""
        return {
            "verdict": self.verdict,
            "note": self.note,
            "scope": self.scope,
            "ran": self.ran,
            "round_status": self.round_status,
            "device_failures": self.device_failures,
            "invalid_tests": self.invalid_tests,
            "platform_errors": self.platform_errors,
            "evidence_corrupt": self.evidence_corrupt,
            "unrecognized": self.unrecognized,
            "unevidenced_failures": self.unevidenced_failures,
            "no_conclusion": self.no_conclusion,
            "out_of_scope_devices": self.out_of_scope,
            "not_run_devices": self.not_run_devices,
            "device_scope": self.device_scope,
        }


def attribute(directory: str | Path) -> Attribution:
    """读一个证据目录,给出四类归因。不认识的记录、坏行都单独报,不猜。"""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"不是一个证据目录: {directory}")

    device_failures: list[dict] = []
    invalid_tests: list[dict] = []
    platform_errors: list[dict] = []
    evidence_corrupt: list[dict] = []
    unrecognized: list[dict] = []
    no_conclusion: list[dict] = []
    ran_rows: set[tuple[str, str]] = set()
    ran_ids: set[str] = set()
    devices: list[str] = []
    no_reading_devices: list[str] = []

    for path in sorted(directory.glob("*.jsonl")):
        if path.name == PLATFORM_ERRORS_FILE:
            continue
        device = path.stem
        devices.append(device)
        records, readings = _read_device_file(
            path,
            device,
            device_failures=device_failures,
            invalid_tests=invalid_tests,
            no_conclusion=no_conclusion,
            corrupt=evidence_corrupt,
            unrecognized=unrecognized,
            ran_rows=ran_rows,
            ran_ids=ran_ids,
        )
        if records == 0:
            # 文件是空的,但“为什么空”要等范围读出来才知道(下面统一分类)。
            # “有帧但没读数”不算:周边设备本来就不产生读数。
            no_reading_devices.append(device)

    scenario_devices: set[str] = set()
    scenarios = _read_scenarios(
        directory,
        device_failures=device_failures,
        invalid_tests=invalid_tests,
        corrupt=evidence_corrupt,
        ran_ids=ran_ids,
        scenario_devices=scenario_devices,
    )
    _read_platform_errors(directory, platform_errors, evidence_corrupt)
    round_status = _read_round_status(directory, no_conclusion, evidence_corrupt, invalid_tests)
    scope, device_nodeids, executed_nodeids, unevidenced_failures = _read_scope(
        directory, no_conclusion, evidence_corrupt, invalid_tests, ran_ids
    )
    ran = {
        # 真正执行过的条数。None = 不知道(旧证据目录没有 executed):不许拿收集数减跳过数冒充。
        "executed": scope["executed"] if scope.get("available") else None,
        "cases": len(ran_rows) + scenarios,
        "ids": sorted(ran_ids),
        "devices": sorted(devices),
    }
    # 头条结论只需要"这一轮到底有没有跑到用例":执行条数已知就用它,不知道时退回证据计数
    # (证据里留下读数的那几条是下界,不会把没跑的算成跑过)。
    ran_cases = _ran_cases(ran)
    # 已经点出名字的失败不再列进“未落证据的失败”:那一栏说的是“pytest 红了、证据里没有结论”,
    # 同一条用例一边写着“哪一步不成立”、一边写着“没有结论”,读的人不知道该信哪一句。
    concluded = {
        entry["case"]["id"]
        for entry in device_failures
        if isinstance(entry.get("case"), dict) and entry["case"].get("id")
    }
    unevidenced_failures = [
        entry for entry in unevidenced_failures if entry.get("case") not in concluded
    ]
    device_scope = _device_scope(scope, scenario_devices)
    # 这台设备上真正跑过的用例的 nodeid —— 用来分"跑到了却没读数"与"没跑到"。
    failed_nodeids = {entry.get("nodeid") for entry in unevidenced_failures}
    out_of_scope = []
    not_run_devices = []
    for device in no_reading_devices:
        if device_scope is not None and device not in device_scope:
            out_of_scope.append(device)
            continue
        nodeids = device_nodeids.get(device, [])
        executed_here = [
            nodeid for nodeid in nodeids if executed_nodeids and nodeid in executed_nodeids
        ]
        # 这一轮一条用例都没执行,或它身上的用例一条都没跑到:读数无从判断,
        # 逐台列在大规模下只会刷屏 —— 收成一行计数(名单在 attribution.json)。
        # executed 未知(旧证据)时不收:那时"没跑到"本身就说不准。
        if executed_nodeids is not None and (
            ran_cases == 0 or (nodeids and not executed_here)
        ):
            not_run_devices.append(device)
            continue
        if any(nodeid in failed_nodeids for nodeid in executed_here):
            reason = _NO_CONCLUSION_REASONS["no_readings_case_failed"]
        elif executed_here:
            reason = _NO_CONCLUSION_REASONS["no_readings_case_silent"]
        else:
            # 执行条数未知 / 没有落在它身上的用例:这一档仍逐台列,但只说看得出的事。
            reason = _NO_CONCLUSION_REASONS["no_readings"]
        no_conclusion.append(
            {
                "kind": "no_readings",
                "device": device,
                "case_nodeids": nodeids,
                "executed_nodeids": executed_here,
                "ref": f"{device}.jsonl",
                "reason": reason,
            }
        )

    verdict = _verdict(
        device_failures,
        invalid_tests,
        platform_errors,
        evidence_corrupt,
        unevidenced_failures,
        ran_cases,
    )
    return Attribution(
        directory=directory,
        verdict=verdict,
        note=_VERDICT_NOTES[verdict],
        scope=scope,
        ran=ran,
        device_failures=device_failures,
        invalid_tests=invalid_tests,
        platform_errors=platform_errors,
        evidence_corrupt=evidence_corrupt,
        unrecognized=unrecognized,
        unevidenced_failures=unevidenced_failures,
        no_conclusion=no_conclusion,
        out_of_scope=out_of_scope,
        not_run_devices=not_run_devices,
        device_scope=sorted(device_scope) if device_scope is not None else None,
        round_status=round_status,
    )


def _read_device_file(
    path: Path,
    device: str,
    *,
    device_failures: list,
    invalid_tests: list,
    no_conclusion: list,
    corrupt: list,
    unrecognized: list,
    ran_rows: set,
    ran_ids: set,
) -> tuple[int, int]:
    """一台设备的 JSONL:读数越界进"设备不合格",link_lost 进"测试无效"。

    返回(记录数, 读条数)—— 记录数为 0 才能说"这一轮根本没碰过这台设备"。
    """
    records = 0
    readings = 0
    manual_entries: list[dict] = []
    for line_no, record, problem in _records(path):
        records += 1
        ref = f"{path.name}:{line_no}"
        if problem is not None:
            corrupt.append({"ref": ref, "reason": problem})
            continue
        kind = record["kind"]
        if kind == "reading":
            readings += 1
            case = record.get("case")
            case_id = case.get("id") if isinstance(case, dict) else None
            if case_id:
                ran_ids.add(case_id)
                ran_rows.add((device, case_id))
            evaluation = record.get("evaluation")
            if evaluation is None:
                # 有判据集却没求值结论:这不是"合格",是这条读数没有结论。
                if record.get("criteria") is not None:
                    no_conclusion.append(
                        {
                            "kind": "no_verdict",
                            "device": device,
                            "case": case,
                            "ref": ref,
                            "reason": _NO_CONCLUSION_REASONS["no_verdict"],
                        }
                    )
                continue
            for breach in evaluation.get("breaches") or ():
                if breach.get("severity") != "reject":
                    continue  # warn 是软信号:只记录与上报,不判不合格
                device_failures.append(_criterion_failure(breach, device, case, ref))
        elif kind == "link_lost":
            invalid_tests.append(
                {
                    "kind": "link_lost",
                    "device": record["device"],
                    "operation": record.get("operation"),
                    "failure": record.get("failure"),
                    "message": record.get("message"),
                    "at": record.get("at"),
                    "ref": ref,
                }
            )
        elif kind == "manual_step":
            # 人工步骤:不产生读数、也不判合格,但**不算通过**。
            # 它不进 QUIET_KINDS —— 只计入记录数就是静默,而那正是本票要防的损失。
            entry = {
                "kind": "manual_step",
                "device": device,
                "case": record.get("case"),
                "action": record.get("action"),
                "expect": record.get("expect"),
                "confirmed": record.get("confirmed"),
                "executed_steps": record.get("executed_steps"),
                "ref": ref,
            }
            no_conclusion.append(entry)
            manual_entries.append(entry)
        elif kind == "evidence_gap":
            # 写证据这一侧丢了读数:证据看着完整、实际少条,所以归「证据损坏」而不是
            # 「设备不合格」—— 设备没错,是写证据的游标追不上窗口。缺的条数照原样报出来。
            corrupt.append(
                {
                    "kind": "evidence_gap",
                    "device": device,
                    "missing": record.get("missing"),
                    "window": record.get("window"),
                    "reason": record.get("note"),
                    "ref": ref,
                }
            )
        elif kind == "step_failed":
            # 含人工步骤的条款先跑自动步骤:那几步没过就是设备不合格,
            # 不许被后面的“等人确认”盖成一条 skip(验收要求:失败不被 skip 掩盖)。
            device_failures.append(
                {
                    "kind": "step_failed",
                    "device": device,
                    "case": record.get("case"),
                    "quantity": record.get("key"),
                    "value": record.get("value"),
                    "expected": record.get("expected"),
                    "text": record.get("text"),
                    "ref": ref,
                }
            )
        elif kind not in QUIET_KINDS:
            # 能解析、只是归因还不认识这种记录:不算坏行(坏行的含义是解析不了)。
            # 汇总成一条提示,不影响结论 —— 以后加记录类型不用再改归因。
            unrecognized.append({"kind": kind, "device": device, "ref": ref})
    _count_pending(manual_entries)
    return records, readings


def _count_pending(entries: list[dict]) -> None:
    """同一条款在这台设备上留了几条人工占位 = 还有几步待确认。

    同一条款的占位前后相邻地写在一起,但按 case id 计数不依赖它们相邻:
    多台设备的文件各算各的,同一条款落在两台设备上时"还有 M 步"也是各自的。
    """
    pending: dict[str, int] = {}
    for entry in entries:
        case = entry.get("case")
        case_id = case.get("id") if isinstance(case, dict) else None
        if case_id:
            pending[case_id] = pending.get(case_id, 0) + 1
    for entry in entries:
        case = entry.get("case")
        case_id = case.get("id") if isinstance(case, dict) else None
        entry["pending_steps"] = pending.get(case_id, 1) if case_id else 1


def _criterion_failure(breach: dict, device: str, case: Any, ref: str) -> dict:
    """一条判据违约。形状与证据层的 breach 同构:等值违约没有边界,只写 expected;
    区间违约的字段名与顺序一字不动(消费方按字面读 limit / exceeded_by)。

    等值违约若写成 limit: null + exceeded_by: 0.0,机器会读成「没有边界、没有越界」,
    而结论是不合格 —— 字面对、读起来反了。不适用就不要占位。
    """
    if breach.get("expected") is not None:
        return {
            "kind": "equals",
            "device": device,
            "case": case,
            "quantity": breach.get("quantity"),
            "value": breach.get("value"),
            "expected": breach.get("expected"),
            "text": breach.get("text"),
            "ref": ref,
        }
    return {
        "kind": "criterion",
        "device": device,
        "case": case,
        "quantity": breach.get("quantity"),
        "value": breach.get("value"),
        "limit": breach.get("limit"),
        "exceeded_by": breach.get("exceeded_by"),
        "text": breach.get("text"),
        "ref": ref,
    }


def _read_scenarios(
    directory: Path,
    *,
    device_failures: list,
    invalid_tests: list,
    corrupt: list,
    ran_ids: set,
    scenario_devices: set,
) -> int:
    """每个场景一份结论:failed 是被测设备不合格,invalid 是测试无效,passed 是跑到了。

    返回值是这一轮场景的条数(算进"跑到多少条"那一行)。
    参与方顺手收进 scenario_devices:场景用例按设备参数化不了,它的设备范围只能从
    scenario-*.json 的 participants 读回来。
    """
    rows = 0
    for path in sorted(directory.glob(f"{SCENARIO_PREFIX}*.json")):
        data, problem = _load_json(path)
        if problem is not None:
            corrupt.append({"ref": path.name, "reason": problem})
            continue
        rows += 1
        for participant in data.get("participants") or ():
            if isinstance(participant, dict) and participant.get("id"):
                scenario_devices.add(participant["id"])
        name = data.get("scenario") or path.stem[len(SCENARIO_PREFIX) :]
        case = data.get("case")
        if isinstance(case, dict) and case.get("id"):
            ran_ids.add(case["id"])
        verdict = data.get("verdict")
        failed_steps = [step for step in data.get("steps") or () if not step.get("passed")]
        if verdict == "invalid":
            invalid_tests.append(
                {
                    "kind": "scenario_invalid",
                    "scenario": name,
                    "case": case,
                    "failed_steps": failed_steps,
                    "ref": f"{path.name}#steps",
                }
            )
        elif verdict == "failed":
            for index, step in enumerate(data.get("steps") or ()):
                if step.get("passed"):
                    continue
                device_failures.append(
                    {
                        "kind": "scenario_step",
                        "device": step.get("device"),
                        "action": step.get("action"),
                        "detail": step.get("detail") or {},
                        "case": case,
                        "ref": f"{path.name}#steps[{index}]",
                    }
                )
        elif verdict != "passed":
            corrupt.append(
                {"ref": path.name, "reason": f"场景结论 {verdict!r} 不是 passed / failed / invalid"}
            )
    return rows


def _read_platform_errors(directory: Path, platform_errors: list, corrupt: list) -> None:
    path = directory / PLATFORM_ERRORS_FILE
    if not path.exists():
        return
    for line_no, record, problem in _records(path):
        ref = f"{path.name}:{line_no}"
        if problem is not None:
            corrupt.append({"ref": ref, "reason": problem})
            continue
        platform_errors.append(
            {
                "device": record.get("device"),
                "case": record.get("case"),
                "nodeid": record.get("nodeid"),
                "error": record.get("error"),
                "message": record.get("message"),
                "at": record.get("at"),
                "ref": ref,
            }
        )


def _read_round_status(
    directory: Path, no_conclusion: list, corrupt: list, invalid_tests: list
) -> dict | None:
    path = directory / ROUND_STATUS_FILE
    if not path.exists():
        no_conclusion.append(
            {
                "kind": "no_round_status",
                "ref": ROUND_STATUS_FILE,
                "reason": _NO_CONCLUSION_REASONS["no_round_status"],
            }
        )
        return None
    data, problem = _load_json(path)
    if problem is not None:
        corrupt.append({"ref": ROUND_STATUS_FILE, "reason": problem})
        return None
    if data.get("verdict") == "needs-review" and not any(
        entry["kind"] == "link_lost" for entry in invalid_tests
    ):
        corrupt.append(
            {
                "ref": ROUND_STATUS_FILE,
                "reason": "round-status 说有断链,但设备证据里没有一条 link_lost 记录",
            }
        )
    return {"verdict": data.get("verdict"), "note": data.get("note")}


def _read_scope(
    directory: Path,
    no_conclusion: list,
    corrupt: list,
    invalid_tests: list,
    ran_ids: set,
) -> tuple[dict, dict, set | None, list]:
    """run-scope.json 是"没跑"与"真跑过"的唯一记录来源:没有它,被筛掉/被跳过的用例就无从得知。

    collected / deselected / skipped / executed / failed / errored 各是各的事实,
    不互相推算:executed 缺失就是"执行条数未知",不拿 collected − skipped 冒充。
    failed / errored 是一类特别的线索:用例在 setup 或 call 里崩了、一条读数都没写,
    证据里就完全看不到它 —— 归因必须从范围里把它找回来。它们仍是范围事实,不是判定。
    返回(范围, 设备 → 落在它身上的 nodeid, 执行过的 nodeid, 未落证据的失败)。
    执行过的 nodeid 另给一份:归因靠它分「跑到了却没读数」与「没跑到」,不把两者混成一句。
    """
    path = directory / RUN_SCOPE_FILE
    if not path.exists():
        no_conclusion.append(
            {
                "kind": "scope_missing",
                "ref": RUN_SCOPE_FILE,
                "reason": _NO_CONCLUSION_REASONS["scope_missing"],
            }
        )
        return (
            {
                "available": False,
                "reason": f"{RUN_SCOPE_FILE} 不存在:这一轮被筛掉 / 被跳过的用例无从得知",
            },
            {},
            None,
            [],
        )
    data, problem = _load_json(path)
    if problem is not None:
        corrupt.append({"ref": RUN_SCOPE_FILE, "reason": problem})
        return {"available": False, "reason": f"{RUN_SCOPE_FILE} 读不出来: {problem}"}, {}, None, []

    collected = data.get("collected") or ()
    # executed 是"真正执行过"的唯一来源。没有这个键 = 不知道(旧证据目录):
    # 这时不许拿 collected − skipped 推算 —— `pytest -x` 中途停下时那个数把没跑的算成跑了。
    executed_nodeids: set[str] | None = None
    if "executed" in data:
        if isinstance(data["executed"], list):
            executed_nodeids = set(data["executed"])
        else:
            corrupt.append(
                {"ref": f"{RUN_SCOPE_FILE}#executed", "reason": "executed 不是 nodeid 列表"}
            )
    skipped_nodeids: set[str] = set()
    deselected_nodeids: set[str] = set()
    scope_devices: set[str] = set()
    device_nodeids: dict[str, list[str]] = {}
    no_evidence: list[dict] = []
    for entry in collected:
        if isinstance(entry, dict):
            scope_devices.update(entry.get("devices") or ())
    for index, entry in enumerate(data.get("deselected") or ()):
        deselected_nodeids.add(entry.get("nodeid"))
        no_conclusion.append(
            {
                "kind": "deselected",
                "case": entry.get("case"),
                "nodeid": entry.get("nodeid"),
                "reason": entry.get("reason"),
                "ref": f"{RUN_SCOPE_FILE}#deselected[{index}]",
            }
        )
    for index, entry in enumerate(data.get("skipped") or ()):
        skipped_nodeids.add(entry.get("nodeid"))
        reason = entry.get("reason") or ""
        if reason.startswith(TEST_INVALID_PREFIX):
            # 周边设备断链 / 装配期周边设备故障:这一条用例没有有效结论,不是设备不合格。
            invalid_tests.append(
                {
                    "kind": "skipped_case",
                    "case": entry.get("case"),
                    "nodeid": entry.get("nodeid"),
                    "operation": _fault_operation(reason),
                    "message": reason[len(TEST_INVALID_PREFIX) :].strip(),
                    "ref": f"{RUN_SCOPE_FILE}#skipped[{index}]",
                }
            )
        else:
            no_conclusion.append(
                {
                    "kind": "skipped",
                    "case": entry.get("case"),
                    "nodeid": entry.get("nodeid"),
                    "reason": reason,
                    # 去掉设备 id 的原因:同一原因的 100 条跳过只差设备,显示时聚成一条。
                    "cause": _cause_of(reason, scope_devices),
                    "ref": f"{RUN_SCOPE_FILE}#skipped[{index}]",
                }
            )

    for entry in collected:
        if not isinstance(entry, dict):
            continue
        nodeid, case_id = entry.get("nodeid"), entry.get("case")
        for device in entry.get("devices") or ():
            device_nodeids.setdefault(device, []).append(nodeid)
        if not case_id or case_id in ran_ids:
            continue
        if nodeid in skipped_nodeids or nodeid in deselected_nodeids:
            continue
        no_evidence.append({"nodeid": nodeid, "case": case_id})

    if no_evidence:
        # 一条一条报会淡成噪音,而且从证据里本来就分不出"没跑到"与"这条用例不读数":
        # 所以聚合成一条(名单留在 attribution.json),但绝不静默 —— 它们不能算进"通过"。
        no_conclusion.append(
            {
                "kind": "not_run",
                "count": len(no_evidence),
                "items": no_evidence,
                "reason": _NO_CONCLUSION_REASONS["not_run"],
                "ref": f"{RUN_SCOPE_FILE}#collected",
            }
        )

    unevidenced = _unevidenced_failures(data, corrupt)

    return (
        {
            "available": True,
            "reason": None,
            "source": RUN_SCOPE_FILE,
            "collected": len(collected),
            "deselected": len(data.get("deselected") or ()),
            "skipped": len(data.get("skipped") or ()),
            # None = 旧证据目录没这个键:不知道,不是"没有失败"。
            "failed": None if "failed" not in data else len(data["failed"] or ()),
            "errored": None if "errored" not in data else len(data["errored"] or ()),
            "executed": None if executed_nodeids is None else len(executed_nodeids),
            # 收集了却连报告都没拿到的条数(中断 / -x 停下):按 nodeid 集合算,
            # 不用减法 —— 用例体内跳过的那条既在 skipped 里、也占过 call 报告。
            "not_reached": None
            if executed_nodeids is None
            else len(
                {
                    entry.get("nodeid")
                    for entry in collected
                    if isinstance(entry, dict)
                }
                - skipped_nodeids
                - executed_nodeids
            ),
            "devices": sorted(scope_devices),
        },
        device_nodeids,
        executed_nodeids,
        unevidenced,
    )


def _unevidenced_failures(data: dict, corrupt: list) -> list:
    """范围里记下的失败:nodeid + 用例身份 + 一行原因。

    pytest 报了失败、证据里却没有结论 —— 两件事对不上时,能说的只有范围事实;
    为什么失败、设备合不合格,证据里没有,不许编。
    """
    entries: list[dict] = []
    for key in ("failed", "errored"):
        if key not in data:
            continue  # 旧证据目录没有这一类:不知道,不是"没有"
        records = data[key]
        if not isinstance(records, list):
            corrupt.append({"ref": f"{RUN_SCOPE_FILE}#{key}", "reason": f"{key} 不是列表"})
            continue
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                corrupt.append(
                    {"ref": f"{RUN_SCOPE_FILE}#{key}[{index}]", "reason": f"{key} 的条目不是对象"}
                )
                continue
            entries.append(
                {
                    "kind": key,
                    "nodeid": record.get("nodeid"),
                    "case": record.get("case"),
                    "reason": record.get("reason"),
                    "ref": f"{RUN_SCOPE_FILE}#{key}[{index}]",
                }
            )
    return entries


def _cause_of(reason: str, device_ids: set[str]) -> str:
    """把原因开头的设备 id 抹掉,当聚合键用:100 台设备因同一原因跳过只该占一行。

    只抹开头那一个:原因按 ``f"{device_id}(型号 ...)"`` 拼,而 pytest 运行期跳过又在前面加
    ``Skipped: ``。正文里的 id 不动 —— 那里是"已配置的设备: [...]" 的列举,抹掉就只剩一串空引号。
    """
    prefix = "Skipped: " if reason.startswith("Skipped: ") else ""
    body = reason[len(prefix) :]
    for device_id in sorted(device_ids, key=len, reverse=True):
        if body.startswith(device_id):
            body = body[len(device_id) :]
            break
    return " ".join(f"{prefix}{body}".split())


def _device_scope(scope: dict, scenario_devices: set[str]) -> set[str] | None:
    """本轮的设备范围:按设备参数化的用例带上收集期落下的 devices,场景用例带上参与方。

    两个来源都交不出设备时说"不知道"(None),不猜:老证据目录、以及直接用
    composition_root 取设备的集成用例都属于这种。这一档下空文件仍逐台列出 ——
    宁可留着噪声,也不能把空读数设备说成"不在范围"。
    """
    if not scope.get("available"):
        return None
    declared = set(scope.get("devices") or ()) | scenario_devices
    return declared or None


def _is_connect_fault(reason: str) -> bool:
    """装配期周边设备故障("...连接失败...")与收帧阶段断链("...收帧阶段断链...")要分开:
    下一步该查的是线接没接上,还是跑到一半掉的线。"""
    return "连接失败" in reason


def _fault_operation(reason: str) -> str:
    """skip 理由说的是哪种周边设备故障。三种排障方向不同:接线 / 查线 / 查复位命令。"""
    if _is_connect_fault(reason):
        return "connect"
    if "恢复初始设定失败" in reason:
        return SETPOINT_RESTORE_OPERATION
    return "receive"


def _operation_text(operation: Any) -> str:
    """一条“测试无效”到底是哪一步不成立。断链与台架复位失败不能写成同一句。"""
    if operation == SETPOINT_RESTORE_OPERATION:
        return "恢复初始设定失败"
    if operation == "connect":
        return "连接失败"
    return f"在 {operation} 时断链"


def _verdict(
    device_failures: list,
    invalid_tests: list,
    platform_errors: list,
    evidence_corrupt: list,
    unevidenced_failures: list,
    ran_cases: int,
) -> str:
    """头条结论。各类各自独立,谁也不并进谁;这里只决定先喊哪一句。

    未落证据的失败排在各具体结论之后、incomplete 之前:有设备不合格时先说那个,
    只剩它自己的时候也不许说 clean —— pytest 退出码非 0 而归因 clean 正是这条票要治的缺陷。
    """
    if platform_errors:
        return "platform-error"
    if evidence_corrupt:
        return "evidence-corrupt"
    if device_failures:
        return "failed"
    if invalid_tests:
        return "invalid"
    if unevidenced_failures:
        return "failed-without-evidence"
    if ran_cases == 0:
        return "incomplete"
    return "clean"


def _records(path: Path) -> Iterator[tuple[int, dict | None, str | None]]:
    """读一份 JSONL,逐行给出 (行号, 记录, 问题)。坏行不算失败,按 ref 单独报。"""
    text = path.read_text(encoding="utf-8")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            yield line_no, None, f"不是合法 JSON: {exc.msg}"
            continue
        if not isinstance(record, dict) or "kind" not in record or "device" not in record:
            yield line_no, None, "缺少 kind / device 字段"
            continue
        yield line_no, record, None


def _load_json(path: Path) -> tuple[dict, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, f"不是合法 JSON: {exc.msg}"
    if not isinstance(data, dict):
        return {}, "顶层不是对象"
    return data, None


def write(directory: str | Path, attribution: Attribution) -> Path:
    """把归因写成机器可读的那一份:evidence/<会话>/attribution.json。"""
    path = Path(directory) / ATTRIBUTION_FILE
    path.write_text(
        json.dumps(attribution.as_record(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def render(attribution: Attribution) -> list[str]:
    """人类可读的那一份:分节列出四类,每一条都带证据出处。"""
    lines = [
        f"一轮归因: {attribution.directory}",
        f"结论: {attribution.verdict} —— {attribution.note}",
        "",
    ]
    if not (
        attribution.device_failures
        or attribution.invalid_tests
        or attribution.platform_errors
        or attribution.evidence_corrupt
        or attribution.unevidenced_failures
    ):
        lines.append("没有失败项:0 条设备不合格 / 0 条测试无效 / 0 条平台错误 / 0 条证据损坏。")
        lines.append(
            f"这不是 0 条用例:这一轮{_executed_phrase(attribution.ran)}(见下方「这一轮的规模」)。"
        )
        lines.append("")
    lines += _render_device_failures(attribution.device_failures)
    lines += _render_unevidenced(attribution.unevidenced_failures)
    lines += _render_invalid_tests(attribution.invalid_tests)
    lines += _render_platform_errors(attribution.platform_errors)
    lines += _render_corrupt(attribution.evidence_corrupt)
    lines += _render_unrecognized(attribution.unrecognized)
    lines += _render_no_conclusion(attribution)
    lines += _render_scale(attribution)
    lines += _render_pending_manual(attribution)
    return lines


def _render_device_failures(entries: list) -> list[str]:
    lines = [f"[设备不合格] {len(entries)} 条"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        if entry["kind"] in ("criterion", "equals", "step_failed"):
            lines.append(
                f"  {entry['device']}  {_breach_text(entry)}"
                f"  {_case_text(entry.get('case'))}  证据 {entry['ref']}"
            )
        else:
            detail = entry.get("detail") or {}
            lines.append(
                f"  {entry['device']}  场景步骤「{entry.get('action')}」不成立"
                f"  {detail or ''}  {_case_text(entry.get('case'))}  证据 {entry['ref']}"
            )
    return lines + [""]


def _breach_text(entry: dict) -> str:
    """等值违约没有边界字段:证据里没带 text 时也要读得出期望值与实测值。"""
    if entry.get("text"):
        return entry["text"]
    if "expected" in entry:
        return f"{entry['quantity']} 期望 {entry['expected']},实测 {entry['value']}"
    return entry["quantity"]


def _render_unevidenced(entries: list) -> list[str]:
    """pytest 报了失败、证据里却没有结论:这一类既不是设备不合格,更不是通过。

    它单独成栏的理由很实际:失败发生在写证据之前(setup 里崩了、或用例读了设备之前就挂了),
    证据里一条读数都没有 —— 排障最想知道的就是这个位置。
    """
    lines = [
        f"[未落证据的失败] {len(entries)} 条"
        "(pytest 报了失败但证据里没有结论,说明失败发生在写证据之前:"
        "不算通过,也不许当设备不合格)"
    ]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        lines.append(
            f"  {entry['kind']}  {_case_text(entry.get('case'))}"
            f"  nodeid {entry.get('nodeid')}  原因 {entry.get('reason')}  证据 {entry['ref']}"
        )
    return lines + [""]


def _render_invalid_tests(entries: list) -> list[str]:
    lines = [f"[测试无效] {len(entries)} 条(台架:断链 / 复位失败,不是设备不合格)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        if entry["kind"] == "link_lost":
            lines.append(
                f"  {entry['device']}  {_operation_text(entry.get('operation'))}"
                f"({entry.get('failure')}): {entry.get('message')}"
                f"  时间 {entry.get('at')}  证据 {entry['ref']}"
            )
        elif entry["kind"] == "skipped_case":
            lines.append(
                f"  {_case_text(entry.get('case'))}  {_operation_text(entry.get('operation'))}"
                f"  原因 {entry.get('message')}  证据 {entry['ref']}"
            )
        else:
            steps = ", ".join(
                f"{step.get('device')}「{step.get('action')}」" for step in entry["failed_steps"]
            )
            lines.append(
                f"  场景 {entry['scenario']}  判 invalid(只有周边设备的步骤不成立)"
                f"  涉及 {steps or '无'}  {_case_text(entry.get('case'))}  证据 {entry['ref']}"
            )
    return lines + [""]


def _render_platform_errors(entries: list) -> list[str]:
    lines = [f"[平台错误] {len(entries)} 条(聚合根不变量被违反:平台自身有 bug)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        lines.append(
            f"  设备 {entry.get('device') or '未标注(异常没带 device_id)'}"
            f"  {entry.get('error')}: {entry.get('message')}"
            f"  {_case_text(entry.get('case'))}  nodeid {entry.get('nodeid')}"
            f"  证据 {entry['ref']}"
        )
    return lines + [""]


def _render_corrupt(entries: list) -> list[str]:
    lines = [f"[证据损坏] {len(entries)} 条(责任方是写证据这一侧,不算设备不合格)"]
    if not entries:
        return lines + ["  无", ""]
    for entry in entries:
        if entry.get("missing") is None:
            lines.append(f"  {entry['ref']}  {entry['reason']}")
        else:
            # 缺几条是这个分节最要紧的信息:漏 5 条与漏 5000 条差三个数量级。
            lines.append(
                f"  {entry['device']}  缺 {entry['missing']} 条读数: {entry['reason']}"
                f"  证据 {entry['ref']}"
            )
    return lines + [""]


def _render_unrecognized(entries: list) -> list[str]:
    """能解析、只是归因还不认识这种记录:汇总成一条提示,不进结论也不说成“证据坏了”。

    这一档存在的原因很实际:把“证据里多了一种记录”判成 corrupt,会让人去查写入层,
    而错的其实是归因自己没见过它。每类给一个计数 + 前三个出处,足够找到它。
    """
    if not entries:
        return []
    kinds: dict[str, list] = {}
    for entry in entries:
        kinds.setdefault(entry["kind"], []).append(entry["ref"])
    lines = [
        f"[未识别的记录] {len(kinds)} 类 × {len(entries)} 条"
        "(归因还不认识这些 kind:它们不影响结论,也不算证据损坏)"
    ]
    for kind in sorted(kinds):
        refs = kinds[kind]
        shown = ", ".join(refs[:3]) + (f" 等 {len(refs)} 条" if len(refs) > 3 else "")
        lines.append(f"  {kind} × {len(refs)}  出处 {shown}")
    return lines + [""]


def _render_no_conclusion(attribution: Attribution) -> list[str]:
    entries = attribution.no_conclusion
    lines = [f"[没有有效结论] {len(entries)} 条(跳过 / 筛掉 / 没跑到 / 证据判断不了 —— 都不算通过)"]
    if attribution.out_of_scope:
        # 不在本轮范围是正常,只给计数:逐条列在大规模下会把真问题浸掉。名单在 attribution.json。
        lines.append(
            f"  另有 {len(attribution.out_of_scope)} 台设备不在本轮范围内"
            "(没跑到,也不在本轮范围里;名单见 attribution.json)"
        )
    if attribution.not_run_devices:
        # 两条都是“没跑到”,但读者的下一步不同:一条用例都没执行时先查 run-scope,
        # 部分没跑到时只需要知道有多少台被漏下了 —— 逐台列在大规模下就是刷屏。
        count = len(attribution.not_run_devices)
        if _ran_cases(attribution.ran) == 0:
            lines.append(
                f"  这一轮没有执行任何用例,{count} 台设备的读数无从判断"
                "(见 run-scope 的 skipped / deselected;名单见 attribution.json)"
            )
        else:
            lines.append(
                f"  另有 {count} 台设备的用例没跑到"
                "(见 run-scope 的 skipped / not_reached;名单见 attribution.json)"
            )
    if not entries:
        if attribution.out_of_scope or attribution.not_run_devices:
            return lines + [""]
        return lines + ["  无", ""]
    for entry in entries:
        if entry["kind"] == "skipped":
            continue  # 跳过另走聚合:同一原因只占一行
        if entry["kind"] == "no_readings":
            # 只说证据里看得到的事:设备在范围内、这份证据里没有读数。为什么没有,下一步自己核。
            executed = entry.get("executed_nodeids") or ()
            if executed:
                detail = (
                    f"  跑到的用例 {', '.join(executed)}"
                    f"  证据 {entry['ref']} 与 {RUN_SCOPE_FILE}#executed"
                )
            else:
                nodeids = ", ".join(entry.get("case_nodeids") or ()) or (
                    "无(run-scope 里没有落在它身上的用例)"
                )
                detail = f"  这一轮的用例 {nodeids}  证据 {entry['ref']} 与 {RUN_SCOPE_FILE}#collected"
            lines.append(f"  设备 {entry['device']}  {entry['reason']}{detail}")
        elif entry["kind"] == "manual_step":
            confirmed = entry.get("confirmed")
            lines.append(
                f"  待人工确认  设备 {entry['device']}  「{entry.get('action')}」"
                f"  按 {entry.get('expect')} 确认"
                f"  confirmed={'未回填' if confirmed is None else confirmed}"
                f"  {_executed_text(entry)}"
                f"  {_case_text(entry.get('case'))}  证据 {entry['ref']}"
            )
        elif entry["kind"] in ("no_round_status", "scope_missing"):
            lines.append(f"  {entry['reason']}  证据 {entry['ref']}")
        elif entry["kind"] == "no_verdict":
            lines.append(
                f"  设备 {entry['device']}  {entry['reason']}  {_case_text(entry.get('case'))}"
                f"  证据 {entry['ref']}"
            )
        elif entry["kind"] == "not_run":
            nodeids = ", ".join(item["nodeid"] for item in entry["items"][:3])
            more = "" if entry["count"] <= 3 else f" 等 {entry['count']} 条(完整名单见 attribution.json)"
            lines.append(
                f"  {entry['count']} 条被收集的用例在证据里没有读数"
                f"(过没过看 pytest 报告,不从这里推断): {nodeids}{more}  证据 {entry['ref']}"
            )
        else:
            label = _NO_CONCLUSION_REASONS[entry["kind"]]
            lines.append(
                f"  {label}  {_case_text(entry.get('case')) or entry.get('nodeid')}"
                f"  原因 {entry.get('reason')}  证据 {entry['ref']}"
            )
    lines += _render_skips([entry for entry in entries if entry["kind"] == "skipped"])
    return lines + [""]


_SKIP_CAUSES_SHOWN = 3


def _render_skips(entries: list) -> list[str]:
    """同一原因只列一条 +(×N),只展开前 3 个不同原因。

    规模清单下 100 条只差设备 id 的跳过逐条列(实测最长一行 4904 字)会把真正的失败挤出屏幕;
    完整名单一直在 attribution.json 的 no_conclusion 里,想逐条核对就去那里。
    """
    groups: dict[str, list] = {}
    for entry in entries:
        cause = entry.get("cause") or entry.get("reason") or "跳过"
        groups.setdefault(cause, []).append(entry)
    rendered = list(groups.items())
    lines = []
    for cause, items in rendered[:_SKIP_CAUSES_SHOWN]:
        first = items[0]
        cases = {item.get("case") for item in items}
        case_text = (
            _case_text(first.get("case")) if len(cases) == 1 else f"{len(cases)} 条不同用例"
        )
        times = "" if len(items) == 1 else f"(×{len(items)})"
        lines.append(f"  被跳过  {case_text}{times}  原因 {cause}  证据 {first['ref']}")
    if len(rendered) > _SKIP_CAUSES_SHOWN:
        rest = rendered[_SKIP_CAUSES_SHOWN:]
        lines.append(
            f"  另有 {len(rest)} 个不同原因的跳过,共 {sum(len(items) for _, items in rest)} 条"
            "(完整名单见 attribution.json)"
        )
    return lines


def _ran_cases(ran: dict) -> int:
    """这一轮到底跑到了几条用例:executed 已知就用它,未知时退回证据里的读数条数(下界)。"""
    return ran["executed"] if ran["executed"] is not None else ran["cases"]


def _executed_phrase(ran: dict) -> str:
    """"这一轮执行了多少条"的两种说法:知道就报数,不知道就说不知道。"""
    if ran["executed"] is None:
        return f"的执行条数未知,证据里留下读数的是 {ran['cases']} 条"
    return f"执行了 {ran['executed']} 条"


def _render_scale(attribution: Attribution) -> list[str]:
    """这一轮到底跑了多少条 —— 全绿时也必须看得见,否则"0 条失败"会被读成"0 条用例"。

    头条的"执行了 N 条"只从 run-scope.json 的 executed 来:`pytest -x` 中途停下、
    会话被打断时,没跑到的用例不能算进去。executed 缺了就说"执行条数未知",
    不拿收集数减跳过数冒充。
    """
    scope = attribution.scope
    if scope.get("available"):
        if scope["executed"] is None:
            head = (
                f"[这一轮的规模] 执行条数未知({RUN_SCOPE_FILE} 里没有 executed:旧证据目录,"
                f"或写范围时被打断);收集 {scope['collected']} 条 / 筛掉 {scope['deselected']} 条"
                f" / 跳过 {scope['skipped']} 条"
            )
        else:
            head = (
                f"[这一轮的规模] 执行了 {scope['executed']} 条"
                f"(收集 {scope['collected']} 条;其中跳过 {scope['skipped']} 条;"
                f"还没跑到 {scope['not_reached']} 条)"
            )
        return [
            head,
            f"  另有 {scope['deselected']} 条被 --select / 适用矩阵筛掉(不在收集里,没跑)",
            f"  其中证据里留下读数的: {attribution.ran['cases']} 条(设备 × 用例身份)",
            f"  涉及设备: {_devices_brief(attribution.ran['devices'])}",
            _device_scope_line(attribution.device_scope),
            f"  范围来源: {scope['source']}",
        ]
    return [
        f"[这一轮的规模] 执行条数未知(没有 {RUN_SCOPE_FILE}:这一轮收集了多少、执行了多少都无从得知)",
        f"  证据里留下读数的: {attribution.ran['cases']} 条(设备 × 用例身份)",
        f"  涉及设备: {_devices_brief(attribution.ran['devices'])}",
        _device_scope_line(attribution.device_scope),
        f"  范围缺失: {scope.get('reason')}",
    ]


def _executed_text(entry: dict) -> str:
    """停在这一步之前已经跑掉的那几步。

    跑到这一步才停,就证明前面那几步全过了(失败会先把这条条款判 failed,不会有占位);
    一步没跑(人工步骤在最前面)也要写出来 —— “停在这里”与“一步没跑”是两件事。
    """
    executed = entry.get("executed_steps")
    pending = entry.get("pending_steps")
    if executed is None:
        return "已执行的自动步骤数未知(旧证据:占位里没有 executed_steps)"
    return f"已执行 {executed} 个自动步骤且全部通过,还有 {pending or 1} 步待人工确认"


def _render_pending_manual(attribution: Attribution) -> list[str]:
    """规模行里单列“待人工确认 N 条”。

    不单列的话它会藏在数十条“被跳过”里:规模清单下人工步骤能占到四分之一,
    “这一轮全绿”就会被读成“全都测过了”。
    """
    count = sum(1 for entry in attribution.no_conclusion if entry["kind"] == "manual_step")
    if not count:
        return []
    return [
        f"  待人工确认 {count} 条(人工操作步骤:不算通过也不算失败,归在「没有有效结论」)"
    ]


def _devices_brief(devices) -> str:
    """一行里最多列几台设备,其余收成计数。

    规模清单 265 台时,把 263 个 id 铺在同一行和逐条列 263 行是同一个病:
    该收成计数的地方逐条列。完整名单在 attribution.json 里。
    """
    devices = list(devices)
    if not devices:
        return "无"
    if len(devices) <= 6:
        return ", ".join(devices)
    return f"{', '.join(devices[:5])} … 共 {len(devices)} 台(名单见 attribution.json)"


def _device_scope_line(device_scope: list | None) -> str:
    """设备范围从哪来 —— 读报告的人得知道哪些设备算"本轮该测的"。"""
    if device_scope is None:
        return "  设备范围: 未知(范围里没有设备信息):空文件仍逐台列出,不按范围外处理"
    return (
        f"  设备范围: {len(device_scope)} 台({', '.join(device_scope)})"
        " —— 按设备参数化的用例取 run-scope 的 devices,场景用例取 scenario-*.json 的参与方"
    )


def _case_text(case: Any) -> str:
    """用例身份有两种来源:证据里的 {id, clause, tags},和 run-scope 里的纯编号。"""
    if isinstance(case, str):
        return f"用例 {case}(条款见 run-scope / 证据)"
    if not isinstance(case, dict) or not case.get("id"):
        return "未声明用例身份"
    return f"用例 {case['id']}({case.get('clause') or '无条款'})"
