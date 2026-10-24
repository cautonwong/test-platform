"""
组合根 —— 整个项目里唯一知道"怎么把 devices.yaml 的一条配置,
翻译成一个真正可用的 driver 对象"的地方。

它只做五件事:
    1. 读配置并校验三段式清单(委托给 config/loader.py + config/manifest.py)
    2. 把型号引用的具名判据集 + 该设备的行内覆盖,合并成判据值对象
    3. 按 access + mode 查 transport 注册表,造出 transport
    4. 按型号上的 (设备类型, 规约) 查 driver 注册表,造出 driver(driver 内部顺带创建聚合根)
    5. 开了证据记录时,给交出去的传输包一层记录(reporting/evidence.py);
       驱动层与领域层照旧 send/receive,不知道外面多了一层写字的手

型号把点表 / 能力 / 判据集声明一次,设备实例只写 id、型号名和接入位置;
装配完成后从这里能问到每台设备的读数键与能力(用例按它们取数、跳过)。

判据的实例化只在这里发生:domain 里没有任何一个默认阈值,
换 60Hz 机型只改清单,不改代码。

新增协议或新增设备类型都不需要改这个文件——只需要在对应的
transport/xxx.py 或 drivers/xxx.py 里用装饰器注册一个工厂函数。

周边设备(role: support)与被测设备共用同一套型号 / 点表 / 驱动 / 注册表,
区别只在装配顺序和结论:connect_support() 在装配时先连周边设备,
被测设备留到用例里连;周边设备连不上抛 SupportEquipmentFault,
结论是"测试无效"而不是"被测设备不合格"。
收帧阶段的断链同样按角色归属:周边设备的传输外面多套一层 _SupportLinkGuard,
它把 ConnectionError 换成 SupportLinkLost(这一条用例测试无效);被测设备不套,
链路就是它自身的一部分,异常原样上抛,仍按设备不合格处理。

写原语上线后,收尾不能只是断开:一条用例把台架设到 0V / 把设备写进工厂模式之后失败,
后面所有用例读到的都是错值,而报告看不出原因。所以复位按 role 分层:
周边设备回到清单声明的 initial_setpoint(失败 = 测试无效);被测设备只做用例自己声明的
收尾(见 reporting/scenario.py 的 ScenarioCleanup)—— 平台不替它猜,也不在 teardown 里
往它身上写:整轮结束时设备被人动过,比留在工厂模式更难查。

接入从属关系(`via:`)也在这里落地:装配按拓扑序(终端先、下游设备后),
下游设备拿到的是借上游那条总线的传输 —— 它不建物理连接,每次请求帧里
带自己的 bus_address 由协议层选中从站。

下面这组 import 本身就是组合根职责的一部分:
import 这些模块会触发模块顶层的 @register_transport / @register_device
装饰器真正执行一次,把具体实现注册进对应的 registry。
除了这里和实现文件本身,代码库里不应该再出现对这些具体实现模块的 import——
domain/ 和 application/ 永远只认识 interfaces.py 里的 Protocol。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Protocol, runtime_checkable

# --- 触发注册:清单在 composition/registrations.py(只注册、不装配) ---
# 组合根要认识所有具体类型,所以它 import 那份清单;只想知道"注册表里有什么"的消费者
# (资产目录 / 工具)也 import 它,而不必把组合根和报告层一起拖进来。
from testplatform.composition import registrations  # noqa: F401

from testplatform.config.loader import load_device_config
from testplatform.config.manifest import (
    DEFAULT_ROLE,
    SUPPORT_ROLE,
    DerivedCriterion,
    Model,
    parse_manifest,
)
from testplatform.domain.criteria import CriteriaProfile, Criterion, Severity
from testplatform.domain.device import DeviceState
from testplatform.drivers.registry import build_device
from testplatform.reporting.evidence import SETPOINT_RESTORE_OPERATION, EvidenceSession
from testplatform.transport.registry import build_transport

# 周边设备故障的结论是"测试无效",不是"被测设备不合格"。
# 测试层把它转成 skip 时用的固定前缀;证据层靠它区分无效与不合格。
TEST_INVALID_PREFIX = "测试无效:"


class SupportEquipmentFault(RuntimeError):
    """周边设备故障:这一次测试无效。

    它和"设备不合格"是两件事:不合格由判据集决定(见 domain/criteria.py),
    这个异常意味着结论本身不成立。
    """

    def __init__(self, device_id: str, cause: BaseException) -> None:
        super().__init__(f"周边设备 {device_id!r} {self._describe()}: {cause}")
        self.device_id = device_id

    def _describe(self) -> str:
        return "连接失败"


class SupportUpstreamNotReady(SupportEquipmentFault):
    """周边设备的接入通路(via: 指向的那台终端)没就绪:这一条用例没有有效结论,
    但不是这台周边设备本身坏了。

    措辞里必须出现"上游",归因层靠它把这条和"设备坏"分开:一台挂在终端下的
    标准表连不上,先要问的是那条总线通没通,而不是先换表。
    """

    def _describe(self) -> str:
        return "的上游没就绪(接入通路没通,不是设备本身故障)"


class SupportLinkLost(SupportEquipmentFault):
    """周边设备在收帧阶段断链:这一条用例没有有效结论,但整轮照常跑下去。

    被测设备断链不走这里 —— 链路是它自身的一部分,异常原样上抛,仍按设备不合格处理。
    站哪一边只看设备清单里的 role(装配时决定装不装 _SupportLinkGuard),不看异常类型。
    """

    def _describe(self) -> str:
        return "收帧阶段断链"


class SupportRestoreFault(SupportEquipmentFault):
    """周边设备没能恢复清单声明的初始设定:这一轮没有有效结论(测试无效)。

    和"设备不合格"无关:台架停在非初始状态时,被测设备这一轮读到的东西可能已经被
    上一条用例留下的设定污染,所以不能拿它判合格与否。
    """

    def _describe(self) -> str:
        return "恢复初始设定失败"


@runtime_checkable
class SetpointRestorableDriver(Protocol):
    """能恢复初始设定的周边设备:消费者定义的接口,组合根只认这一个方法。

    setpoint 就是清单里声明的 initial_setpoint 原样交进来,键是这台设备设定值对象的
    字段名(电网模拟器是 voltage_v / freq_hz)。
    声明了 initial_setpoint 却没收下这个接口的设备在装配期就报错(见 build):
    否则复位只是"没发生",后面所有读数都被污染,而报告里看不出来。
    """

    def apply_initial_setpoint(self, setpoint: Mapping[str, float]) -> Any: ...


class UpstreamFault(RuntimeError):
    """上游设备(这台设备的接入通路)连不上:这一条用例没有有效结论。

    上游是不是 role: dut 与这里无关:对挂在下游设备上的用例来说,终端不是被测设备,
    而是它的接入通路 —— 通路没通,下游设备的结论无从谈起。所以结论是"测试无效"
    (和周边设备故障同一个前缀),既不能读成设备不合格,也不是平台自身的 bug。
    终端自己的用例连的是它自己,那条 connect() 失败仍按它不合格算。
    """

    def __init__(self, device_id: str, cause: BaseException) -> None:
        super().__init__(f"上游设备 {device_id!r} 连接失败: {cause}")
        self.device_id = device_id


class _SupportLinkGuard:
    """周边设备的传输:收帧阶段断链时把结论从"链路错误"换成"测试无效"。

    只认 ConnectionError(对端关了):它是链路不在,不是设备不回,和超时不能合并
    (见 transport/interfaces.py 的 receive 契约)。证据与汇总仍靠异常类型记 closed,
    这里只是把归属按 role 定下来,不改任何传输行为。
    """

    def __init__(self, inner: Any, device_id: str, evidence: EvidenceSession | None) -> None:
        self._inner = inner
        self._device_id = device_id
        self._evidence = evidence

    def connect(self) -> None:
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def has_client(self) -> bool:
        return self._inner.has_client()

    def send(self, data: bytes) -> None:
        try:
            self._inner.send(data)
        except ConnectionError as exc:
            self._link_lost("send", exc)

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        try:
            return self._inner.receive(timeout=timeout, expected=expected)
        except ConnectionError as exc:
            self._link_lost("receive", exc)

    def _link_lost(self, operation: str, cause: ConnectionError) -> None:
        # 记录先于转换:证据要在异常换成别的类型之前就落盘,换完就看不到原始种类了。
        if self._evidence is not None:
            self._evidence.record_link_lost(self._device_id, operation=operation, cause=cause)
        raise SupportLinkLost(self._device_id, cause) from cause


class _SharedBusTransport:
    """下游设备的传输:物理链路归 via 指向的那台终端,这里只借用它收发字节。

    connect() 只确认终端已经连上 —— 它不建立物理连接,也不把总线重开一遍,
    所以一台终端下挂多块表时,表的连接不会重复连总线;"选中从站"发生在协议层:
    每个请求帧里带的就是这块表自己的 bus_address。
    """

    def __init__(self, bus: Any, upstream: Any) -> None:
        self.bus = bus
        self._upstream = upstream

    def connect(self) -> None:
        # 终端没连就读表是平台自身的问题,不是设备不合格:直接报错。
        self._upstream.ensure_connected()

    def disconnect(self) -> None:
        """总线归终端,下游设备断开时不碰它。"""

    def has_client(self) -> bool:
        return self._upstream.state is DeviceState.CONNECTED

    def send(self, data: bytes) -> None:
        self.bus.send(data)

    def receive(self, timeout: float = 2.0, expected=None) -> bytes:
        return self.bus.receive(timeout=timeout, expected=expected)


@dataclass
class CompositionRoot:
    drivers: Dict[str, Any] = field(default_factory=dict)
    # 每台设备的物理/共享总线那一层:下游设备存的是借上游总线的代理(它的 .bus 指向上游那条)。
    # 开了证据记录时,driver 拿到的是包着它的 RecordingTransport,这里仍是它里面那一层——
    # 下游设备借总线、用例查看接入关系都认这一层。
    transports: Dict[str, Any] = field(default_factory=dict)
    # 下面三个字典是给测试层用的"元信息",不参与任何业务逻辑,
    # 纯粹是为了让用例可以按类型/能力筛选设备、按实际运行模式跳过某些用例。
    device_types: Dict[str, str] = field(default_factory=dict)
    actual_modes: Dict[str, str] = field(default_factory=dict)
    models: Dict[str, Model] = field(default_factory=dict)
    roles: Dict[str, str] = field(default_factory=dict)
    # 周边设备声明的初始设定(设备 id → {设定项名: 数值}):teardown 与用例收尾恢复到这里。
    # 被测设备不会出现在这里(清单在解析时就拒了),所以复位永远不往被测设备身上写。
    initial_setpoints: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # 下游设备 → 它挂的那台上游设备(via: 链的一环)。取设备的人靠它先把上游连好。
    upstreams: Dict[str, str] = field(default_factory=dict)
    # 每台设备被行内覆盖掉的判据物理量。判据集名(CriteriaProfile.name)是基础名,
    # 光看名字看不出用了覆盖,证据层靠这里补上"实际生效的是基础集 + 哪些被覆盖"。
    criteria_overrides: Dict[str, tuple] = field(default_factory=dict)
    # 证据会话:None = 不记录,平台行为与没有证据层时完全一致。
    evidence: EvidenceSession | None = None
    # 派生判据(跨设备算出来的量的区间):区间在这里,公式在场景用例里 —— 清单里没有公式。
    derived_criteria: Dict[str, DerivedCriterion] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        config_path: str,
        force_mode: str | None = None,
        evidence: EvidenceSession | None = None,
    ) -> "CompositionRoot":
        manifest = parse_manifest(load_device_config(config_path))
        drivers: Dict[str, Any] = {}
        transports: Dict[str, Any] = {}
        device_types: Dict[str, str] = {}
        actual_modes: Dict[str, str] = {}
        models: Dict[str, Model] = {}
        roles: Dict[str, str] = {}
        initial_setpoints: Dict[str, Dict[str, float]] = {}
        upstreams: Dict[str, str] = {}
        criteria_overrides: Dict[str, tuple] = {}
        # 判据量 / 能力的可达性校验要等整份清单装配完再报:一次列出所有型号的缺失量。
        quantity_checks: list = []

        for spec in manifest.ordered_devices:
            device_id = spec["id"]
            model = manifest.models[spec["model"]]
            mode = force_mode or spec.get("mode", "real")

            # 下游设备不建物理连接:它借的是 via 指向的那台终端的总线,
            # 每次请求帧里带自己的 bus_address,由协议层选中从站。
            upstream_id = spec.get("via")
            if upstream_id is None:
                transport = build_transport(spec["access"], mode, spec.get("connection", {}))
            else:
                upstreams[device_id] = upstream_id
                transport = _SharedBusTransport(transports[upstream_id], drivers[upstream_id].device)
            transports[device_id] = transport

            criteria = _resolve_criteria(manifest.criteria_profiles, spec, model)
            criteria_overrides[device_id] = _overridden_quantities(spec)
            drivers[device_id] = build_device(
                model,
                device_id,
                spec,
                _driver_transport(evidence, device_id, spec, transport),
                criteria,
            )
            quantity_checks.append((device_id, model, criteria, drivers[device_id]))
            device_types[device_id] = model.type
            actual_modes[device_id] = mode
            models[device_id] = model
            roles[device_id] = spec["role"]
            initial = spec.get("initial_setpoint")
            if initial is not None:
                # 声明了初始设定、驱动却收不下它时在这里就报:留到 teardown 才发现,
                # 那时复位已经静默没发生,后面所有读数都被污染,而报告里看不出来。
                if not isinstance(drivers[device_id], SetpointRestorableDriver):
                    raise ValueError(
                        f"设备 {device_id!r}(型号 {model.name!r})声明了 initial_setpoint,"
                        f"但驱动 {type(drivers[device_id]).__name__} 没有 apply_initial_setpoint:"
                        f"这台周边设备无法恢复初始设定,收尾复位会静默不发生"
                    )
                initial_setpoints[device_id] = dict(initial)

        _validate_quantities_are_reachable(quantity_checks)
        return cls(
            drivers=drivers,
            transports=transports,
            device_types=device_types,
            actual_modes=actual_modes,
            models=models,
            roles=roles,
            initial_setpoints=initial_setpoints,
            upstreams=upstreams,
            criteria_overrides=criteria_overrides,
            evidence=evidence,
            derived_criteria=dict(manifest.derived_criteria),
        )

    def model_of(self, device_id: str) -> Model:
        if device_id not in self.models:
            raise KeyError(f"未找到设备: {device_id!r},已配置的设备: {sorted(self.models)}")
        return self.models[device_id]

    def reading_keys_of(self, device_id: str) -> tuple[str, ...]:
        """这台设备按点表能读到的读数键。用例按读数键取数,不认驱动类的方法名。"""
        return self.model_of(device_id).reading_keys

    def capabilities_of(self, device_id: str) -> frozenset:
        """这台设备声明的能力。用例按能力跳过,不按型号分叉。"""
        return self.model_of(device_id).capabilities

    def derived_criterion(self, name: str) -> DerivedCriterion:
        """清单里声明的一条派生判据(参与方 + 区间)。参与方只进证据,怎么算由用例决定。"""
        if name not in self.derived_criteria:
            raise KeyError(
                f"清单里没有派生判据 {name!r},已声明: {sorted(self.derived_criteria)}"
                f"(在 devices.yaml 的 derived_criteria 段声明它,见 #30)"
            )
        return self.derived_criteria[name]

    def judge_derived(self, name: str, value: float) -> bool:
        """派生量(如误差)按清单里的区间判合格:区间是数据,值是用例算出来的。

        公式不进 yaml(ADR 0002 / 0005),所以这里只认一个已经算好的值;
        改阈值仍然只改清单一处。
        """
        return self.derived_criterion(name).limits.evaluate(value) is None

    def get(self, device_id: str) -> Any:
        if device_id not in self.drivers:
            raise KeyError(f"未找到设备: {device_id!r},已配置的设备: {sorted(self.drivers)}")
        return self.drivers[device_id]

    def get_ready(self, device_id: str) -> Any:
        """取设备,并按 via: 链先把上游(终端)连好再交付。

        下游设备不建物理连接,它的 connect() 只确认上游连着,所以上游必须由取设备的人
        负责连上 —— 这件事属于 fixture,不属于用例。同一台上游只连一次:一条总线下挂
        几十块表,重复连总线正是 _SharedBusTransport 要避免的。
        上游连不上 → UpstreamFault:这一条用例没有有效结论,下游设备还没被验过。
        """
        upstream_id = self.upstreams.get(device_id)
        if upstream_id is not None:
            self.get_ready(upstream_id)
            upstream = self.get(upstream_id)
            if upstream.device.state is not DeviceState.CONNECTED:
                try:
                    upstream.connect()
                except Exception as exc:  # noqa: BLE001 - 上游的任何连接异常都让这条用例无效
                    raise UpstreamFault(upstream_id, exc) from exc
        return self.get(device_id)

    def all_drivers(self):
        return list(self.drivers.values())

    def ids_of_type(self, device_type: str, role: str = DEFAULT_ROLE) -> list[str]:
        """供测试层做"按类型批量取设备"用。和契约测试一致:缺省只取 role: dut ——
        周边设备不参与一致性判定。"""
        return [
            did
            for did, t in self.device_types.items()
            if t == device_type and self.roles[did] == role
        ]

    def role_of(self, device_id: str) -> str:
        if device_id not in self.roles:
            raise KeyError(f"未找到设备: {device_id!r},已配置的设备: {sorted(self.roles)}")
        return self.roles[device_id]

    def ids_of_role(self, role: str) -> list[str]:
        return [did for did, r in self.roles.items() if r == role]

    def connect_support(self) -> None:
        """装配顺序:周边设备先连接,被测设备后连接。

        周边设备在这里(装配时)连一次就位,被测设备的连接留在用例里——
        一台被测设备连不上只影响它自己的用例,而周边设备连不上则整个结论无效。
        所以装配阶段只连 role: support,这也让"先周边后被测"不靠用例自觉:
        组合根交出去之前,周边设备已经连着,被测设备还没连。
        连不上抛 SupportEquipmentFault,由测试层唯一的那一处转换成 skip。

        周边设备自己也可能挂在 via: 链上(标准表经被测终端那条总线访问),所以连它
        之前先把上游连好 —— 装配顺序在清单层已经排过(ordered_devices),这里按
        get_ready 逐台把链补上,总线只连一次。上游没通是"测试无效",但不是这台
        周边设备坏了,所以换成 SupportUpstreamNotReady 而不是泛泛的连接失败。
        """
        for device_id in self.ids_of_role(SUPPORT_ROLE):
            try:
                self.get_ready(device_id).connect()
            except UpstreamFault as exc:
                raise SupportUpstreamNotReady(device_id, exc) from exc
            except Exception as exc:  # noqa: BLE001 - 周边设备的任何连接异常都让本次测试无效
                raise SupportEquipmentFault(device_id, exc) from exc

    def mode_of(self, device_id: str) -> str:
        return self.actual_modes[device_id]

    def record_evidence(self, *, identity=None, events: Iterable[Any] = ()) -> None:
        """把这一轮的读数、判据结论、领域事件与用例身份写进证据文件(没开记录就什么都不做)。"""
        if self.evidence is None:
            return
        self.evidence.record_round(
            drivers=self.drivers,
            criteria_overrides=self.criteria_overrides,
            modes=self.actual_modes,
            identity=identity,
            events=events,
        )

    def teardown(self) -> None:
        """整轮收尾:补一次证据 → 台架复位 → 断开并关句柄。

        前两步都会抛,清理不能挂在它们后面:补记录抛错就跳过断开与关句柄的话,
        台架留在原处、句柄开着,下一轮被污染。所以清理在 finally 里,永不往外抛;
        它自己的失败落一条 platform_error,正在飞的那条原异常原样抛回。
        """
        try:
            if self.evidence is not None:
                # 先补一次证据再断开:用例崩在收尾记录之前时(失败必存),
                # 已读到的读数与它产生的事件仍然落盘,不因为异常丢了证据。
                self.record_evidence(events=_pending_events(self.drivers))
            try:
                # 台架回到清单声明的初始设定要在断开之前:复位要发帧,断开之后就发不出去了。
                # 这里只碰周边设备 —— 被测设备的收尾由用例自己声明,平台不在整轮结束时动它。
                self.restore_support()
            except SupportRestoreFault as exc:
                # 整轮收尾不再抛:每条用例的收尾已经把“恢复不了”转成了测试无效(skip),
                # 这里再抛一次会顶掉那条 skip(pytest 里后一个 finalizer 抛异常会盖掉前一个的
                # Skipped),退出码也从 0 变 1 —— 同一件事报两次,而最该被看见的那条被吞了。
                # 不静默的方式是落进本轮状态:round-status.json 与归因都按 operation 写成
                # “台架复位失败”,不是断链。
                if self.evidence is not None:
                    self.evidence.record_link_lost(
                        exc.device_id,
                        operation=SETPOINT_RESTORE_OPERATION,
                        cause=exc.__cause__ or exc,
                    )
        except Exception as exc:
            # 收尾自己出的错也要在证据里留下名字(这条随后原样抛回):只留下"记录抛错"
            # 而没有现场,事后分不清是证据写不进去还是台架复位发不出帧。
            self._note_teardown_failure("teardown", exc)
            raise
        finally:
            self._release_bench()

    def _release_bench(self) -> None:
        """断开所有设备并关掉证据句柄。自己的失败落进证据、不往外抛:
        抛出去会顶掉正在飞的那条原异常,现场就只剩清理这条。
        """
        for device_id, driver in self.drivers.items():
            try:
                driver.disconnect()
            except Exception as exc:
                # 组合根负责收尾,单个设备断开失败不应该掩盖其他设备的清理
                self._note_teardown_failure(f"teardown:{device_id}:disconnect", exc)
        if self.evidence is not None:
            try:
                # 证据写完了才关句柄:收尾这一轮记录还能进得去。
                self.evidence.close()
            except Exception as exc:
                self._note_teardown_failure("teardown:close", exc)

    def _note_teardown_failure(self, where: str, cause: BaseException) -> None:
        """收尾里的失败落一条 platform_error。记不进去也不抛:证据层自己坏了,
        没有第二处可记,不能因此顶掉原异常或跳过剩下的清理。"""
        if self.evidence is None:
            return
        try:
            self.evidence.record_platform_error(nodeid=where, case=None, cause=cause)
        except Exception:
            pass

    def restore_support(self) -> None:
        """把周边设备恢复到清单声明的初始设定(没声明 initial_setpoint 的不动)。

        用例把自己的设定留在台架上是"上一条用例污染这一条"的唯一来源:下一条用例读到
        错值会被判成设备不合格,而归因里没有这一类。所以这条在每条用例结束后与整轮
        结束时都跑一次。
        恢复失败 → SupportRestoreFault:这一轮没有有效结论(测试无效),不是设备不合格。
        """
        for device_id, setpoint in self.initial_setpoints.items():
            driver = self.get(device_id)
            if driver.device.state is not DeviceState.CONNECTED:
                # 这一轮没连过它:写不进去,也没有“这一轮把它设成什么”需要复原
                # (设定命令本身就要求先连上,见 domain/grid_simulator.py)。
                continue
            try:
                driver.apply_initial_setpoint(setpoint)
            except Exception as exc:  # noqa: BLE001 - 台架的任何恢复异常都让这一轮无效
                raise SupportRestoreFault(device_id, exc) from exc


def _driver_transport(
    evidence: EvidenceSession | None, device_id: str, spec: dict, transport: Any
) -> Any:
    """给 driver 用的传输:开了记录就包一层,没开就原样交出去(行为与没有证据层时一致)。

    非字节流的传输在这里包不进来:证据层只实现了字节流的那五个方法,所以 wrap 会直接
    报错 —— 错误发生在装配期(而不是第一次连接时),消息会说明只支持字节流规约。

    周边设备最外面再套一层断链归属(记录层在里面,所以丢帧的失败种类照样落盘);
    被测设备不套:它的链路断了就是它不合格。
    """
    if evidence is not None:
        if spec["role"] == DEFAULT_ROLE:
            # 每台被测设备先占一份文件:这一轮一条读数都没有的设备也看得出来测过它。
            evidence.open_device(device_id)
        transport = evidence.wrap(device_id, transport)
    if spec["role"] == SUPPORT_ROLE:
        return _SupportLinkGuard(transport, device_id, evidence)
    return transport


def _overridden_quantities(spec: dict) -> tuple:
    """这台设备用 criteria_override 覆盖掉了哪些物理量(只是呈现用的元信息,不参与求值)。"""
    return tuple(
        dict.fromkeys(entry["quantity"] for entry in spec.get("criteria_override") or [])
    )


def _pending_events(drivers: Dict[str, Any]) -> list:
    """各聚合根里还没被取走的事件(收尾时补救用;正常情况已经被每轮的事件搬运取空了)。"""
    return [event for driver in drivers.values() for event in driver.device.pull_events()]


def _resolve_criteria(profiles: Dict[str, list], spec: dict, model: Model) -> CriteriaProfile:
    """型号引用的具名判据集 + 该设备的行内覆盖,合并成一个具体的判据值对象。

    覆盖规则:同一物理量、同一严重度的判据被整条替换,其余追加。
    判据里的边界值可以写成 {设备参数: 系数}(比如额定功率的 110%),
    由这里按设备参数算成绝对值——判据集因此在清单里只写一次,
    额定功率不同的设备各算各的。
    """
    name = model.criteria
    raw = list(profiles.get(name, [])) + list(spec.get("criteria_override", []) or [])
    criteria: Dict[tuple, Criterion] = {}
    for entry in raw:
        criterion = _build_criterion(entry, spec, where=f"设备 {spec['id']}")
        criteria[(criterion.quantity, criterion.severity)] = criterion

    return CriteriaProfile(name=name or spec["id"], criteria=tuple(criteria.values()))


def _build_criterion(entry: dict, spec: dict, where: str) -> Criterion:
    low, low_exclusive = _bound(entry, "min", "gt", spec, where)
    high, high_exclusive = _bound(entry, "max", "lt", spec, where)
    return Criterion(
        quantity=entry["quantity"],
        low=low,
        low_exclusive=low_exclusive,
        high=high,
        high_exclusive=high_exclusive,
        severity=Severity(entry.get("severity", "reject")),
        equals=entry.get("equals"),
    )


def _judged_quantities(driver: Any) -> Mapping[str, str]:
    """驱动自述的「读数键 → 判据量名」。没有这层映射的驱动(电表 / 终端 / 台架)返回空:
    它们的判据量就是点表读数键本身。"""
    return getattr(driver.device, "quantities", {})


def _validate_quantities_are_reachable(checks: Iterable[tuple]) -> None:
    """装配期交叉校验:判据量和能力都必须落在这台设备真能判的量里,一次报全。

    失败点原本在第一条读数 —— 那时台架已接、电源已开。capabilities 更阴:能力声明的
    用途是"没有就跳过用例",声明了却读不到就是让跳过机制说谎。

    能判的量是并集,不是只有点表读数键:电表的判据量就是读数键(点表这一半),
    vendor_ascii 型号的判据量是驱动把读数键映射成的聚合根参数名(有功功率 → output_power_w,
    驱动这一半)。映射只在驱动里,所以由驱动自述,组合根不另抄一份。

    校验跑在**最终生效**的判据集上(基础集 + criteria_override 合并后),不对基础集与覆盖条目
    各校一次:同一物理量被覆盖整条替换时,以最终生效的那条为准。

    derived_criteria 不在这条校验里:它的量是跨设备算出来的,不属于任何一台点表。
    """
    point_keys: Dict[str, tuple] = {}
    gaps: Dict[str, list] = {}
    model_level_checked: set = set()
    for device_id, model, criteria, driver in checks:
        mapping = _judged_quantities(driver)
        keys = frozenset(model.reading_keys)
        reachable = keys | frozenset(mapping.values())
        point_keys.setdefault(model.name, model.reading_keys)
        lines = gaps.setdefault(model.name, [])
        if model.name not in model_level_checked:
            # 点表与能力都是型号级声明:一个型号只查一次。
            model_level_checked.add(model.name)
            absent_capabilities = sorted(model.capabilities - keys)
            if absent_capabilities:
                lines.append(f"声明的能力 {absent_capabilities} 不在点表读数键里")
            unread = sorted(set(mapping) - keys)
            if unread:
                lines.append(f"驱动的映射表要读 {unread},点表里没有这些读数键")
        absent = sorted({criterion.quantity for criterion in criteria.criteria} - reachable)
        if absent:
            lines.append(
                f"判据集 {criteria.name!r}(设备 {device_id!r})判的量 {absent} 点表和驱动都没有"
            )
    reported = [f"型号 {name!r}: " + ";".join(lines) for name, lines in gaps.items() if lines]
    if not reported:
        return
    raise ValueError(
        "装配期交叉校验失败: " + " | ".join(reported) + "。各型号点表读数键: "
        + "; ".join(f"{name} {sorted(keys)}" for name, keys in point_keys.items())
    )


def _bound(entry: dict, inclusive_key: str, exclusive_key: str, spec: dict, where: str):
    """闭区间写 min/max,开区间写 gt/lt;同一侧两个都写是清单写错了。"""
    written = [key for key in (inclusive_key, exclusive_key) if key in entry]
    if len(written) > 1:
        raise ValueError(
            f"{where}: 判据 {entry['quantity']!r} 同一侧不能同时写 {inclusive_key} 和 {exclusive_key}"
        )
    if not written:
        return None, False
    key = written[0]
    return _threshold(entry[key], spec, where), key == exclusive_key


def _threshold(value, spec: dict, where: str) -> float:
    if isinstance(value, dict):
        ((base_key, factor),) = value.items()
        if base_key not in spec:
            raise KeyError(f"{where}: 判据基准 {base_key!r} 不在设备配置 {sorted(spec)} 里")
        return spec[base_key] * factor
    return float(value)
