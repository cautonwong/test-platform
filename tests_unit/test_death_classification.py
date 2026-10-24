"""
死因分类的毫秒级用例:假进程 / 假范围记录直接喂给分类器。

真进程那侧另有接线用例(tests_platform/test_task_runner.py:产品真的起了、死了、证据真的落了)。
这一侧只管"分类改错时必须红" —— 造三种真崩溃又慢又脆,覆盖不全四个分支。
"""
from types import SimpleNamespace

from testplatform.execution.death import (
    PLATFORM_KILLED,
    SELF_EXIT,
    exit_event,
    unexecuted_event,
)

AT = "2026-10-26T00:00:00.000+00:00"


def _process(code):
    return SimpleNamespace(returncode=code)


def test_self_exit_is_a_product_failure_with_its_code():
    event = exit_event(_process(3), ready=True, platform_stopped=False, at=AT)
    assert event == {"kind": "product_exit", "how": SELF_EXIT, "code": 3, "signal": None, "at": AT}


def test_killed_by_a_signal_keeps_the_signal_not_a_negative_code():
    event = exit_event(_process(-15), ready=True, platform_stopped=False, at=AT)
    assert event["how"] == SELF_EXIT
    assert event["code"] is None and event["signal"] == 15


def test_platform_stop_produces_no_conclusion_of_its_own():
    event = exit_event(_process(1), ready=True, platform_stopped=True, at=AT)
    assert event["how"] == PLATFORM_KILLED


def test_product_that_never_became_ready_has_no_death_cause():
    """起不来(可执行缺失 / 参数错 / 权限)是平台错误:退出码非零也不记成产品不合格。"""
    assert exit_event(_process(3), ready=False, platform_stopped=False, at=AT) is None
    assert exit_event(_process(0), ready=False, platform_stopped=True, at=AT) is None


def test_unexecuted_counts_what_neither_ran_nor_was_skipped_or_deselected():
    """超时 / 崩溃后被叫停:collected 里剩下的就是没执行的,跳过与被筛掉的不算。"""
    scope = {
        "collected": [{"nodeid": name} for name in ("a", "b", "c", "d", "e")],
        "executed": ["a"],
        "skipped": [{"nodeid": "b"}],
        "deselected": [{"nodeid": "c"}],
    }
    assert unexecuted_event(scope, "task_timeout") == {
        "kind": "unexecuted",
        "count": 2,
        "reason": "task_timeout",
    }


def test_unexecuted_is_absent_when_nothing_was_left_behind():
    scope = {"collected": [{"nodeid": "a"}], "executed": ["a"], "skipped": [], "deselected": []}
    assert unexecuted_event(scope, "product_crashed") is None


def test_wire_labels_are_pinned_literals():
    """证据是给 04 / 人看的 wire 格式:`how` 的字面值是契约,不能跟着常量名一起漂移。"""
    assert (SELF_EXIT, PLATFORM_KILLED) == ("self_exit", "platform_killed")
