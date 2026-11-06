"""归因的人类可读渲染与 attribution.json 写出。"""
from __future__ import annotations

import json
from pathlib import Path

from testplatform.reporting.evidence import json_safe

from .core import *  # noqa: F401,F403


def write(directory: str | Path, attribution: Attribution) -> Path:
    """把归因写成机器可读的那一份:evidence/<会话>/attribution.json。"""
    path = Path(directory) / ATTRIBUTION_FILE
    path.write_text(
        json.dumps(
            json_safe(attribution.as_record()),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
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
