"""
测试层自己的 fixture。这里有两类完全不同用途的 fixture,写用例时要分清楚用哪个:

1. 泛型契约 fixture(按设备类型自动生成,比如 inverter / battery)——配合下面的
   pytest_generate_tests 钩子,在"收集测试"阶段就去读 devices.yaml,把同一个
   测试函数自动展开成"每一台该类型设备各跑一次"。新增一台逆变器,所有用这个
   fixture 写的用例会自动对它生效,不需要加任何测试代码,这是解决"很多设备"
   问题的关键机制。适合写"这一类设备所有实例都必须满足的通用规则"。

2. 具名 fixture(inverter_01 / inverter_02_via_ipc / battery_01)——
   只对应 devices.yaml 里某一个固定 id,适合写"只对这一个特定实例/
   特定接入链路有意义"的用例(比如专门验证工控机网关这条链路本身)。

3. 这里的泛型 fixture 不是手写的:文件末尾按 drivers/registry.py 里注册的设备
   类型,给每种类型各生成一个 `<类型>` fixture 和 `<类型>_id` 参数化钩子。
   新增一种设备类型只需要新建 domain + driver 文件并在组合根 import,测试层
   一个字都不用改(见 docs/WRITING_TESTS.md)。

4. 用例依赖某能力时标 `@pytest.mark.requires_capability("无功电量")`,
   设备型号没声明这项能力就在收集阶段自动跳过,用例里不写 if(机制见
   下面 pytest_collection_modifyitems)。

设备的创建/销毁由会话级的 composition_root(定义在 testplatform/plugin.py)
统一管理,这里的 fixture 只做"按 id 或按类型取出来",不负责生命周期。
"""
import os

import pytest

# 这两行 import 的作用是触发组合根的注册副作用:import 组合根会 import 所有
# 具体 driver 模块,让 @register_device 装饰器真正执行一遍。测试层由此拿到
# "当前有哪些设备类型"的权威列表,而不是自己再维护一份。
from testplatform.composition.root import (  # noqa: F401
    TEST_INVALID_PREFIX,
    CompositionRoot,
    SupportEquipmentFault,
    UpstreamFault,
)
from testplatform.application.event_relay import EventEmitFailed, drain_device_events
from testplatform.cases import case_of
from testplatform.config.loader import load_device_config
from testplatform.config.manifest import DUT_ROLE, Manifest, parse_manifest
from testplatform.drivers.registry import registered_types

from tests_support.named_device import device_or_skip
# 周边设备故障 = 测试无效的两种转换在同一处实现(tests_scenario/ 也用这一份)。
# 这里直接引名字:测试层加载这个 conftest 后按 `conftest.connect_support_or_skip` 调它
# (见 tests/test_support_equipment.py)。
from tests_support.support_faults import connect_support_or_skip, restore_support_or_invalid  # noqa: F401


@pytest.fixture(autouse=True)
def _restore_support_after_test(composition_root):
    """每条用例结束后把周边设备恢复到清单声明的 initial_setpoint。

    autouse 是因为它不能靠用例自觉:失败的那条用例把台架留在 0V 上时,下一条用例
    读到的是错值,而且会被判成“设备不合格” —— 归因里没有“被上一条用例污染”这一类。
    恢复失败 = 测试无效(不是设备不合格),转换只有 restore_support_or_invalid 一处。
    """
    yield
    restore_support_or_invalid(composition_root)


@pytest.fixture(scope="session", autouse=True)
def _connect_support_before_tests(composition_root):
    """装配顺序在这里生效:整个会话的用例开跑前,周边设备已经连上。"""
    connect_support_or_skip(composition_root)


@pytest.fixture(autouse=True)
def _drain_device_events_after_test(composition_root, event_sink, request):
    """"数据向下/事件向上"的收尾动作:每条用例跑完后,
    把这条用例期间所有设备产生的事件收集进 event_sink,并按同一份事件落证据。
    标成 autouse,是因为"收集事件"这件事不应该要求每个测试作者
    自己记得去调用——它和"组合根只装配一次"是同一种设计姿态:
    基础设施性质的事情,不应该散落在业务测试代码里重复声明。

    证据也在这里收尾:这是唯一"知道当前是哪条用例"的地方,
    用 case_of(request.node) 取 @case 身份,和筛选用的是同一份元数据。
    测试函数抛错时 fixture 的收尾照样执行,所以失败的用例也留得下证据。

    放在 tests/conftest.py 而不是插件里:插件对 tests_unit/ 也生效,
    而 tests_unit 测的是聚合根自身,不该被拉着装配任何设备。
    event_sink 本身仍留在插件里(它是会话级的汇总点,不依赖装配)。"""
    yield
    try:
        events = drain_device_events(composition_root.all_drivers(), event_sink)
    except EventEmitFailed as failure:
        # 事件没送到 sink = 归因少了一块输入:写进证据(报告据此出 evidence_gap),再原样抛 ——
        # 开发侧照样看到 ERROR。只在**有证据会话**时写:单测注入的假 sink 不许碰真证据。
        if composition_root.evidence is not None:
            for device_id, _event, cause in failure.failures:
                composition_root.evidence.record_event_emit_failure(
                    device_id, missing=failure.count_of(device_id), reason=str(cause)
                )
        raise
    composition_root.record_evidence(identity=case_of(request.node), events=events)


def _manifest(config: pytest.Config) -> Manifest:
    return parse_manifest(load_device_config(config.getoption("--device-config")))


def _device_ids_of_type(config: pytest.Config, device_type: str) -> list[str]:
    """设备类型在型号上声明,所以要先按 model 解析出每台实例属于哪一类。
    只取 role: dut —— 周边设备不参与一致性判定,契约测试不对它展开。"""
    return list(_manifest(config).ids_of_type(device_type))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """pytest 原生的动态参数化钩子:在收集阶段(此时还没有任何 fixture 被执行)
    就能看到每个测试函数声明了哪些参数,据此决定要把它展开成几个测试节点。

    这里按注册表遍历设备类型:测试函数只要用了 `<类型>` 这个泛型 fixture
    (或者写了 `<类型>_id`),就会被自动按 devices.yaml 里
    对应类型的设备数量展开——报告里会看到
    test_xxx[inv_01]、test_xxx[inv_02_via_ipc]、test_xxx[inv_03]
    这样分别成行的结果,而不是一个函数内部悄悄循环、第一个失败就看不到后面的。

    参数化的名字直接用设备类型本身,间接参数化(indirect=True)让下面生成的
    泛型 fixture 从 request.param 拿到这一轮该用哪台设备的 id。
    """
    for device_type in registered_types():
        if device_type in metafunc.fixturenames or f"{device_type}_id" in metafunc.fixturenames:
            metafunc.parametrize(
                device_type,
                _device_ids_of_type(metafunc.config, device_type),
                indirect=True,
            )

    # 设置类条款同一条路:参数取值来自清单(不是手写列表),
    # 一条条款 = 一个节点,节点 id 就是条款编号。
    if "setting_case" in metafunc.fixturenames:
        ids = sorted(_manifest(metafunc.config).setting_cases)
        if ids:
            metafunc.parametrize("case_id", ids)
        else:
            # 这份清单没有 setting_cases(比如规模清单 / 只读它类设备的客户清单):不是错,
            # 是"这个清单里没有设置类条款"。给一个显式占位并标 skip —— 不要让 pytest 自己
            # 造一个 NOTSET 出来,那会让整轮的收集直接失败("no tests ran")。
            metafunc.parametrize(
                "case_id",
                [pytest.param(None, marks=pytest.mark.skip(reason="这份清单里没有 setting_cases(设置类条款)"))],
            )


@pytest.fixture()
def setting_case(request, case_id):
    """清单里这一条设置类条款(型号 + clause + 步骤)。取值 case_id 由清单驱动。"""
    return _manifest(request.config).setting_cases[case_id]


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """收集期的两件事:能力不足的设备跳过;按拓扑给用例打并行分组。

    tryfirst 是为了赶在 pytest-xdist 之前:它那个把 xdist_group 写进 nodeid 的钩子
    比这里的 conftest 注册得更早,不抢在前面打标记,分组就静默不生效。

    能力那一条:

        @pytest.mark.requires_capability("无功电量")
        def test_meter_reactive_energy(meter): ...

    设备 id 从按类型参数化的 fixture 拿(间接参数化传的就是设备 id),
    再去型号声明的能力里查;缺哪项就报哪项,不算失败。
    """
    manifest = _manifest(config)
    models = manifest.models
    device_ids = {spec["id"]: spec["model"] for spec in manifest.devices}

    for item in items:
        marker = item.get_closest_marker("requires_capability")
        if marker is None:
            continue
        needed = set(marker.args)
        targets = _parametrized_device_ids(item, device_ids)
        if not targets:
            absent = [
                name
                for name in getattr(item, "fixturenames", ())
                if name in registered_types() and not _device_ids_of_type(config, name)
            ]
            if absent:
                # 这份清单里根本没有这类设备:与"设备缺这项能力"同一个结论 —— 跳过。
                # 任务清单常常只列一类设备(某个产品的任务只关心电表),不能因为这个
                # 让整轮收集直接失败(同 pytest_generate_tests 里 setting_cases 那条先例)。
                item.add_marker(
                    pytest.mark.skip(reason=f"这份清单里没有 {'/'.join(sorted(absent))} 设备")
                )
                continue
            raise pytest.UsageError(
                f"{item.nodeid}: 标了 requires_capability 但没有按设备参数化,无法判断哪台设备的能力"
            )
        for device_id in targets:
            model = models[device_ids[device_id]]
            missing = needed - model.capabilities
            if missing:
                item.add_marker(
                    pytest.mark.skip(
                        reason=f"{device_id}(型号 {model.name})未声明能力 {sorted(missing)},跳过"
                    )
                )
                break

    _mark_topology_groups(items, manifest, device_ids)


def _mark_topology_groups(items: list, manifest: Manifest, device_ids: dict) -> None:
    """同一个 via: 下的设备打同一个 xdist_group,不同终端之间各是一组。

    配合 `-n 4 --dist loadgroup`:loadgroup 把一个组整体派给同一个 worker,
    组内因此串行、不会两条用例同时抢终端那条总线;组与组之间照常并行。
    没有 via: 的设备按自己的 id 自成一组(它不共享任何总线)。

    只对按设备参数化的用例生效:不用泛型 fixture 的入口(tests_unit、
    直接拿 composition_root 的集成测试)测的是平台自身或若干台设备的组合,
    不落在单台设备的总线上,不参与分组。
    """
    topology = {device_id: _topology_group(manifest, device_id) for device_id in device_ids}
    for item in items:
        targets = _parametrized_device_ids(item, device_ids)
        if targets:
            item.add_marker(pytest.mark.xdist_group(topology[targets[0]]))


def _topology_group(manifest: Manifest, device_id: str) -> str:
    """分组键:沿着 via: 链跟到根(那台终端)。链中间那层的设备也归到同一组。"""
    by_id = {spec["id"]: spec for spec in manifest.devices}
    spec = by_id[device_id]
    while spec.get("via") is not None:
        spec = by_id[spec["via"]]
    return spec["id"]


def _require_loadgroup(config: pytest.Config, manifest: Manifest) -> None:
    """`-n` 只负责开 worker;分组标记要被 xdist 采纳,分发模式必须是 loadgroup。

    漏掉 `--dist loadgroup` 时 xdist 退化成 load:同一终端下的两条用例可能被派到
    两个 worker,一起抢那条 485 总线 —— 这正是拓扑分组要防的随机失败,而两条路都
    跑绿、退出码 0,看不出来。所以清单里有被测设备(即存在按设备参数化的分组用例),
    开了 -n 用的却不是 loadgroup 时,直接报错,不给静默降级的机会。

    在 pytest_configure 里查而不是翻收集到的 item:xdist 的 controller 不收集
    (dsession.pytest_collection 直接返回),收集期钩子只在 worker 里跑,在那里报错
    只会碎成一句 worker 崩溃。
    """
    if hasattr(config, "workerinput"):
        return  # xdist worker 进程不判定,由 controller 报一次就够
    processes = getattr(config.option, "numprocesses", None)
    if not processes:
        return  # 单进程:分组本来就没意义
    dist = getattr(config.option, "dist", "no")
    if dist == "loadgroup":
        return
    if not any(spec["role"] == DUT_ROLE for spec in manifest.devices):
        return  # 没有被测设备:没有分组可失效
    raise pytest.UsageError(
        f"设备清单里有按设备参数化的用例会打 xdist_group 分组,但分发模式是 {dist!r}"
        f"(-n {processes}),分组不会生效:同一总线上的设备可能被两个 worker 同时访问。"
        "改成 `pytest -n <N> --dist loadgroup`;单进程跑就去掉 -n。"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """开了 -n 却不带 --dist loadgroup 时开跑前就报错(判定理由见 _require_loadgroup)。"""
    _require_loadgroup(config, _manifest(config))


def _parametrized_device_ids(item: pytest.Item, device_ids: dict) -> list[str]:
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return []
    return [v for v in callspec.params.values() if isinstance(v, str) and v in device_ids]


@pytest.fixture(autouse=True)
def _record_parallel_assignment(request):
    """TESTPLATFORM_PARALLEL_LOG 指到文件时,把这条用例落在哪个 worker、测的哪台设备、
    挂在哪台终端下追加一行 CSV。并行分组是否真的生效靠这份记录实测,
    不靠"跑绿了"推断。不设这个变量时什么都不做,正常运行行为不变。
    """
    yield
    log = os.environ.get("TESTPLATFORM_PARALLEL_LOG")
    if log is None:
        return
    by_id = {spec["id"]: spec for spec in _manifest(request.config).devices}
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    with open(log, "a", encoding="utf-8") as stream:
        for device_id in _parametrized_device_ids(request.node, by_id):
            stream.write(f"{worker},{device_id},{by_id[device_id].get('via', '')}\n")


def _make_type_fixture(device_type: str):
    """泛型 fixture:本轮参数化给哪台设备的 id,就取哪台设备的 driver,
    并保证它挂的上游(终端)已经连好 —— 下游设备自己不建物理连接,用例里也不手写连接。

    上游连不上 = 这一条用例没有有效结论(skip + 固定前缀),不是这台设备不合格:
    通路没通,被测设备还没被验过。终端自己的 connect() 仍由用例调,失败按它自己不合格算。
    """

    def _driver(request, composition_root):
        try:
            return composition_root.get_ready(request.param)
        except UpstreamFault as exc:
            pytest.skip(f"{TEST_INVALID_PREFIX}{exc}")

    return pytest.fixture(name=device_type)(_driver)


def _make_type_id_fixture(device_type: str):
    """`<类型>_id` 只回答当前跑的是哪台设备,取值跟着泛型 fixture 走,
    保证同一个用例里两者永远指向同一台设备。"""

    def _device_id(request):
        return request.getfixturevalue(device_type).device.id

    return pytest.fixture(name=f"{device_type}_id")(_device_id)


for _device_type in registered_types():
    globals()[_device_type] = _make_type_fixture(_device_type)
    globals()[f"{_device_type}_id"] = _make_type_id_fixture(_device_type)
del _device_type


# ---- 具名 fixture:只用于"必须指定某一个具体实例"的用例 ----
#
# 取不到设备时跳过并说明(device_or_skip):这份清单里没有这台设备就是没有这条用例,
# 不是平台 bug —— KeyError 会被读成后者。tests_scenario/ 与 tests_integration/ 里
# 在用例体内点名的设备用同一个 helper,同一个措辞。


@pytest.fixture()
def inverter_01(composition_root):
    return device_or_skip(composition_root, "inv_01")


@pytest.fixture()
def battery_01(composition_root):
    return device_or_skip(composition_root, "bat_01")


@pytest.fixture()
def inverter_02_via_ipc(composition_root):
    """经工控机网关接入的逆变器——对测试用例来说用法和普通逆变器毫无区别,
    这正是"消费者定义接口"想要达到的效果:测试代码感知不到接入方式的差异。"""
    return device_or_skip(composition_root, "inv_02_via_ipc")


@pytest.fixture()
def grid_sim_01(composition_root):
    """周边设备(电网模拟器)。它不参与契约测试展开,用例按 id 取同一个对象——
    多台被测设备共用这一台,清单里只声明一个 support 实例。"""
    return device_or_skip(composition_root, "grid_sim_01")
