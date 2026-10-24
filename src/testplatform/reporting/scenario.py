"""
场景记录 —— 一次跨设备场景跑完,另留一份"这个场景整体过没过"的证据。

per-device 的 JSONL 回答"这台设备这一轮读了什么、判据结论如何",回答不了"这条场景过没过":
单台设备合格不代表场景通过,反过来也一样。所以场景结论由参与方各步骤的实际求值结果算出来,
写进证据会话目录的 scenario-<场景名>.json,字段 verdict / note 与 round-status.json 同名同义。

三种结论,跟着 role 的语义走:
    passed   每一步都成立
    failed   有被测设备的步骤不成立 —— 设备不合格
    invalid  只有周边设备的步骤不成立 —— 测试无效,不是被测设备不合格

结论不能由用例里一个手写的 True 得出:每一步的 passed 必须是求值结果(判据集的 passed、
设定值与设备实际回报的比较),这里只负责把它们汇起来。

收尾(cleanup)也在这里落盘:用例自己声明“这条用例动过什么、要改回哪里去”
(退出工厂模式、把改过的参数写回原值),平台不替它猜。收尾在正常与异常两条路径
都要执行,结果进同一份 scenario-*.json 的 cleanup 字段。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from testplatform.config.manifest import SUPPORT_ROLE

from .evidence import EvidenceSession

_VERDICT_NOTES = {
    "passed": "参与方的每一步都成立:周边设备按设定输出,被测设备的读数满足判据集",
    "failed": "有被测设备的步骤不成立:设备不合格(单台设备合格不等于场景通过)",
    "invalid": "只有周边设备的步骤不成立:测试无效,不是被测设备不合格",
}


@dataclass(frozen=True)
class ScenarioStep:
    """场景里的一步:谁做的、做了什么、结果如何。passed 必须是实际求值的结果。

    device 为 None 的是 settle 那一步:没有设备参与,也就没有设备判定,它不进结论(见 verdict_of)。
    """

    device: str | None
    action: str
    passed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)


SETTLE_LIMIT_S = 600.0

# 用例在走到 record_scenario 之前就抛了:没有步骤就没有结论,verdict 记 invalid。
# 空步骤会被 verdict_of 算出 passed,那是这个平台上最危险的假通过 —— 崩在中间的用例
# 恰恰什么都没验过。崩本身由 pytest 报告,这里只补“收尾跑了没有”。
_INTERRUPTED_NOTE = (
    "用例在写出步骤之前就中断了:这一轮没有有效结论(测试无效),不是设备不合格;"
    "收尾动作照常执行,结果见 cleanup 字段"
)
# 步骤都成立、收尾没成立:这条用例仍然失败,但失败不在判据上。
_CLEANUP_FAILED_NOTE = "收尾失败(cleanup_failed):这是流程问题,不是判据不合格"


class ScenarioCleanup:
    """用例自己声明的收尾动作:“退出工厂模式”、“把改过的参数写回原值”……

    平台不替被测设备猜收尾:这条用例动过什么,只有条款知道。所以收尾由用例显式注册
    (scenario.on_cleanup(...)),不是藏在 fixture 里的魔法。

    正常与异常两条路径都要执行:正常路径由 record_scenario 执行(结论与收尾一起落盘);
    异常路径由场景 fixture 的收尾执行 —— 用例崩在中间时没有人会走到 record_scenario,
    而那正是设备最容易停在工厂模式的时刻。

    收尾自己抛异常不掩盖原异常:这里只记录、不上抛,用例手上那个异常照旧是这条用例的
    结论;收尾失败是流程问题,写进 cleanup 字段并用 cleanup_failed 标出来。
    """

    def __init__(self) -> None:
        self._actions: list[tuple[str, Callable[[], Any]]] = []
        self._results: list[dict] | None = None

    def on_cleanup(self, action: Callable[[], Any], *, label: str = "") -> None:
        """注册一条收尾动作。label 不写时用函数名(lambda 一律叫 <lambda>,所以建议写)。"""
        self._actions.append((label or getattr(action, "__name__", "") or "收尾动作", action))

    @property
    def ran(self) -> bool:
        return self._results is not None

    def run(self) -> list[dict]:
        """逐条执行(只执行一次):一条失败不影响后面的,也不上抛。"""
        if self._results is not None:
            return self._results
        results = []
        for label, action in self._actions:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - 收尾失败记下来,不能顶掉用例手上的原异常
                results.append(
                    {
                        "action": label,
                        "passed": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            else:
                results.append({"action": label, "passed": True})
        self._results = results
        return results


def settle(seconds: float, reason: str = "", *, confirm_long: bool = False) -> ScenarioStep:
    """等一段固定时长,把计划时长与真正等了多久一起记成场景里的一步。

    固定时长不是"等待到条件":这里等的是条款规定的物理时长(台架输出 60 秒,表就走 60 秒的字),
    机器快慢改变不了它,所以结论不依赖机器速度 —— ADR-0005 拒绝的是轮询到某个条件成立,
    那才会产出偶发红。**别把这里扩展成 wait_until(条件, 超时)**:要判"条件什么时候成立",
    那是重开 ADR-0005 那条不支持清单的事,不是给这个函数加参数。

    actual_s 是量出来的(monotonic 前后各一次),不是把 seconds 抄一遍:被拖长或被打断,
    记录里对得上。这一步 device 为 None,不进场景结论(见 verdict_of)——它不是断言,
    settle 本身永远判不出通过或不通过(记录里的 passed: true 只表示"这一步没有判失败的东西",
    不表示谁合格)。
    """
    if seconds < 0:
        raise ValueError(f"settle 时长不能为负:{seconds}")
    if seconds > SETTLE_LIMIT_S and not confirm_long:
        raise ValueError(
            f"settle 时长 {seconds}s 超过上限 {SETTLE_LIMIT_S}s:"
            "确认要真等这么久时传 confirm_long=True"
        )
    started = time.monotonic()
    time.sleep(seconds)
    actual = time.monotonic() - started
    action = f"等待 {seconds:g}s({reason})" if reason else f"等待 {seconds:g}s"
    return ScenarioStep(
        None,
        action,
        True,
        {"reason": reason, "target_s": seconds, "actual_s": round(actual, 3)},
    )


def verdict_of(steps: Iterable[ScenarioStep], participants: Mapping[str, str]) -> str:
    """场景结论:不成立的那一步落在谁身上,决定是"设备不合格"还是"测试无效"。

    participants 是参与方 id → role(role 来自设备清单,不在用例里重复声明)。
    没有设备的步骤(settle)不参与:等多久不构成结论,它只让后面的读数有意义。
    """
    failed = [step for step in steps if step.device is not None and not step.passed]
    if not failed:
        return "passed"
    if all(participants[step.device] == SUPPORT_ROLE for step in failed):
        return "invalid"
    return "failed"


def record_scenario(
    evidence: EvidenceSession | None,
    *,
    name: str,
    participants: Mapping[str, str],
    reading_keys: Mapping[str, Iterable[str]],
    steps: Iterable[ScenarioStep],
    identity=None,
    cleanups: ScenarioCleanup | None = None,
) -> Path | None:
    """把这一次场景的参与方、引用的读数键、步骤结果、收尾与结论写进证据目录。

    没开证据记录(--no-evidence)时什么都不写,返回 None:行为与没有这一层时一致。
    identity 是 cases.Case(测试函数上的 @case),和 per-device 证据读同一份元数据。
    cleanups 在这里执行(正常路径),结果进 cleanup 字段;收尾失败就是把结论改成 failed ——
    步骤都成立而收尾没成立时,这条用例仍然不算过。
    """
    # 收尾先跑:证据关掉时它照样要执行 —— 设备停在工厂模式与记不记证据无关。
    cleanup = cleanups.run() if cleanups is not None else []
    if evidence is None:
        return None
    steps = list(steps)
    verdict = verdict_of(steps, participants)
    if _failed_cleanups(cleanup) and verdict == "passed":
        verdict = "failed"
    record = _record_of(
        name=name,
        identity=identity,
        verdict=verdict,
        note=_note_of(verdict, cleanup),
        participants=participants,
        reading_keys=reading_keys,
        steps=steps,
        cleanup=cleanup,
    )
    return _write(evidence, name, record)


def record_interrupted_scenario(
    evidence: EvidenceSession | None,
    *,
    name: str,
    identity=None,
    cleanups: ScenarioCleanup | None = None,
) -> Path | None:
    """用例在走到 record_scenario 之前就抛了:收尾照样跑,结果照样落盘。

    没有步骤就没有结论,verdict 记 invalid(测试无效);参与方与步骤都留空 ——
    写出未发生的步骤比不写更糟。
    """
    cleanup = cleanups.run() if cleanups is not None else []
    if evidence is None:
        return None
    record = _record_of(
        name=name,
        identity=identity,
        verdict="invalid",
        note=_INTERRUPTED_NOTE,
        participants={},
        reading_keys={},
        steps=[],
        cleanup=cleanup,
    )
    return _write(evidence, name, record)


def _failed_cleanups(cleanup: Iterable[Mapping[str, Any]]) -> list:
    return [item for item in cleanup if not item["passed"]]


def _note_of(verdict: str, cleanup: Iterable[Mapping[str, Any]]) -> str:
    note = _VERDICT_NOTES[verdict]
    if _failed_cleanups(cleanup):
        return f"{note};{_CLEANUP_FAILED_NOTE}"
    return note


def _record_of(
    *,
    name: str,
    identity,
    verdict: str,
    note: str,
    participants: Mapping[str, str],
    reading_keys: Mapping[str, Iterable[str]],
    steps: Iterable[ScenarioStep],
    cleanup: Iterable[Mapping[str, Any]],
) -> dict:
    cleanup = list(cleanup)
    return {
        "scenario": name,
        "case": _case_of(identity),
        "verdict": verdict,
        "note": note,
        "participants": [
            {"id": device_id, "role": role} for device_id, role in participants.items()
        ],
        "reading_keys": {device_id: list(keys) for device_id, keys in reading_keys.items()},
        "steps": [
            {
                "device": step.device,
                "action": step.action,
                "passed": step.passed,
                "detail": dict(step.detail),
            }
            for step in steps
        ],
        "cleanup": cleanup,
        "cleanup_failed": bool(_failed_cleanups(cleanup)),
        "evidence": {device_id: f"{device_id}.jsonl" for device_id in participants},
    }


def _write(evidence: EvidenceSession, name: str, record: Mapping[str, Any]) -> Path:
    path = evidence.directory / f"scenario-{name}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _case_of(identity) -> dict | None:
    if identity is None:
        return None
    return {"id": identity.id, "clause": identity.clause, "tags": list(identity.tags)}
