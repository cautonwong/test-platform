"""
证据落盘 —— 一次测试留下的原始报文、解码读数、适用判据与结论,按设备写进 JSONL。

采集方式是装饰:RecordingTransport 包在真实传输外面,形状与 ByteStreamTransport
一模一样,驱动层和领域层照旧 send/receive,不知道外面多了一层写字的手 ——
证据采集因此不侵入任何业务代码。

目录按会话隔离:evidence/<会话时间戳>/<设备id>.jsonl,两次运行不互相覆盖;
并行时目录名带上 worker 名(evidence/<时间戳>-gw0/),每个 worker 各开一个会话,
同一秒开也不会撞到同一个目录,更不会有两条用例从两个 worker 写同一份设备文件。
每行一条记录,用 kind 区分:
    frame    一次收发的原始报文(hex)与时间戳;收发失败时带 error(失败种类)
    reading  一次读数:解码后的物理量、实际生效的判据集、求值结论、领域事件、用例身份、
             这一轮跑在实际模式(real / simulator)上
    event    这一轮只有事件没有读数(比如设备进故障态)时的记录
    link_lost 周边设备在收帧阶段断链:设备 id、时间、失败种类与当时在干什么。
             单独一个 kind,不混进 reading —— 它不产生读数,也不参与判合格。
    injection 一次数据注入:哪个设备、哪个读数键、被设成什么、编码后的字节、哪条用例设的。
             单独一个 kind,不混进 write —— 注入不是写路径,"产品能不能写设备"不能拿它当证据。
    batch_fallback 块读降级:一次合并的块读被从站用异常应答拒掉,已拆开逐点重试。
             带这次块的地址区间、异常码、逐点读到的点与真读不到的点。
             没有它,现场只会看到"这次读数突然慢了好几倍"却看不出为什么。
    write    一次写动作:读数键、写下去的物理量、回读到的值、两者是否一致(verified)。
             写成功和值真的生效是两件事,所以回读结果跟写动作记在同一条里;
             写失败(超时 / 异常应答)不写这条,它已经落在 frame 那条的 error 上。
    manual_step 一步人工操作的占位:做什么(action)、由谁按什么确认(expect)、
             confirmed 初始为 null。人工操作平台看不见,所以它既不产生读数、也不判合格;
             confirmed 留着给回填工具填(确认人 + 时间),没有它这一条永远不算通过。
             executed_steps 是停在这一步之前已执行并通过的自动步骤数 —— 含人工步骤的条款
             先跑自动步骤、碰到人工步骤才停,所以证据要分得清“跑到哪一步”与“一步没跑”。
    bench_setup 条款的台架前置下发了什么:量名 → 值,以及台架回报的实际值。
             写在被测设备那一份里(台架自己那份有 frame 记录):现场要分得清
             “前置没发下去”与“前置发下去了、表没反应”—— 前者落成 link_lost(测试无效),
             后者落成 step_failed(设备不合格),这条记录是两种结论的分界点。
    step_failed 一个自动步骤没通过:读数键、写下去的值、期望值、判据给的说明。
             单独一条,是因为它不产生判据集里的读数越界(相等口径由条款自己给),
             不记下来归因就只能把它当“没有证据的失败”,说不出是哪一步不成立。
    evidence_gap 一次取增量时读数的缺口:两轮之间记的条数超过 history 窗口,最旧的那几条
             已被裁掉、取不回来。带缺了几条(missing)与窗口大小(window)。
             没有它,证据会看起来完整而实际缺条 —— 失败必留证据这条承诺就无声地破了。

所有时间戳(记录里的 at、领域读数的 timestamp、会话目录名)都按 UTC 取,记录里的 at
带偏移(+00:00)。两台台架各跑一轮、把证据并到一起时才能排出先后。旧证据的 at 没有偏移,
读的一方按本地时间理解(见 parse_at):改格式不改读法,否则跨台架的问题会换成跨版本的问题。
会话结束时另写两份:
    round-status.json  这一轮有没有断链、哪些设备,CI / 报告层据此复核,不必解析终端文本
    run-scope.json     这一轮的范围:哪些用例被收集、哪些真正执行过、哪些被筛掉 / 跳过,以及为什么。
                       只记范围,不记判定 —— 判定仍然只有 pytest 报告和证据两处,
                       不出现第三个真相源
聚合根不变量被违反(平台自身有 bug)时另写一条 platform-errors.jsonl:
    platform_error   设备 id、异常类型、消息、用例 nodeid、时间。单独一个文件,
                      不混进任何设备的 JSONL —— 否则那台设备会凭空长出一条"不合格"的错觉
没有证据的结论不可复现,所以失败也要落盘:报文在 send/receive 那一刻就写,
收不回来也写一条带 error 的 —— "没回"与"回了但被拒"在证据里必须长得不一样。
读数由这一轮的收尾动作补写;用例中途崩了,组合根 teardown 还有一次兜底。

每台设备一个长期打开的句柄,每条记录写完 flush:一轮 5940 条记录时逐条 open/close
要 8 秒(实测),这是唯一随规模线性放大的开销。flush 之后记录仍在 OS 里,
所以"报文在收发那一刻就落盘"这条承诺不变 —— 攒在内存里批量写会让崩溃丢掉最后几条,
那正是证据层最不能接受的。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, TextIO

from testplatform.domain.device import HISTORY_WINDOW
from testplatform.transport.interfaces import ByteStreamTransport, FrameLength

EVIDENCE_ROOT = "evidence"
ROUND_STATUS_FILE = "round-status.json"
RUN_SCOPE_FILE = "run-scope.json"
PLATFORM_ERRORS_FILE = "platform-errors.jsonl"
# 台架复位在记录里的 operation 名:驱动上的入口就叫 apply_initial_setpoint,
# 写的人和读的人用同一个名字。复位失败与收帧阶段断链都记进 link_lost
# (都是周边设备不合作 = 这一轮结论不成立),但人可见的文案必须分开:
# 一个是查线,一个是查复位命令。
SETPOINT_RESTORE_OPERATION = "apply_initial_setpoint"
# 台架前置下发的 operation 名:同样是驱动上的入口名(apply_three_phase)。
# 前置没发下去 = 这一轮没有有效结论(测试无效),与断链、复位失败同归 link_lost,
# 但三种现场排障方向不同,所以 operation 必须分开写。
BENCH_SETUP_OPERATION = "apply_three_phase"


class EvidenceSession:
    """一次会话的证据目录:每台被测设备一份 JSONL,记录它这一轮的收发与读数。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # 每台设备上次取走时的 readings_recorded 值:差值就是这一轮新增了几条。
        # 用它而不是 len(history) 当游标 —— history 是固定窗口,长度会封顶,
        # 拿封顶的长度当游标会让窗口之后再记的读数永远漏记。
        self._recorded_readings: Dict[str, int] = {}
        # 这一轮的断链(只记周边设备):写 round-status.json 与终端汇总都读它。
        self.link_lost: list[dict] = []
        # 事件没送到 sink 的那些(票 13a2):`missing` 是**条数**,不是布尔。
        # 报告读它生成 evidence_gap{kind: event_emit_failed} —— 只写终端等于复盘的人看不到。
        self.event_emit_failed: list[dict] = []
        # 每台设备一个长期打开的句柄:懒打开(第一条记录时),close() 统一关。
        self._streams: Dict[Path, TextIO] = {}

    @classmethod
    def open(cls, base: str | Path = EVIDENCE_ROOT) -> "EvidenceSession":
        """按会话时间戳建目录。同一秒里跑了两次就加序号——上一次的证据不能被覆盖。

        并行执行时每个 worker 是独立进程、各自开一次会话:目录名再带上 worker 名
        (pytest-xdist 给的 PYTEST_XDIST_WORKER,gw0 / gw1 …),否则同一秒启动的
        几个 worker 会算到同一个目录名上,证据互相覆盖。
        """
        root = Path(base)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        name = f"{stamp}-{worker}" if worker else stamp
        directory, index = root / name, 1
        while directory.exists():
            index += 1
            directory = root / f"{name}-{index}"
        return cls(directory)

    def path_of(self, device_id: str) -> Path:
        return self.directory / f"{device_id}.jsonl"

    def open_device(self, device_id: str) -> None:
        """先占一份文件:这一轮一条读数都没有的设备,目录里也看得出测过它。"""
        self.path_of(device_id).touch()

    def record_injection(
        self,
        device_id: str,
        *,
        key: str,
        value: float,
        raw: bytes,
        identity: Any = None,
        mode: str = "simulator",
    ) -> dict:
        """一次数据注入:事后要能看出"这个值是测试设的,不是设备自己的"。

        没有这条记录,一条"产品读到 1234 kWh"的证据就分不清是现场值还是用例摆出来的,
        报告也就说不出这条结论验的是什么。identity 是设置它的那条用例的身份。
        """
        entry: Dict[str, Any] = {
            "kind": "injection",
            "device": device_id,
            "key": key,
            "value": value,
            "hex": raw.hex(),
            "mode": mode,
            "at": _now(),
        }
        identity_payload = _case_of(identity)
        if identity_payload is not None:
            entry["case"] = identity_payload
        self.record(device_id, entry)
        return entry

    def record_link_lost(self, device_id: str, *, operation: str, cause: BaseException) -> dict:
        """周边设备不合作:落一条 link_lost,并记进本轮状态。

        三种来路(operation 区分):收帧阶段断链、收尾没把台架恢复回初始设定、
        条款的台架前置没发下去。这里只负责记 —— "这一条用例测试无效"的判定在调用点,
        两者分开:没有证据层也照样按现场判。
        """
        entry = {
            "device": device_id,
            "at": _now(),
            "operation": operation,
            "failure": _failure_kind(cause),
            "message": str(cause),
        }
        self.link_lost.append(entry)
        self.record(
            device_id,
            {
                "kind": "link_lost",
                "operation": operation,
                "failure": entry["failure"],
                "message": entry["message"],
            },
        )
        return entry

    def record_manual_step(
        self, device_id: str, *, action: str, expect: str, executed_steps: int, case=None
    ) -> dict:
        """一步人工操作的占位:平台没有它的机读判据,所以只留"做了什么、按什么确认"。

        confirmed 先把 null 写进去 —— 键本身不能缺:回填工具上线时,缺键与"填了没确认"
        是同义的,分不清就会把没确认的步骤读成已确认。回填是后续工具的事
        (confirmed + 时间 + 人),这一层只保证占位落得下来。

        executed_steps 由用例给(它才知道自己跑到哪一步停的):写进来,归因才说得出
        "已执行的 N 步全部通过、还有 M 步待人工确认",而不是只剩一条“跳过”。
        """
        entry = {
            "kind": "manual_step",
            "case": _case_of(case),
            "action": action,
            "expect": expect,
            "confirmed": None,
            "executed_steps": executed_steps,
        }
        self.record(device_id, entry)
        return entry

    def record_bench_setup(
        self,
        device_id: str,
        *,
        bench: str,
        setpoints: Mapping[str, float],
        applied: Mapping[str, float],
        case=None,
    ) -> dict:
        """条款的台架前置下发了什么:量名 → 值,以及台架回报的实际值。

        写在被测设备那一份里,顺序上就落在随后的 reading 之前:
        "前置发了"与"表有没有反应"因此可以事后分开看。
        """
        entry = {
            "kind": "bench_setup",
            "case": _case_of(case),
            "bench": bench,
            "setpoints": dict(setpoints),
            "applied": dict(applied),
        }
        self.record(device_id, entry)
        return entry

    def record_step_failure(
        self, device_id: str, *, key: str, value: Any, expected: Any, text: str, case=None
    ) -> dict:
        """一个自动步骤没通过:写下去什么、读回什么、期望什么,一条记下来。

        判据集只判物理量的区间与等值,而条款的相等口径(期望值)来自清单,不在判据集里 ——
        所以这一步的结论只能由用例给出。不记进证据,归因就只能把它当“pytest 报了失败、
        证据里没有结论”,说不出是哪一步、哪个键不成立。
        """
        entry = {
            "kind": "step_failed",
            "case": _case_of(case),
            "key": key,
            "value": value,
            "expected": expected,
            "text": text,
        }
        self.record(device_id, entry)
        return entry

    def write_round_status(self) -> Path:
        """会话结束时写一份机器可读的本轮状态,让人和 CI 不必解析终端文本。

        没有断链时文件照样写、link_lost 为空列表:"文件不存在"与"没跑完"不能混为一谈。
        """
        status = {
            "verdict": "needs-review" if (self.link_lost or self.event_emit_failed) else "clean",
            "note": _round_note(self.link_lost),
            "link_lost": list(self.link_lost),
            # 事件没落盘也是"这一轮结论不成立"的一种:报告据此生成 evidence_gap
            "event_emit_failed": list(self.event_emit_failed),
        }
        path = self.directory / ROUND_STATUS_FILE
        path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def write_run_scope(self, scope: Mapping[str, Any]) -> Path:
        """会话结束时写一份"这一轮的范围":哪些用例被收集、真正执行过、被筛掉 / 跳过及原因、哪些失败了。

        范围是归因入口唯一能知道"被 --select 筛掉、被能力跳过、失败发生在写证据之前"的途径:
        证据里只有真跑过的东西,"没跑"不落成记录就只能靠缺席去猜。所以这里只写范围、不写判定。
        executed 是"真正执行过"的唯一来源:没有它(旧证据目录)归因只有"执行条数未知",
        不许拿 collected − skipped 冒充 —— `pytest -x` 中途停下时那个数把没跑的算成跑了。
        failed / errored 同理:没有它们,在写证据之前就崩掉的用例会从归因里彻底消失。
        写失败不吞异常:缺了这份文件,归因入口必须报"范围缺失"而不是当成"这轮只有这些用例"。
        """
        record = {
            "note": "本文件只记这一轮的范围:哪些用例被收集、哪些真正执行过、哪些没跑(被筛掉 / 被跳过)及原因、哪些失败了。判定结果不在这里。",
            "collected": list(scope.get("collected", ())),
            "deselected": list(scope.get("deselected", ())),
            "skipped": list(scope.get("skipped", ())),
        }
        if "executed" in scope:
            # 缺这个键 = 不知道跑了多少;写成 [] 会被读成"一条也没跑"。
            record["executed"] = list(scope["executed"])
        for key in ("failed", "errored"):
            if key in scope:
                record[key] = list(scope[key])
        path = self.directory / RUN_SCOPE_FILE
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def record_event_emit_failure(self, device_id: str, *, missing: int, reason: str) -> dict:
        """有 N 条事件没送到 sink(sink 自己坏了):丢掉的是归因的输入,必须进报告。

        只允许**真实失败**写进来:单元里注入的假 sink 不许碰真证据会话(否则测试污染别人的报告)。
        """
        entry = {"device": device_id, "missing": int(missing), "reason": reason, "at": _now()}
        self.event_emit_failed.append(entry)
        return entry

    def record_platform_error(self, *, nodeid: str, case, cause: BaseException) -> dict:
        """聚合根不变量被违反 = 平台自身有 bug:落一条现场记录,与设备不合格彻底分开。

        这一刻不记,后面只能靠"证据看起来矛盾"去推断,而推断出来的东西没法核对。
        所以记下设备(异常自己带 device_id 时才记)、异常类型、消息、用例身份与 nodeid。
        """
        entry = {
            "at": _now(),
            "kind": "platform_error",
            "device": getattr(cause, "device_id", None),
            "case": _case_of(case),
            "nodeid": nodeid,
            "error": type(cause).__name__,
            "message": str(cause),
        }
        self._append(self.directory / PLATFORM_ERRORS_FILE, entry)
        return entry

    def record(self, device_id: str, record: Mapping[str, Any]) -> None:
        self._append(self.path_of(device_id), {"at": _now(), "device": device_id, **record})

    def close(self) -> None:
        """关掉所有句柄。重复调用无害:组合根 teardown 与 pytest 会话收尾都会调一次。

        正确性不依赖这次关闭 —— 每条记录写完就 flush,进程被强杀也不丢已写的记录。
        关句柄是为了不留悬空文件句柄,而不是为了把数据刷出去。
        """
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()

    def _append(self, path: Path, entry: Mapping[str, Any]) -> None:
        stream = self._streams.get(path)
        if stream is None:
            stream = self._streams[path] = path.open("a", encoding="utf-8")
        stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        # 每条写完就交给 OS:崩溃 / 被中断时已写的记录仍在文件里(攒批量写做不到)。
        stream.flush()

    def wrap(self, device_id: str, transport: Any) -> "RecordingTransport":
        """给一台设备的传输套上记录层:对外还是同一个传输协议形状。

        记录层只实现了字节流的五个方法,别的形状(CAN 的 send_frame、HTTP 的 request……)
        套进来会在第一次连接时变成"驱动写对了却连不上"的 AttributeError。所以这里在
        装配期就拒掉:证据层目前只支持字节流规约,不支持就说清楚,不假装支持。
        """
        if not isinstance(transport, ByteStreamTransport):
            raise TypeError(
                f"设备 {device_id!r} 的传输是 {type(transport).__name__},不是字节流"
                "(connect / disconnect / has_client / send / receive):"
                "证据层目前只支持字节流规约,不能给这个形状的设备记录证据"
            )
        return RecordingTransport(transport, self, device_id)

    def record_round(
        self,
        *,
        drivers: Mapping[str, Any],
        criteria_overrides: Mapping[str, Iterable[str]],
        modes: Mapping[str, str],
        identity=None,
        events: Iterable[Any] = (),
    ) -> None:
        """把这一轮(一条用例)新增的读数、判据结论、事件与用例身份写进各自的证据文件。

        identity 是 cases.Case(来自测试函数上的 @case),证据因此能回答
        "这是哪条用例、依据哪条标准条款",不需要第二份用例清单。
        modes 是组合根已经算好的实际模式(real / simulator):一份证据必须能自证
        "这条结论是模拟器跑出来的还是真机上跑出来的",否则两者长得一模一样。
        """
        by_device: Dict[str, list] = {}
        for event in events:
            by_device.setdefault(event.device_id, []).append(event)

        for device_id, driver in drivers.items():
            device = driver.device
            recorded = getattr(device, "readings_recorded", 0)
            fresh_count = recorded - self._recorded_readings.get(device_id, 0)
            history = getattr(device, "history", ())
            # max(..., 0):两轮之间记了超过一个窗口的读数时,已被裁掉的补不回来,
            # 能取多少取多少 —— 所以每轮结束就要取走,不能攒几轮再取。
            fresh = history[max(len(history) - fresh_count, 0) :]
            missing = fresh_count - len(fresh)
            if missing > 0:
                # 拿不回来的那几条必须留下痕迹:证据里少几条却看不出来,
                # “失败必留证据”就无声地破了。归因把这条归进「证据损坏」(责任在写证据这一侧)。
                self.record(
                    device_id,
                    {
                        "kind": "evidence_gap",
                        "missing": missing,
                        "window": HISTORY_WINDOW,
                        "note": (
                            f"两轮之间有 {fresh_count} 条读数,窗口只留最近 {HISTORY_WINDOW} 条,"
                            f"最旧的 {missing} 条已被裁掉、不在证据里"
                        ),
                    },
                )
            self._recorded_readings[device_id] = recorded
            produced = by_device.get(device_id, [])
            if not fresh:
                if produced:
                    self.record(
                        device_id,
                        {
                            "kind": "event",
                            "case": _case_of(identity),
                            "events": _events_of(produced),
                        },
                    )
                continue
            for index, reading in enumerate(fresh):
                record = {
                    "kind": "reading",
                    "mode": modes.get(device_id),
                    "case": _case_of(identity),
                    "criteria": _criteria_of(device, criteria_overrides.get(device_id, ())),
                    "readings": _quantities_of(reading),
                    "evaluation": _evaluation_of(reading),
                    "events": [],
                }
                if index == len(fresh) - 1:
                    # 事件在一次读数里产生、按轮取走:记在同一轮最后一条读数上,不重复。
                    record["events"] = _events_of(produced)
                self.record(device_id, record)


class RecordingTransport:
    """装饰一层真实传输:形状不变,只把收发的原始字节写进证据文件。"""

    def __init__(self, inner: Any, session: EvidenceSession, device_id: str) -> None:
        self._inner = inner
        self._session = session
        self._device_id = device_id

    def connect(self) -> None:
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def has_client(self) -> bool:
        return self._inner.has_client()

    def report_degraded_block(self, record: Mapping[str, Any]) -> None:
        """驱动层报告一次块读降级:合并的块读被异常应答拒掉,已拆开逐点重试。

        字段由报告方给(块区间、异常码、读到的点、读不到的点),这一层只管落盘。
        驱动层用 getattr 探这个方法:没套记录层(没开证据)时它不存在,行为不变。
        """
        self._session.record(self._device_id, {"kind": "batch_fallback", **record})

    def report_write(self, record: Mapping[str, Any]) -> None:
        """驱动层报告一次写动作(含回读结果):落一条 kind: write。

        和 report_degraded_block 同一个姿态:驱动层用 getattr 探这个方法,
        没套记录层(没开证据)时它不存在,行为不变。
        """
        self._session.record(self._device_id, {"kind": "write", **record})

    def send(self, data: bytes) -> None:
        self._session.record(
            self._device_id, {"kind": "frame", "direction": "send", "hex": data.hex()}
        )
        try:
            self._inner.send(data)
        except Exception as exc:
            # 链路断在发这一侧(设备拔了、socket 被对端关掉):同样要留下失败种类。
            self._record_failure("send", exc)
            raise

    def receive(self, timeout: float = 2.0, expected: FrameLength = None) -> bytes:
        started = time.monotonic()
        try:
            data = self._inner.receive(timeout=timeout, expected=expected)
        except Exception as exc:
            # 收不到应答正是最需要证据的时候:`error: timeout` 与"收到了但被拒"(有 hex、无 error)
            # 在证据里必须一眼分得开,否则现场排障只能猜。异常照常往上抛,行为不变。
            # 失败种类见 _failure_kind:断链记 closed,设备不回记 timeout,两者不能合并。
            self._record_failure(
                "receive", exc, timeout=timeout, waited=round(time.monotonic() - started, 3)
            )
            raise
        self._session.record(
            self._device_id, {"kind": "frame", "direction": "receive", "hex": data.hex()}
        )
        return data

    def _record_failure(self, direction: str, exc: BaseException, **extra: Any) -> None:
        self._session.record(
            self._device_id,
            {"kind": "frame", "direction": direction, "error": _failure_kind(exc), **extra},
        )


def _round_note(link_lost: list) -> str:
    """本轮状态的说明。断链、台架复位失败、台架前置下发失败是三种现场,排障方向不同,
    不能合成一句"断链"。"""
    if not link_lost:
        return "本轮没有断链"
    operations = [entry["operation"] for entry in link_lost]
    known = (SETPOINT_RESTORE_OPERATION, BENCH_SETUP_OPERATION)
    parts = [
        name
        for name, hit in (
            ("断链", any(operation not in known for operation in operations)),
            ("台架复位失败", SETPOINT_RESTORE_OPERATION in operations),
            ("台架前置下发失败", BENCH_SETUP_OPERATION in operations),
        )
        if hit
    ]
    return f"本轮存在{'与'.join(parts)}:相关用例已按“测试无效”跳过(不是设备不合格),结论需复核"


def _failure_kind(exc: BaseException) -> str:
    """失败种类:timeout / closed / disconnect,其余记异常类名。

    先认超时(TimeoutError 在 3.10+ 也是 OSError),再认对端关闭(ConnectionError 是 OSError 的子类),
    剩下的 OSError 才是链路不可达。closed 与 timeout 必须分开:超时可能是设备问题(判不合格),
    断链是台架问题(判测试无效)—— 合并了上层就无从区分结论归属。
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "closed"
    if isinstance(exc, OSError):
        return "disconnect"
    return type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_at(text: str) -> datetime:
    """读一条证据记录里的 at,给排序用:返回值一律是带时区的 datetime。

    新证据带偏移,旧证据(这条规则之前的目录)没有 —— 没有偏移就按本地时间理解,
    否则跨版本的证据混在一起时排序会在 naive 与 aware 之间抛 TypeError。
    """
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _case_of(identity) -> dict | None:
    if identity is None:
        return None
    return {"id": identity.id, "clause": identity.clause, "tags": list(identity.tags)}


def _criteria_of(device: Any, overridden: Iterable[str]) -> dict | None:
    """实际生效的判据集:基础判据集名 + 这台设备行内覆盖掉的物理量。

    判据集名来自型号(CriteriaProfile.name 是基础名),光看名字看不出行内覆盖,
    所以把被覆盖的物理量一并写下来——否则证据里读不出"实际用的是哪一套"。
    """
    profile = getattr(device, "criteria", None)
    if profile is None:
        return None  # 终端这类没有上游验收判据的设备:读数只记录,不判合格
    return {
        "profile": profile.name,
        "overridden": list(overridden),
        "criteria": [_criterion_of(criterion) for criterion in profile.criteria],
    }


def _criterion_of(criterion) -> dict:
    return {
        "quantity": criterion.quantity,
        "low": criterion.low,
        "low_exclusive": criterion.low_exclusive,
        "high": criterion.high,
        "high_exclusive": criterion.high_exclusive,
        "severity": criterion.severity.value,
    }


def _quantities_of(reading) -> dict:
    """一次读数的物理量:电表/终端是「读数键 → 物理量」映射,其余设备类型是读数值对象上的数值字段。"""
    measured = getattr(reading, "readings", None)
    if measured is not None:
        return dict(measured)
    return {
        attribute.name: getattr(reading, attribute.name)
        for attribute in fields(reading)
        if type(getattr(reading, attribute.name)) in (int, float)
    }


def _evaluation_of(reading) -> dict | None:
    if not hasattr(reading, "breaches"):
        return None  # 不判合格的设备
    return {
        "passed": reading.passed,
        "breaches": [_breach_of(breach) for breach in reading.breaches],
    }


def _breach_of(breach) -> dict:
    """一条违约记录。

    等值违约没有边界:limit 写成 null、exceeded_by 写成 0.0,机器读成「没有边界、
    没有越界」,而结论是 passed: false —— 字面对、读起来反了。所以只留 kind 与 expected,
    两个区间字段干脆不出现(不适用就不要占位)。
    """
    if breach.expected is not None:
        return {
            "kind": "equals",
            "quantity": breach.quantity,
            "value": breach.value,
            "expected": breach.expected,
            "severity": breach.severity.value,
            "text": breach.describe(),
        }
    return {
        "quantity": breach.quantity,
        "value": breach.value,
        "limit": breach.limit,
        "exceeded_by": breach.exceeded_by,
        "severity": breach.severity.value,
        "text": breach.describe(),
    }


def _events_of(events: Iterable[Any]) -> list:
    return [{"type": type(event).__name__, **asdict(event)} for event in events]
