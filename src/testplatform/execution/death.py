"""
产品进程死因的分类(票 03):依据是平台自己有没有发起过终止,不是退出码。

    self_exit        产品自己退出 —— 产品不合格,证据记下 how / code / signal
    platform_killed  平台主动终止(收尾 / 任务超时 / 取消 / 重启)—— 终止本身不产生结论:
                     已跑完的用例保持各自结论,没跑到的另外记 unexecuted{count, reason}
    (不记)           产品没到过就绪就没了 = 起不来(可执行缺失 / 参数错 / 权限),
                     那是平台错误,不能读成产品不合格

被放弃的那次尝试(端口被抢 → 换端口重来)单独用 attempt_exit 记:退出码与"是不是平台停的"
是事实,写进 port_retry;而死因没到过就绪就不成立,一律 unverified —— 不留空。

两个函数都是纯的:吃进程 / 范围记录,吐证据里的那一条。所以"分类改错"能在毫秒级用例里红,
不必造三种真崩溃;真进程那侧另有用例验接线(进程真的起了、死了、证据真的落了)。

规则本身只有一份,但有两个调用点(等 pytest 时发现产品没了、收尾时停产品)——
分头判会一边说 self_exit、另一边说 platform_killed,所以放一个模块里两边都读。
"""
from __future__ import annotations

from typing import Any, Mapping

SELF_EXIT = "self_exit"
PLATFORM_KILLED = "platform_killed"
# 没到过就绪的进程:退出码是事实,但它是自己死的还是平台停的,平台不下结论(也下不了)。
# 读法:**平台没拿到死因** —— 不等于"产品没崩":产品确实退出了,退出码就在旁边的 code 里。
UNVERIFIED = "unverified"


def exit_event(
    process: Any, *, ready: bool, platform_stopped: bool, at: str
) -> dict | None:
    """一条 product_exit 记录;产品没到过就绪时返回 None(起不来 = 平台错误,不记死因)。

    code 为负是 POSIX 的「被信号杀死」:拆成 code=null + signal=N,读的一方不用猜负数的含义。
    """
    if not ready:
        return None
    code = process.returncode
    signal = -code if code is not None and code < 0 else None
    return {
        "kind": "product_exit",
        "how": PLATFORM_KILLED if platform_stopped else SELF_EXIT,
        "code": None if signal is not None else code,
        "signal": signal,
        "at": at,
    }


def attempt_exit(process: Any, *, platform_stopped: bool) -> dict:
    """一次被放弃的尝试里那个进程的事实:退出码与 who(谁结束的),两个字段都不许是空的。

    与 exit_event 的分工:exit_event 记的是「产品的死因」(只有到过就绪才算,没到过就是平台错误);
    这里记的是「这一轮尝试里进程怎么没的」—— 端口被抢时整轮没跑起来,不能记成产品不合格,
    但退出码得留着:丢了它,task.json 里只剩 product_exit_code=null,平台就说不清自己为什么停。
    产品已经自己退出了(platform_stopped=False)= 死因无从判断,记 unverified,不猜。
    """
    code = process.returncode
    return {
        "how": PLATFORM_KILLED if platform_stopped else UNVERIFIED,
        "code": code,
        "signal": -code if code is not None and code < 0 else None,
    }


def unexecuted_event(scope: Mapping[str, Any], reason: str) -> dict | None:
    """被平台叫停时没跑到的用例:collected 里有,executed / skipped / deselected 里都没有的。

    只有 run-scope 说得清"哪些没跑";一条也没有时返回 None —— count=0 的一条只会让读的人
    以为出过事。理由由调用方给(崩溃 / 超时 / 取消),与死因记录分开两条,报告要能分开陈述。
    """
    reached = set(scope.get("executed", ()))
    for key in ("deselected", "skipped"):
        reached.update(entry["nodeid"] for entry in scope.get(key, ()))
    remaining = {
        entry["nodeid"]
        for entry in scope.get("collected", ())
        if entry["nodeid"] not in reached
    }
    if not remaining:
        return None
    return {"kind": "unexecuted", "count": len(remaining), "reason": reason}
