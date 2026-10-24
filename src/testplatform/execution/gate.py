"""
CI 门禁:一批 `summary.json` → **一个退出码 + 一行理由**。

GitHub 的 job 结论只有红绿两态,而我们的报告有四档(合格 / 不合格 / 测试无效 / 未完成)。
这一层只做一件事:把一批报告压成一个红绿答案,并把"为什么红"写成一句人话 ——
判决口径据此只存在于一处,CI 的 YAML 里不再各写一遍 if。

优先级(先命中先算;理由按同一顺序**全部列出**,混档不互相掩盖):

    1 不合格      有被测设备不合格(含产品自己退出)
    2 测试无效    判定被环境吞掉(断链 / 台架故障)—— 平台问题,不是产品
    3 未完成      申报外未测 / 设备级 0/0
    4 平台错误    产品起不来 / 任务配置错(reason=platform_error,报告里落未完成)
    5 没有任务    0 份报告 —— 没跑不等于通过
    6 没有验收    这一批只有冒烟任务 —— 冒烟不验设备,等于没有一条验收在跑
    0 合格        全部验收任务都合格

第 6 档是第 5 档的孪生:一个是"一份报告都没有",一个是"报告有,但没有一份是验收"。
两者都不许绿 —— 全标 smoke 就是让 CI 永远绿的那条后门。

`kind: smoke` 的任务不参与验收门禁(它验的是通路,不是设备),但**必须留痕**:
输出里单列一行"另有 N 个冒烟任务(不参与验收)";其中非合格的那些**还要单独一行点名**
("其中 M 个冒烟任务是失败的: …")。只列名单不够 —— 把红任务标成 smoke 是唯一能绕过验收的
后门,它必须在输出里以"失败"字样出现,看着不像正常任务。这不影响退出码,只是让它不可能被静默。

这一层是**纯读**:输入是报告文件,输出是退出码与一行字;它不产报告、不碰证据、不起进程。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

GATE_OK = 0
GATE_FAILED = 1
GATE_INVALID = 2
GATE_INCOMPLETE = 3
GATE_PLATFORM_ERROR = 4
GATE_NO_TASKS = 5
GATE_NO_ACCEPTANCE = 6

# 报告的 wire 契约(见 reporting/report.py::Report.as_record):这里只认字段值,不 import 它 ——
# 门禁读报告,报告不认识门禁;两边都不许反向依赖。
VERDICT_FAILED = "failed"
VERDICT_INVALID = "invalid"
VERDICT_INCOMPLETE = "incomplete"
VERDICT_QUALIFIED = "clean"
REASON_PLATFORM_ERROR = "platform_error"
KIND_SMOKE = "smoke"

SUMMARY_FILE = "summary.json"
DEFAULT_REPORTS_DIR = "reports"

CLASS_FAILED = "failed"
CLASS_INVALID = "invalid"
CLASS_INCOMPLETE = "incomplete"
CLASS_PLATFORM_ERROR = "platform_error"

# 判据表:顺序 = 退出码优先级。删掉/改掉任何一行,对应的用例必须红 —— 这张表就是门禁本身。
RULES = (
    (CLASS_FAILED, GATE_FAILED, "不合格"),
    (CLASS_INVALID, GATE_INVALID, "测试无效"),
    (CLASS_INCOMPLETE, GATE_INCOMPLETE, "未完成"),
    (CLASS_PLATFORM_ERROR, GATE_PLATFORM_ERROR, "平台错误"),
)

_TITLE = {name: title for name, _code, title in RULES}

_HINT = {
    CLASS_FAILED: "",
    CLASS_INVALID: " —— 平台问题,不是产品",
    CLASS_INCOMPLETE: " —— 申报外未测 / 设备级没验到",
    CLASS_PLATFORM_ERROR: " —— 平台问题(产品起不来 / 任务配置错),不是产品不合格",
}


class GateInputError(ValueError):
    """输入本身有问题(文件读不了 / 不是 JSON 对象):平台侧问题,不是产品不合格。"""


@dataclass(frozen=True)
class Decision:
    """门禁结论。`reason` 是一行理由;`details` 是必须单独成行的补充(smoke 留痕)。"""

    exit_code: int
    reason: str
    details: tuple[str, ...] = ()


def judge(summaries: Sequence[Mapping[str, Any]]) -> Decision:
    """把一批 summary.json 的**内容**压成一个结论。空输入 = 红(没跑过不等于通过)。"""
    if not summaries:
        return Decision(
            GATE_NO_TASKS,
            "没有任何任务被跑过:这一批里 0 份报告(没跑不等于通过)",
        )

    smoke: list[str] = []
    smoke_failed: list[tuple[str, str]] = []
    acceptance: dict[str, list[str]] = {name: [] for name, _code, _title in RULES}
    clean: list[str] = []

    for summary in summaries:
        name = str(summary.get("task") or "<未命名任务>")
        gate_class = _class_of(summary)
        if str(summary.get("kind") or "") == KIND_SMOKE:
            smoke.append(name)
            if gate_class is not None:
                # 冒烟任务不参与门禁,但它的非合格结论必须被点名(带上是哪一档)。
                smoke_failed.append((name, gate_class))
            continue
        if gate_class is None:
            clean.append(name)
        else:
            acceptance[gate_class].append(name)

    details = _details_of(smoke, smoke_failed)
    if not clean and not any(acceptance.values()):
        # 有报告,但一份验收任务都没有:冒烟不验设备,所以这一批什么都没验收。
        # 和"0 份报告"分开报(6 vs 5):一个是没跑,一个是跑了但没验。
        return Decision(
            GATE_NO_ACCEPTANCE,
            "没有一条验收任务在跑:这一批只有冒烟任务(冒烟不验设备)",
            details,
        )
    return Decision(_exit_of(acceptance), _reason_of(acceptance, clean), details)


def load_summaries(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """读一批报告:给目录就取 `<目录>/<任务>/summary.json`,给文件就直接读。

    读不出来直接抛 GateInputError —— 坏掉的报告不能被当成"没这条"糊过去:
    那会让一轮本来就跑失败的事悄悄变成合格。
    """
    loaded: list[dict[str, Any]] = []
    for path in _summary_files(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GateInputError(f"报告读不了: {path}({exc})") from exc
        if not isinstance(payload, dict):
            raise GateInputError(f"报告不是 JSON 对象: {path}")
        loaded.append(payload)
    return loaded


def main(argv: list[str] | None = None) -> int:
    """命令行入口:打印一行理由(冒烟任务另起一行),返回退出码。"""
    parser = argparse.ArgumentParser(
        description="CI 门禁:一批 summary.json → 一个退出码",
        epilog="\n".join(
            [
                "退出码(半年后仍然要能一眼看懂):",
                "  0  合格:全部验收任务都合格",
                "  1  不合格:任一验收任务判了不合格",
                "  2  测试无效:判定被环境吞掉 —— 平台问题,不是产品",
                "  3  未完成:申报外未测 / 设备级没验到",
                "  4  平台错误:产品起不来 / 任务配置错(报告里 verdict 仍是未完成,门禁单独映射)",
                "  5  没跑过任务:0 份报告 —— 没跑不等于通过",
                "  6  没有验收任务:这一批只有冒烟任务 —— 冒烟不验设备,等于没验收",
                "",
                "kind: smoke 的任务不参与门禁,但会在输出里单列(非合格的点名)",
            ]
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "reports",
        nargs="*",
        help=f"报告目录(取其中 */summary.json)或 summary.json 文件;省略时取 {DEFAULT_REPORTS_DIR}/",
    )
    arguments = parser.parse_args(argv)
    targets = arguments.reports or [DEFAULT_REPORTS_DIR]

    try:
        decision = judge(load_summaries(targets))
    except GateInputError as exc:
        print(f"平台错误:{exc}")
        return GATE_PLATFORM_ERROR

    print(decision.reason)
    for line in decision.details:
        print(line)
    return decision.exit_code


# ---------------------------------------------------------------- 内部 ----


def _class_of(summary: Mapping[str, Any]) -> str | None:
    """这份报告落在门禁的哪一类(None = 合格)。不认识的值按平台错误算。"""
    reason = str(summary.get("reason") or "")
    verdict = str(summary.get("verdict") or "")
    if reason == REASON_PLATFORM_ERROR:
        return CLASS_PLATFORM_ERROR
    if verdict == VERDICT_FAILED:
        return CLASS_FAILED
    if verdict == VERDICT_INVALID:
        return CLASS_INVALID
    if verdict == VERDICT_INCOMPLETE:
        return CLASS_INCOMPLETE
    if verdict == VERDICT_QUALIFIED:
        return None
    # 报告里的 verdict 不认识 = 报告这一层出了问题:按平台错误红,不许当成合格。
    return CLASS_PLATFORM_ERROR


def _exit_of(acceptance: Mapping[str, list[str]]) -> int:
    for name, code, _title in RULES:
        if acceptance[name]:
            return code
    return GATE_OK


def _details_of(
    smoke: Sequence[str], smoke_failed: Sequence[tuple[str, str]]
) -> tuple[str, ...]:
    """smoke 的两行留痕。第二行只在真有非合格冒烟任务时出现 —— 让它无法被静默。"""
    lines: list[str] = []
    if smoke:
        lines.append(f"另有 {len(smoke)} 个冒烟任务(不参与验收): {'、'.join(sorted(smoke))}")
    if smoke_failed:
        named = "、".join(
            f"{name}({_TITLE[gate_class]})" for name, gate_class in sorted(smoke_failed)
        )
        lines.append(f"其中 {len(smoke_failed)} 个冒烟任务是失败的: {named}")
    return tuple(lines)


def _reason_of(acceptance: Mapping[str, list[str]], clean: Sequence[str]) -> str:
    parts = []
    for name, _code, title in RULES:
        tasks = acceptance[name]
        if not tasks:
            continue
        listed = "、".join(sorted(tasks))
        parts.append(f"{title} {len(tasks)} 个({listed}){_HINT[name]}")
    if not parts:
        return f"全部合格:{len(clean)} 个验收任务"
    return ";".join(parts)


def _summary_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob(f"*/{SUMMARY_FILE}")))
        elif path.is_file():
            files.append(path)
        else:
            raise GateInputError(f"没有这个报告路径: {path}")
    return files
