"""
pytest 插件:负责命令行参数 + 组合根的会话级装配 + "数据向下/事件向上"
这条原则在 pytest 层面的完整闭环。

composition_root 这个 fixture 是整个测试会话里唯一一处调用
CompositionRoot.build() 的地方——这是"组合根"原则在 pytest 世界里的落地:
不管测试文件写了多少个、测试用例有多少条,具体设备实现只在这里被装配一次,
然后"向下"注入到每一条用例。

event_sink 则是"事件向上"那一半的落地:
聚合根产生的事件,经过 driver(它不碰事件,只是透传 .device),
由设备测试层的 autouse fixture(tests/conftest.py 里的
_drain_device_events_after_test)在每条用例结束后"向上"收集进 event_sink,
最终在整个会话结束时由 pytest_terminal_summary 统一打印到终端——
事件从最底层的聚合根一路冒泡到最顶层的会话报告,中间每一层
(driver、application.event_relay、这个插件)都不需要知道事件
最终会被谁、以什么方式呈现,只管把它继续往上传。

收集动作不放在这个插件里:插件对整个套件生效,而 tests_unit/ 测的是
聚合根自身,必须能在没有任何设备清单的情况下跑通。

筛选也不在用例里自己写:--select(型号 / 能力 / 标签 / 编号)与清单里的
test_matrix 就在收集阶段一次性决定"这轮跑哪些用例",用例一行 if 都不用加。
--lf 是 pytest 自带的,本插件不碰。

这一轮的范围(哪些用例被收集、哪些真正执行过、落在哪些设备上、哪些被筛掉 / 跳过及原因、
哪些失败了)写进证据目录的 run-scope.json:证据里只有真跑过的东西,"没跑"不落成记录就只能靠缺席去猜。
失败也要落范围记录:用例在写证据之前就崩了时,证据里一条读数都没有,归因只读证据就看不见它 ——
那正是"pytest 红、归因 clean"的来路。这里只记范围,不记判定。
聚合根不变量被违反时(平台自己把自己搞崩了)另落一条 platform_error 证据 ——
只读 report 里的异常,不改写任何 report。
"""
from __future__ import annotations

import pytest

from testplatform.cases import Case, case_of, case_param_of, parse_select, set_case_of
from testplatform.clauses import ClauseConfigError, ClauseRegistry, load_clauses
from testplatform.config.paths import PlatformRootError
from testplatform.composition.root import TEST_INVALID_PREFIX, CompositionRoot, SupportLinkLost
from testplatform.config.loader import load_device_config
from testplatform.config.manifest import Manifest, parse_manifest
from testplatform.domain.exceptions import DeviceNotConnectedError, InvariantViolation
from testplatform.reporting.event_sinks import InMemoryEventSink
from testplatform.reporting.evidence import (
    ROUND_STATUS_FILE,
    SETPOINT_RESTORE_OPERATION,
    EvidenceSession,
)

_EVENT_SINK_KEY = pytest.StashKey[InMemoryEventSink]()
_EVIDENCE_KEY = pytest.StashKey[EvidenceSession]()
_SCOPE_KEY = pytest.StashKey[dict]()

# 平台自身把自己搞崩了:不变量被违反是本项目定义里的"平台 bug",与被测设备合格与否无关。
# DeviceFaultError 不在这里:设备进故障态是设备自己的事实。
PLATFORM_ERROR_TYPES = (InvariantViolation, DeviceNotConnectedError)

# pytest 的 short summary 也是这么分的:call 里失败是 FAILED,setup 里崩掉是 ERROR。
_FAILED_SCOPE_KEY = {"call": "failed", "setup": "errored"}

# 失败原因只留一行、最多这么多字:异常消息有时带着整份设备清单(见 KeyError)。
_REASON_LIMIT = 160


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("testplatform")
    group.addoption(
        "--device-config",
        action="store",
        default="configs/devices.yaml",
        help="设备清单 YAML 路径",
    )
    group.addoption(
        "--force-simulator",
        action="store_true",
        default=False,
        help="忽略 YAML 里每个设备的 mode 设置,强制全部使用模拟器(适合 CI 环境)",
    )
    group.addoption(
        "--no-evidence",
        action="store_true",
        default=False,
        help="不落证据(默认按会话目录 evidence/<时间戳>/ 每台被测设备留一份)",
    )
    group.addoption(
        "--select",
        action="store",
        default="",
        help=(
            "统一筛选入口,逗号分隔、可叠加(条件之间是\"且\"):"
            "model:<型号名> / capability:<能力名> / tag:<标签> / case:<编号前缀>"
        ),
    )
    group.addoption(
        "--clause-config",
        action="store",
        default=None,
        help=(
            "标准条款受控清单 YAML(条款 id → 标准 / 编号 / summary);"
            "缺省用平台自带的那份(按平台根解析);用例的 clause 必须是其中的 id 或 free: 原文"
        ),
    )
    group.addoption(
        "--evidence-dir",
        action="store",
        default=None,
        help=(
            "证据目录;缺省按会话时间戳建 evidence/<时间戳>/。"
            "任务执行器用它把这一轮的证据落到任务自己的目录里(与 task.json 同一处)"
        ),
    )


@pytest.fixture(scope="session")
def composition_root(request: pytest.FixtureRequest):
    config_path = request.config.getoption("--device-config")
    force_mode = "simulator" if request.config.getoption("--force-simulator") else None
    # 证据默认开:一轮跑完每台被测设备留一份。--no-evidence 关掉时装配与行为
    # 完全回到没有证据层的样子(不建目录、不多包一层传输)。
    root = CompositionRoot.build(
        config_path, force_mode=force_mode, evidence=_evidence_of(request.config)
    )
    yield root
    root.teardown()


@pytest.fixture(scope="session")
def evidence_session(request: pytest.FixtureRequest) -> EvidenceSession:
    """这一轮的证据目录:用例自己装配组合根时交给它,证据才和会话其余部分落在同一处。"""
    evidence = _evidence_of(request.config)
    if evidence is None:
        pytest.skip("这一轮没开证据(--no-evidence)")
    return evidence


def _evidence_of(config: pytest.Config) -> EvidenceSession | None:
    """这一轮的证据会话:第一次被用到时建,存进 stash 让 sessionfinish 写的范围与轮次状态
    落进同一个目录。`--evidence-dir` 给了就用它(任务执行器把 task.json 与每设备 JSONL 放同一处),
    否则按会话时间戳新建。--no-evidence 时为 None:行为回到没有证据层的样子。"""
    evidence = config.stash.get(_EVIDENCE_KEY, None)
    if evidence is not None or config.getoption("--no-evidence"):
        return evidence
    directory = config.getoption("--evidence-dir")
    evidence = EvidenceSession(directory) if directory else EvidenceSession.open()
    config.stash[_EVIDENCE_KEY] = evidence
    return evidence


@pytest.fixture(scope="session")
def event_sink(request: pytest.FixtureRequest) -> InMemoryEventSink:
    """整个会话只造一个 sink,存进 config.stash 以便 pytest_terminal_summary
    这个钩子函数(它拿不到 fixture,只能拿到 config)也能访问到同一个实例。"""
    sink = InMemoryEventSink()
    request.config.stash[_EVENT_SINK_KEY] = sink
    return sink


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """全局开关:加了 --force-simulator 时,所有标了 @pytest.mark.real_hardware
    的用例统一跳过——这类用例(比如响应延迟)在模拟器下跑没有意义,
    与其让它们"假装通过",不如明确跳过并说明原因。

    注意这只是"粗粒度"的开关(基于命令行参数,不管具体是哪台设备);
    "细粒度"的判断(这台具体设备实际是不是 real 模式)要在用例内部
    用 composition_root.mode_of(device_id) 自己决定要不要 pytest.skip(),
    因为收集阶段(这个钩子执行的时候)composition_root 还没有被装配出来。
    test_inverter.py::test_inverter_responds_within_protocol_timeout 演示了这个写法。
    """
    if config.getoption("--force-simulator"):
        skip_marker = pytest.mark.skip(reason="real_hardware 用例在 --force-simulator 下自动跳过")
        for item in items:
            if "real_hardware" in item.keywords:
                item.add_marker(skip_marker)

    _apply_param_case_identity(config, items)
    _validate_clause_references(config, items)
    _validate_case_ids_are_unique(items)
    _apply_case_selection(config, items)


def _apply_param_case_identity(config: pytest.Config, items: list) -> None:
    """把"身份由参数给"的用例(``case_from_param``)逐节点 resolve 成各自的 ``@case`` 身份。

    527 条设置类条款只有一个测试函数:编号与 clause 从清单的 ``setting_cases`` 取,
    参数值就是条款编号。resolve 出来的身份直接挂到**节点**上(不是函数上),
    因为参数不同 = 条款不同 —— 共用一份身份的话,证据与归因分不清谁是谁。

    必须在唯一性校验与筛选之前跑:两者都从 ``case_of`` 读身份。
    """
    declaring = [item for item in items if case_param_of(item) is not None]
    if not declaring:
        return
    setting_cases = _manifest_of(config).setting_cases
    for item in declaring:
        declaration = case_param_of(item)
        case_id = _param_value(item, declaration.param)
        if case_id is None and not setting_cases:
            # 清单里没有 setting_cases:conftest 已给这条节点标了 skip,这里不再当错误。
            continue
        entry = setting_cases.get(case_id)
        if entry is None:
            raise pytest.UsageError(
                f"{item.nodeid}: 参数 {declaration.param}={case_id!r} 在清单的 setting_cases 里没有,"
                f"已声明的条款编号: {sorted(setting_cases)}"
                f"(条款写在配置的 setting_cases 段里,一条一行)"
            )
        set_case_of(item, Case(id=entry.id, clause=entry.clause, tags=declaration.tags))


def _param_value(item: pytest.Item, param: str):
    """这条节点在这个参数上取的值。声明的参数没被参数化 = 写错了(不会自己凭空长出一个)。"""
    callspec = getattr(item, "callspec", None)
    params = {} if callspec is None else callspec.params
    if param not in params:
        raise pytest.UsageError(
            f"{item.nodeid}: case_from_param 声明的参数 {param!r} 没有被参数化"
            f"(这条节点的参数: {sorted(params)})"
        )
    return params[param]


def _clauses_of(config: pytest.Config) -> ClauseRegistry:
    """这一轮用的条款清单。清单写坏 / 不存在是**用法错误**:整轮收集就停,不跑一半。"""
    try:
        return load_clauses(config.getoption("--clause-config"))
    except (ClauseConfigError, PlatformRootError) as exc:
        raise pytest.UsageError(str(exc)) from exc


def _validate_clause_references(config: pytest.Config, items: list) -> None:
    """每条用例的条款必须是清单里的 id,或显式写成 `free: <原文>`。

    放在收集期(不是用例里):拼错的标准编号会静默变成一个"看着像条款"的字符串,
    报告按条款聚合时才发现对不上,而那时结论已经发出去了。

    自由文本只留 `free:` 一个入口 —— 兼容不能是"随便写文本也收",否则没人会迁移。
    """
    registry = _clauses_of(config)
    unknown = []
    for item in items:
        identity = case_of(item)
        if identity is None or registry.has(identity.clause) or registry.is_free(identity.clause):
            continue
        unknown.append((item.nodeid, identity.clause))
    if not unknown:
        return
    listed = "\n".join(f"  {nodeid}: clause={clause!r}" for nodeid, clause in unknown[:10])
    more = "" if len(unknown) <= 10 else f"\n  …另有 {len(unknown) - 10} 条"
    raise pytest.UsageError(
        f"{len(unknown)} 条用例引用了不在条款清单里的 clause"
        f"(清单: {config.getoption('--clause-config')}):\n{listed}{more}\n"
        f"可用条款 id: {', '.join(registry.ids())}\n"
        f"确实不是受控条款时写成 clause=\"free: <原文>\"(迁移期旧写法,报告里按自由文本单独聚合)。"
    )


def _validate_case_ids_are_unique(items: list) -> None:
    """同一个 @case 编号挂在两个不同的测试函数上 = 报错:编号是归因与证据的唯一身份。

    参数化产生的多个节点共用一个编号是合法的(它们本来就是同一条用例),所以按**测试函数**
    归并,不按 nodeid 去重 —— 按 nodeid 比会把正常的参数化判成重号。
    放在筛选之前:被 --select 筛掉的重复编号同样是重复,不能靠筛掉它来瞒过去。
    """
    seen: dict[str, tuple[object, str]] = {}
    for item in items:
        identity = case_of(item)
        if identity is None:
            continue
        function = _test_function_of(item)
        first = seen.get(identity.id)
        if first is None:
            seen[identity.id] = (function, item.nodeid)
            continue
        if first[0] is function:
            continue
        raise pytest.UsageError(
            f"@case id {identity.id} 挂在两个不同的测试函数上:{first[1]} 与 {item.nodeid}。"
            f"编号是归因与证据的唯一身份,不能重复;参数化同一条用例不产生新编号。"
        )


def _test_function_of(item: pytest.Item):
    """参数化只改 nodeid、不改测试函数对象:按它归并才能把参数化节点看成同一条用例。"""
    return getattr(item, "function", None) or item.obj


def _apply_case_selection(config: pytest.Config, items: list) -> None:
    """统一的筛选入口:--select(型号 / 能力 / 标签 / 编号)+ 清单里的适用矩阵。

    两者都回答"这轮实际跑哪些用例",同一条用例要同时满足才留下。
    筛完一条不剩时报错而不是报 0 passed:0 条的绿色会让人以为测过了。

    只重跑上次失败用 pytest 自带的 --lf / --last-failed,这里不另实现缓存。
    注意 --lf 按测试节点 id 缓存(如 tests/test_meter.py::test_meter_connects[meter_01]),
    设备清单一旦变了(增删设备、改 id、改型号),节点 id 就跟着变,旧缓存对不上新节点:
    缓存失配,表现成"上次失败的用例这轮没被选中"。换清单后先跑一次全量,别拿旧缓存当结论。
    """
    if not items:
        return
    manifest = _manifest_of(config)
    selectors = _selectors_of(config)
    matrix = manifest.test_matrix
    if not selectors and not matrix:
        return

    device_model = _device_model(manifest)
    _validate_matrix_references(config, manifest, items, device_model)
    _validate_device_cases(config, manifest, items, device_model)

    kept = [
        item
        for item in items
        if _selected(item, selectors, manifest, device_model)
        and _allowed_by_matrix(item, matrix, manifest, device_model)
    ]
    kept_ids = {id(item) for item in kept}
    _note_deselected(
        config,
        [
            (item, _deselected_reason(item, selectors, matrix, manifest, device_model))
            for item in items
            if id(item) not in kept_ids
        ],
    )
    if not kept:
        raise pytest.UsageError(
            _nothing_selected_message(selectors, matrix, manifest, items, device_model)
        )
    items[:] = kept


def _scope_of(config: pytest.Config) -> dict:
    """这一轮的范围记录:收集期就攒起来,sessionfinish 一次写进证据目录。

    executed 单列一份:它是"真正跑过"的唯一来源。用 collected − skipped 推算会把
    `pytest -x` 中途停下、会话被打断时没跑到的用例算成跑过。
    """
    return config.stash.setdefault(
        _SCOPE_KEY,
        {
            "collected": [],
            "deselected": [],
            "skipped": [],
            "executed": [],
            # failed = call 阶段失败,errored = setup 阶段崩掉:与 pytest 的 short summary 同口径。
            "failed": [],
            "errored": [],
        },
    )


def _note_deselected(config: pytest.Config, dropped: list) -> None:
    """被 --select / 适用矩阵筛掉的用例:它们没跑,归因里必须是独立一类,不能读成通过。"""
    scope = _scope_of(config)
    for item, reason in dropped:
        scope["deselected"].append(_scope_entry(item, reason))


def _deselected_reason(item, selectors, matrix, manifest: Manifest, device_model: dict) -> str:
    if not _selected(item, selectors, manifest, device_model):
        written = ",".join(f"{s.kind}:{s.value}" for s in selectors)
        return f"不满足 --select {written}"
    return "不在该型号的适用矩阵里"


def pytest_collection_finish(session: pytest.Session) -> None:
    """收集结束:记下这一轮真的会跑哪些用例,以及收集期就被跳过的那些与原因。

    按设备参数化的用例还要记下它落在哪些设备上(devices):归因层据此才分得清
    "这台设备不在这一轮范围"与"这台设备在范围内却没测到"。设备范围在收集期就已知,
    这里只是把它写下来 —— 仍然只记范围,不记判定。
    """
    scope = _scope_of(session.config)
    device_model = _device_model(_manifest_of(session.config)) if session.items else {}
    scope["collected"] = [
        _scope_entry(item, devices=_devices_of(item, device_model)) for item in session.items
    ]
    for item in session.items:
        reason = _collected_skip_reason(item)
        if reason is not None:
            scope["skipped"].append(_scope_entry(item, reason))


def _collected_skip_reason(item: pytest.Item) -> str | None:
    for marker in item.iter_markers(name="skip"):
        return marker.kwargs.get("reason") or "跳过"
    return None


def _scope_entry(item: pytest.Item, reason: str | None = None, devices: list | None = None) -> dict:
    """一条范围记录:哪条用例(节点 id + 编号),落在哪些设备上,没跑的话为什么。

    判定结果不在这里。devices 只有按设备参数化的用例才有;场景用例参与方在
    scenario-*.json 里,收集期还写不出来。
    """
    identity = case_of(item)
    entry = {"nodeid": item.nodeid, "case": identity.id if identity else None}
    if devices:
        entry["devices"] = list(devices)
    if reason is not None:
        entry["reason"] = reason
    return entry


def _manifest_of(config: pytest.Config) -> Manifest:
    return parse_manifest(load_device_config(config.getoption("--device-config")))


def _device_model(manifest: Manifest) -> dict:
    """设备 id → 型号名:筛选用它查型号,记范围用它认出参数化传进来的设备 id。"""
    return {spec["id"]: spec["model"] for spec in manifest.devices}


def _devices_of(item: pytest.Item, device_ids: dict) -> list[str]:
    """这条用例落在哪些设备上:间接参数化传进来的值就是设备 id。"""
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return []
    return sorted(v for v in callspec.params.values() if isinstance(v, str) and v in device_ids)


def _selectors_of(config: pytest.Config):
    try:
        return parse_select(config.getoption("--select"))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


def _device_profile(item: pytest.Item, manifest: Manifest, device_model: dict) -> tuple[set, set]:
    """这条用例落在哪些型号上、这些型号声明了哪些能力。

    只有按设备类型参数化的用例才看得出设备(间接参数化传进来的值就是设备 id);
    用 composition_root 直接取设备的集成测试看不出型号,因此不受这些条件约束。
    """
    targets = _devices_of(item, device_model)
    model_names = {device_model[device_id] for device_id in targets}
    capabilities = {c for name in model_names for c in manifest.models[name].capabilities}
    return model_names, capabilities


def _selected(item: pytest.Item, selectors, manifest: Manifest, device_model: dict) -> bool:
    if not selectors:
        return True
    model_names, capabilities = _device_profile(item, manifest, device_model)
    identity = case_of(item)
    return all(
        selector.matches(
            identity=identity,
            nodeid=item.nodeid,
            model_names=model_names,
            capabilities=capabilities,
        )
        for selector in selectors
    )


def _allowed_by_matrix(item: pytest.Item, matrix, manifest: Manifest, device_model: dict) -> bool:
    """矩阵决定某型号实际跑哪些用例。不在矩阵里的型号不受限(全跑)。"""
    if not matrix:
        return True
    model_names, _ = _device_profile(item, manifest, device_model)
    restricted = [matrix[name] for name in model_names if name in matrix]
    if not restricted:
        return True
    identity = case_of(item)
    return any(
        entry.matches(identity=identity, nodeid=item.nodeid)
        for entries in restricted
        for entry in entries
    )


def _validate_device_cases(
    config: pytest.Config, manifest: Manifest, items: list, device_model: dict
) -> None:
    """按设备参数化的用例必须能被矩阵表达:有 @case 身份,且至少一个型号的矩阵覆盖它。

    矩阵只认编号和标签。一条按设备参数化却没打 @case 的用例,矩阵无法表达它的适用范围;
    一条哪个型号的矩阵都没提到的用例,只要它落的型号被矩阵限制着就会被 _allowed_by_matrix
    静默筛掉——报告全绿,而那个型号从此不再测这条。两种都在收集阶段直接报错。

    覆盖要求是"至少一个型号"而不是"每个":不同型号跑不同子集是合法的。
    只在整轮收集时检查,理由同 _validate_matrix_references:只跑一个文件时
    别的文件的用例根本没被收集,拿矩阵全集去比会误报。
    """
    matrix = manifest.test_matrix
    if not matrix:
        return
    entries = [entry for model_entries in matrix.values() for entry in model_entries]
    whole_run = not config.getoption("file_or_dir")
    for item in items:
        model_names, _ = _device_profile(item, manifest, device_model)
        if not model_names:
            continue
        identity = case_of(item)
        if identity is None:
            raise pytest.UsageError(
                f"{item.nodeid}: 按设备参数化但没有 @case 身份,矩阵无法表达它的适用范围,"
                f"它会被静默跳过。给它补 @case(id=..., tags=[...])。"
            )
        if not whole_run:
            continue
        restricted = [name for name in model_names if name in matrix]
        covered = len(restricted) < len(model_names) or any(
            entry.matches(identity=identity, nodeid=item.nodeid) for entry in entries
        )
        if not covered:
            raise pytest.UsageError(
                f"{item.nodeid}: 用例 {identity.id} 不属于任何型号的适用矩阵"
                f"(它落在 {'/'.join(sorted(model_names))} 上),会被静默跳过不跑。"
                f"把它的编号或标签加进 test_matrix,或把它落的型号从 matrix 里拿掉(不受限=全跑)。"
            )


def _validate_matrix_references(
    config: pytest.Config, manifest: Manifest, items: list, device_model: dict
) -> None:
    """矩阵里引用的编号 / 标签必须真有对应用例,否则那个型号会静默少跑用例。

    只在整轮收集时校验:``pytest tests/test_meter.py`` 这种只跑一个文件的用法下,
    别的文件的用例根本没被收集,拿它去比会误报。
    """
    if not manifest.test_matrix or config.getoption("file_or_dir"):
        return
    for model_name, entries in manifest.test_matrix.items():
        scoped = [
            item
            for item in items
            if model_name in _device_profile(item, manifest, device_model)[0]
        ]
        if not scoped:
            continue
        for entry in entries:
            if not any(entry.matches(identity=case_of(item), nodeid=item.nodeid) for item in scoped):
                raise pytest.UsageError(
                    f"test_matrix 型号 {model_name!r} 引用的 {entry.kind}:{entry.value} 对不上任何用例:"
                    f"该型号下没有这个编号/标签的用例。矩阵写错会让这个型号静默少跑用例。"
                )


def _nothing_selected_message(
    selectors, matrix, manifest: Manifest, items: list, device_model: dict
) -> str:
    lines = ["--select / 适用矩阵筛完没有任何用例可跑(条件之间是\"且\"):"]
    for selector in selectors:
        why = _why_matches_nothing(selector, manifest, items, device_model)
        lines.append(f"  {selector.kind}:{selector.value} —— {why}")
    if matrix:
        described = "; ".join(
            f"{name} → {[f'{entry.kind}:{entry.value}' for entry in entries]}"
            for name, entries in matrix.items()
        )
        lines.append(f"  适用矩阵也在生效: {described}")
    return "\n".join(lines)


def _why_matches_nothing(selector, manifest: Manifest, items: list, device_model: dict) -> str:
    """说清是"清单里没有"/"没有用例带它",而不是只说一句没选到。"""
    if selector.kind == "model":
        if selector.value not in manifest.models:
            return f"清单里没有这个型号(已声明型号: {sorted(manifest.models)})"
        if selector.value not in set(device_model.values()):
            return "清单里没有这个型号的设备实例"
        return "清单里有这个型号的设备,但没有用例同时满足其它条件"
    if selector.kind == "capability":
        declared = {
            name
            for name in set(device_model.values())
            if selector.value in manifest.models[name].capabilities
        }
        if not declared:
            return "没有设备声明这项能力"
        return f"声明了这项能力的型号 {sorted(declared)} 没有用例同时满足其它条件"
    if selector.kind == "tag":
        tags = sorted(
            {tag for item in items if (identity := case_of(item)) for tag in identity.tags}
        )
        return f"没有用例带这个标签(这一轮收集到的标签: {tags})"
    ids = sorted({identity.id for item in items if (identity := case_of(item))})
    return f"没有用例的编号以它开头(这一轮收集到的编号: {ids})"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """周边设备在收帧阶段断链 = 这一条用例没有有效结论:转成 skip,不是 fail。

    这是收帧期唯一的转换点,和连接期的 connect_support_or_skip 用同一个前缀、同一个结论;
    转换放在插件里而不是 tests/conftest.py,因为集成层与场景层也要生效。
    只接 SupportLinkLost —— 它只由 role: support 的传输抛;被测设备断链抛的仍是
    ConnectionError,照旧算设备不合格。skip 不改退出码:整轮继续跑完。
    """
    try:
        return (yield)
    except SupportLinkLost as exc:
        pytest.skip(f"{TEST_INVALID_PREFIX}{exc}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """会话结束时把本轮状态与范围写成机器可读文件(没开证据记录就没有目录可写)。

    写完就关句柄:会话被中断(KeyboardInterrupt / Ctrl-C)、有用例崩了,
    这个钩子照样会被调用,所以不需要另挂 atexit —— 而且每条记录写完就 flush,
    就算这个钩子也没跑到,已写的证据也不会丢。
    """
    evidence = session.config.stash.get(_EVIDENCE_KEY, None)
    if evidence is None:
        return
    scope = session.config.stash.get(_SCOPE_KEY, None)
    try:
        if scope is not None:
            evidence.write_run_scope(scope)
        evidence.write_round_status()
    finally:
        evidence.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """只读 report:平台自己把自己搞崩时落一条证据,运行期的跳过与失败记进本轮范围。

    读 report 是允许的,改写 report 仍然禁止 —— 这里不改任何结论,
    只是给"不变量被违反"留一条现场记录,让归因能指回是哪台设备、哪条用例。
    """
    report = yield
    if report.when == "call":
        evidence = item.config.stash.get(_EVIDENCE_KEY, None)
        if evidence is not None and call.excinfo is not None:
            exc = call.excinfo.value
            if isinstance(exc, PLATFORM_ERROR_TYPES):
                evidence.record_platform_error(
                    nodeid=item.nodeid, case=case_of(item), cause=exc
                )
    if report.when in ("setup", "call"):
        if report.skipped:
            _note_skip(item, report)
        elif report.outcome == "failed":
            _note_failed(item, report)
    _note_executed(item.config, report)
    return report


def _note_skip(item: pytest.Item, report) -> None:
    """跳过也是范围事实。收集期标记的跳过在 pytest_collection_finish 已经记过,
    这里只补运行期那部分:fixture 在 setup 里 skip(比如清单里没有这台设备)与用例体内的跳过。"""
    scope = _scope_of(item.config)
    if any(entry["nodeid"] == item.nodeid for entry in scope["skipped"]):
        return
    scope["skipped"].append(_scope_entry(item, _runtime_skip_reason(report)))


def _note_failed(item: pytest.Item, report) -> None:
    """失败只记范围事实:哪条用例、哪个编号、一行原因。

    不记判定 —— 用例崩在写证据之前时证据里没有读数,归因无从判定设备合不合格;
    它要做的只是别把这条失败弄丢(单列一栏「未落证据的失败」)。
    """
    bucket = _scope_of(item.config)[_FAILED_SCOPE_KEY[report.when]]
    if any(entry["nodeid"] == item.nodeid for entry in bucket):
        return
    bucket.append(_scope_entry(item, _failure_reason(report)))


def _failure_reason(report) -> str:
    """一行原因:longrepr 有几十行,取最后一行断言/异常文本就够指认,超长截断。

    异常消息里可能带着整份设备清单(KeyError),不截断的话这一行就是几 KB。
    """
    lines = [line.strip() for line in (getattr(report, "longreprtext", "") or "").splitlines()]
    lines = [line for line in lines if line]
    reason = next((line[2:].strip() for line in reversed(lines) if line.startswith("E ")), "")
    reason = reason or (lines[-1] if lines else "失败原因未记录")
    return reason if len(reason) <= _REASON_LIMIT else reason[: _REASON_LIMIT - 1] + "…"


def _note_executed(config: pytest.Config, report) -> None:
    """记下真正执行过的用例:进了 call 并跑出结论(通过 / 失败),或 setup 就报错。

    call 报告是 skipped 的用例不记:它已经在 skipped 里占了一条,两边都算会让
    「收集 = 执行 + 跳过 + 没跑到」这个加法对不上。中断时没收到报告的用例自然
    不会进来 —— 这一行才是"这一轮跑了多少条"的诚实来源。
    """
    concluded = report.when == "call" and not report.skipped
    if not (concluded or (report.when == "setup" and report.failed)):
        return
    executed = _scope_of(config)["executed"]
    if report.nodeid not in executed:
        executed.append(report.nodeid)


def _runtime_skip_reason(report) -> str:
    """skip 报告里长这样:(文件, 行号, 原因)。拿不到原因也不能丢这条记录。"""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_report_header(config: pytest.Config) -> str:
    if config.getoption("--force-simulator"):
        mode_desc = "全部强制模拟器(--force-simulator)"
    else:
        mode_desc = "按 YAML 配置文件各自决定"
    return f"testplatform: 设备配置={config.getoption('--device-config')}, 运行模式={mode_desc}"


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """事件冒泡的终点:整个会话期间所有设备产生过的事件,
    在这里统一打印成一个独立的报告小节。这条用例、那条用例谁产生了
    什么事件,这个钩子完全不关心——它只认识 event_sink 里攒下来的结果,
    这正是"事件向上"想要的效果:上层不需要追踪事件的来源细节,
    只需要在合适的汇聚点统一处理。"""
    _report_link_lost(terminalreporter, config)

    evidence = config.stash.get(_EVIDENCE_KEY, None)
    if evidence is not None:
        terminalreporter.write_line(
            f"一轮归因(哪些设备不合格 / 哪些测试无效 / 断在哪一步): "
            f"python tools/attribute_round.py {evidence.directory}"
        )

    sink = config.stash.get(_EVENT_SINK_KEY, None)
    if not sink or not sink.events:
        return
    terminalreporter.section("设备事件汇总(数据向下 / 事件向上)")
    for event in sink.events:
        terminalreporter.write_line(f"  {event}")


def _report_link_lost(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """周边设备不合作必须显眼:受影响的用例是 skip,退出码不变,一眼扫过去很容易漏。

    两种来路分开写:收帧阶段断链是查线,台架复位失败是查复位命令。
    合成一句“断链”,现场会按错的方向查。
    """
    evidence = config.stash.get(_EVIDENCE_KEY, None)
    if evidence is None or not evidence.link_lost:
        return
    devices = sorted({entry["device"] for entry in evidence.link_lost})
    terminalreporter.section(
        f"本轮存在{_link_lost_title(evidence.link_lost)} —— 结论需复核",
        sep="=",
        red=True,
        bold=True,
    )
    for entry in evidence.link_lost:
        terminalreporter.write_line(
            f"  {entry['device']}  {entry['at']}  {_link_lost_action(entry)}"
            f"({entry['failure']}): {entry['message']}",
            red=True,
        )
    terminalreporter.write_line(
        f"  共 {len(evidence.link_lost)} 次,涉及设备: {', '.join(devices)}", bold=True
    )
    terminalreporter.write_line(
        "  受影响的用例已按“测试无效”跳过(不是设备不合格),被测设备的结论仍需复核"
    )
    terminalreporter.write_line(f"  机器可读记录: {evidence.directory / ROUND_STATUS_FILE}")


def _link_lost_title(entries: list) -> str:
    restore = any(entry["operation"] == SETPOINT_RESTORE_OPERATION for entry in entries)
    broken = any(entry["operation"] != SETPOINT_RESTORE_OPERATION for entry in entries)
    return "断链与台架复位失败" if restore and broken else "台架复位失败" if restore else "断链"


def _link_lost_action(entry: dict) -> str:
    """一条记录到底发生了什么事:断链(查线)还是复位失败(查复位命令)。"""
    if entry["operation"] == SETPOINT_RESTORE_OPERATION:
        return "恢复初始设定失败"
    return f"{entry['operation']} 时断链"
