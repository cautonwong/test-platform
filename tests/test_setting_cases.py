"""设置类条款:清单里一条一行,平台侧一个函数。

上游 659 条条款里 527 条是"写 N 个参数 → 读回 → 判相等",没有因果链
(.scratch/device-model/issues/35-setting-cases-table-driven.md)。写成场景函数就是把上游
350 行/条的文件换个外壳,所以这类条款的取值留在清单的 ``setting_cases`` 段里:
一个测试函数 + 清单驱动,一条条款一个节点,节点 id 就是条款编号。

身份由参数给(``@case_from_param``):编号与 clause 从清单取,收集期挂到每个节点上,
所以证据与归因里 527 条条款各有自己的 ``case.id``,而代码只有一个函数。

人工操作(切换上位机模式 / 目视看液晶 / 拔插卡)是**第三种状态** ``pending_manual``:
上游实测 17% 的步骤是人工操作,平台只能执行自动步骤。含人工步骤的条款按清单顺序
**先跑前面的自动步骤**(写 → 读回 → 判相等),全部通过之后碰到人工步骤才 skip ——
理由写"含 N 个待人工确认步骤",证据里给每一步人工操作留 ``confirmed: null`` 的占位等操作员回填。
自动步骤一旦失败,这条条款就是 failed,不许被"等人确认"盖掉:真故障不能靠 skip 隐瞒。
7,589 个人工步骤分布在 1,921 条条款里(平均每条 3.9 步),整条不跑会把同一条款里的
自动步骤(写参数、读回、判相等)一起丢掉,而那些恰恰是能抓设备故障的部分。

边界(不归本票):confirmed=false 的回填流程、写被拒的异常码语义、期望是区间、
参数依赖前一步读数、跨设备联动。有这几样的条款拆开或归场景层(tests_scenario/)。
"""
from collections import defaultdict

import pytest
from testplatform.cases import Case, case_from_param, case_of
from testplatform.composition.root import TEST_INVALID_PREFIX
from testplatform.config.manifest import (
    DUT_ROLE,
    GRID_SIMULATOR_TYPE,
    SUPPORT_ROLE,
    ManualStep,
    SettingCase,
    SettingStep,
)
from testplatform.domain.criteria import Criterion
from testplatform.reporting.evidence import BENCH_SETUP_OPERATION


@case_from_param("case_id", tags=["一致性", "设置"])
def test_setting_roundtrip(composition_root, setting_case, request):
    """写下去立刻读回:值一致才算通过,期望值与取值全在清单里。

    按清单顺序走:自动步骤先执行,碰到人工步骤才 skip(不是 pass 也不是 fail)。
    条款声明了 bench: 时,顺序是连接 → 台架前置下发 → steps → 回读判定。
    """
    device_ids = _duts_of(composition_root, setting_case.model)
    if not device_ids:
        pytest.skip(f"清单里没有型号 {setting_case.model!r} 的被测设备,这条条款没地方落")
    drivers = {device_id: composition_root.get_ready(device_id) for device_id in device_ids}
    for driver in drivers.values():
        driver.connect()
    identity = case_of(request.node)
    _apply_bench(composition_root, setting_case, identity, drivers)
    executed = 0
    for step in setting_case.steps:
        if isinstance(step, ManualStep):
            _record_pending_manual(
                composition_root, drivers, setting_case, identity, executed
            )
            pytest.skip(_manual_reason(setting_case.manual_steps, executed))
        for device_id, driver in drivers.items():
            _assert_roundtrip_step(composition_root.evidence, driver, step, identity, device_id)
        executed += 1


def _apply_bench(composition_root, case: SettingCase, identity, drivers: dict) -> None:
    """条款的台架前置:由周边设备驱动下发,跑在任何一步之前。

    前置没发下去(台架连不上 / 下发被拒 / 量不被接受) = 这一轮没有有效结论(测试无效),
    不是被测设备不合格 —— 表还没被验过。所以这里 skip 而不是失败,并把这一次下发记成
    link_lost(operation 与复位 / 断链分开),归因因此不会把它算到设备头上。

    前置发下去了、表却没反应,那是随后的步骤判定的事(step_failed → 设备不合格)。
    证据里两种现场靠 bench_setup 记录与 reading 一前一后分得开。

    shortcut: 三相前置不参与 teardown 复位(那里只复位 initial_setpoint 的电压 / 频率),
    跨条款的三相残留按台架的合并语义累积;要复位先给台架声明三相初始设定。
    """
    if not case.bench:
        return
    bench_ids = composition_root.ids_of_type(GRID_SIMULATOR_TYPE, SUPPORT_ROLE)
    if not bench_ids:
        raise ValueError(
            f"条款 {case.id} 声明了 bench: 台架前置,但清单里没有 type: {GRID_SIMULATOR_TYPE} 的 "
            f"role: {SUPPORT_ROLE} 设备能接下它"
        )
    for bench_id in bench_ids:
        try:
            applied = composition_root.get(bench_id).apply_three_phase(dict(case.bench))
        except Exception as exc:  # noqa: BLE001 - 台架的任何下发异常都让这一条测试无效
            if composition_root.evidence is not None:
                composition_root.evidence.record_link_lost(
                    bench_id, operation=BENCH_SETUP_OPERATION, cause=exc
                )
            pytest.skip(
                f"{TEST_INVALID_PREFIX}台架 {bench_id!r} 没接下条款 {case.id} 的前置 "
                f"{dict(case.bench)}: {exc}"
            )
        if composition_root.evidence is None:
            continue
        for device_id in drivers:
            composition_root.evidence.record_bench_setup(
                device_id,
                bench=bench_id,
                setpoints=case.bench,
                applied=applied.values,
                case=identity,
            )


def _manual_reason(manual: tuple[ManualStep, ...], executed: int) -> str:
    """skip 理由:待人工确认自己一档,不许混成“测试无效”。

    测试无效是台架出了问题(断链 / 复位失败),下一步查线;待人工确认是这一条本来就
    要人动手,下一步找操作员。两者都 skip、退出码都是 0,但归因里分得开。
    已执行的步数写进理由:skip 了不等于一步没跑,报告里得看得出跑到哪一步停的。
    """
    actions = ";".join(step.action for step in manual)
    return (
        f"含 {len(manual)} 个待人工确认步骤(前面的 {executed} 个自动步骤已执行且通过):"
        f"不算通过也不算失败,由操作员确认后回填:{actions}"
    )


def _record_pending_manual(
    composition_root, drivers, case: SettingCase, identity, executed: int
) -> None:
    """证据里给每一步人工操作留一条占位(confirmed: null),带上停在这一步之前已执行几步自动步骤。

    executed 随占位一起落盘:归因只读证据,数不出"这条条款跑到哪一步停的"就只能猜,
    而"已执行 N 步全部通过、还剩 M 步待确认"正是待人工确认里唯一能说的事实。

    shortcut: 回填工具本轮不做,所以只有占位、没有“填完重跑转有效结论”那条路;
              要回填时读这几条 kind: manual_step 的记录。
    """
    if composition_root.evidence is None:
        return  # 没开证据(--no-evidence):跳过时无凭可留,与读数记录同一个姿态
    for device_id in drivers:
        for step in case.manual_steps:
            composition_root.evidence.record_manual_step(
                device_id,
                action=step.action,
                expect=step.expect,
                case=identity,
                executed_steps=executed,
            )


def _assert_roundtrip_step(evidence, driver, step: SettingStep, identity, device_id: str) -> None:
    """写一步、读回一步、判相等。相等口径就是判据的 equals(两边都能当数字按数值比,否则按文本比)。

    读回单独读一次而不是拿 write() 的返回值:写入的回读已经在 write 证据里(verified),
    而判定这一读要进读数历史,证据里才带着条款身份与判据结论可查。单读(read)不产生读数,
    所以这里走 read_all —— 条款的判定离不开一条可判的读数。
    不相等时先在证据里点名是哪一步(键 + 期望 + 实测)再失败:归因据此判"设备不合格",
    不会把这条条款读成"等人确认"。
    """
    driver.write(step.write, step.value)
    actual = driver.read_all().readings[step.write]
    breach = Criterion(quantity=step.write, equals=step.expect).evaluate(actual)
    if breach is not None and evidence is not None:
        evidence.record_step_failure(
            device_id,
            key=step.write,
            value=actual,
            expected=step.expect,
            text=breach.describe(),
            case=identity,
        )
    assert breach is None, (
        f"条款 {identity.id}({identity.clause}) 在设备 {device_id} 上不成立:{breach.describe()}"
    )


def _duts_of(composition_root, model_name: str) -> list[str]:
    """这条条款落在哪几台被测设备上。型号决定点表;周边设备不参与一致性判定。"""
    return [
        driver.device.id
        for driver in composition_root.all_drivers()
        if composition_root.model_of(driver.device.id).name == model_name
        and composition_root.role_of(driver.device.id) == DUT_ROLE
    ]


class _FixedReadback:
    """不管写什么、读回都是同一个值:用来验证"期望写错"时的失败消息。"""

    def __init__(self, readback: float) -> None:
        self.readback = readback

    def write(self, key: str, value: float) -> float:
        return self.readback

    def read_all(self) -> "_FixedReadback":
        return self

    @property
    def readings(self) -> dict:
        return defaultdict(lambda: self.readback)


def test_a_wrong_expect_names_the_clause_and_the_key():
    """故意写错一个 expect:消息里必须看得出是哪条条款、哪个键、期望什么、实际什么。"""
    identity = Case(id="M_00_00_10_000008_02", clause="DLT645-4.5.2")
    step = SettingStep(write="有功计量模式", value=1601.0, expect="1600")

    with pytest.raises(AssertionError) as excinfo:
        _assert_roundtrip_step(None, _FixedReadback(1601.0), step, identity, "meter_01")

    message = str(excinfo.value)
    assert "M_00_00_10_000008_02" in message and "DLT645-4.5.2" in message
    assert "有功计量模式" in message
    assert "'1600'" in message and "1601.0" in message
