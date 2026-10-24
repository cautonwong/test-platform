"""
周边设备故障的人可见文案:同一份 link_lost 有三种来路,三种排障方向不同,文案不许串。

    收帧阶段断链        查线(跑到一半掉的线)
    台架复位失败        查复位命令(台架没回到清单声明的 initial_setpoint)
    装配期连不上        查线接没接上

三者都记进同一份记录(都是"这一轮没有有效结论"),所以文案是唯一的区分点:
写成一句"断链",现场会按错的方向查,而证据字段全是对的 —— 这正是最难发现的那种错。
"""
import pytest
from testplatform.plugin import _link_lost_action, _link_lost_title

from testplatform.reporting.attribution import _operation_text
from testplatform.reporting.evidence import SETPOINT_RESTORE_OPERATION, _round_note

BROKEN = {"operation": "receive", "device": "grid_sim_01"}
RESTORE = {"operation": SETPOINT_RESTORE_OPERATION, "device": "grid_sim_01"}
CONNECT = {"operation": "connect", "device": "grid_sim_01"}


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([BROKEN], "本轮存在断链:相关用例已按“测试无效”跳过(不是设备不合格),结论需复核"),
        ([RESTORE], "本轮存在台架复位失败:相关用例已按“测试无效”跳过(不是设备不合格),结论需复核"),
        (
            [BROKEN, RESTORE],
            "本轮存在断链与台架复位失败:相关用例已按“测试无效”跳过(不是设备不合格),结论需复核",
        ),
        ([], "本轮没有断链"),
    ],
)
def test_round_status_note_names_what_actually_happened(entries, expected):
    assert _round_note(entries) == expected


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([BROKEN], "断链"),
        ([RESTORE], "台架复位失败"),
        ([BROKEN, RESTORE], "断链与台架复位失败"),
    ],
)
def test_the_terminal_section_title_names_what_actually_happened(entries, expected):
    assert _link_lost_title(entries) == expected


def test_each_record_says_what_it_was():
    assert _link_lost_action(BROKEN) == "receive 时断链"
    assert _link_lost_action(RESTORE) == "恢复初始设定失败"


def test_attribution_says_what_it_was():
    """归因里那条"测试无效"同理:它带 operation,渲染时按 operation 说人话。"""
    assert _operation_text(SETPOINT_RESTORE_OPERATION) == "恢复初始设定失败"
    assert _operation_text("connect") == "连接失败"
    assert _operation_text("receive") == "在 receive 时断链"
