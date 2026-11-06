"""清单的值对象:Point / Model / Manifest / 设置类条款。"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from testplatform.cases import Selector
from testplatform.domain.criteria import Criterion
from testplatform.protocol.modbus_rtu import READ_HOLDING_REGISTERS

from .constants import DEFAULT_ROLE

@dataclass(frozen=True)
class Point:
    """点表的一项:一个读数键怎么从地址上取出来。

    address 的写法由规约决定:DL/T645 写数据标识(4 字节,写成 8 位十六进制,如 "00010000"),
    Modbus 写寄存器地址。length 是字节数,倍率/字节序/字序/功能码/单位都在这一项里声明,
    型号差异因此只出现在点表里,不进代码。
    """

    key: str
    address: Any
    length: int
    encoding: str
    scale: float = 1.0
    byteorder: str = "big"
    # 字序:值跨 2 个以上寄存器时,先拼哪些字。与 byteorder 是两条独立的轴。
    word_order: str = "big"
    # Modbus 功能码(已解析成 0x03 / 0x04):其它规约恒为 0x03,帧里用不到。
    function: int = READ_HOLDING_REGISTERS
    unit: str = ""
    # 这个点允许被写。是与否就是全部信息:写哪一个地址、怎么写由点表配置决定,不在代码里猜。
    writable: bool = False


@dataclass(frozen=True)
class SettingStep:
    """设置回环的一步:写哪个点、写什么值、读回来应该是什么。"""

    write: str
    value: float
    expect: Any


@dataclass(frozen=True)
class ManualStep:
    """一步人工操作:切换上位机通信模式、目视看液晶、拔插卡。

    平台没有它的机读判据,所以它既不判通过也不判失败(第三种状态 pending_manual):
    含人工步骤的条款先把前面的自动步骤跑完(失败就是设备不合格),碰到它才 skip,
    证据里留一条 confirmed: null 的占位等操作员回填。
    expect 是文字说明(由谁按什么确认),写成数字就变成机读判据了 —— 装配前拒绝。
    """

    action: str
    expect: str


@dataclass(frozen=True)
class SettingCase:
    """一条设置类条款:型号 + 条款编号 + 若干步"写 → 读回相等"或人工操作。

    这类条款没有因果链(实测上游 659 条里 527 条是这样),所以不写进场景层:
    清单里一条一行,用例侧只有一个函数(tests/test_setting_cases.py)。
    型号在这里的作用是"写哪个点表里可写的点",装配前据此校验 write 的键。
    """

    id: str
    model: str
    clause: str
    steps: Tuple[SettingStep | ManualStep, ...]
    # 台架前置:台架点表的读数键 → 值。它不是步骤、也不参与判定,只是"让表能测"的前提
    # (ADR 0006):条款前面那段每一条都一样的台架设定写在这里,不塞进 steps。
    bench: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def manual_steps(self) -> Tuple[ManualStep, ...]:
        """这条条款里要人做的步。非空就只跑到第一个人工步骤:前面的自动步骤照跑(它们能抓设备故障),
        跑到人工步骤停 —— skip 一条不等于一步没跑。"""
        return tuple(step for step in self.steps if isinstance(step, ManualStep))


@dataclass(frozen=True)
class DerivedCriterion:
    """一条派生判据:跨多台设备算出来的量(误差)允许落在什么区间。

    参与方只为说清"这个量牵涉谁"(并让装配前查得出来写错的 id),不绑定怎么算:
    算的过程在场景用例的 Python 里,清单里没有公式可写。
    limits 复用判据值对象:派生量的合格与否仍由 Criterion.evaluate 判,
    不另造一套结论模型。
    """

    name: str
    participants: Tuple[str, ...]
    limits: Criterion


@dataclass(frozen=True)
class Model:
    """型号:同一大类的具体产品规格。点表、能力、判据集在型号上声明一次。"""

    name: str
    type: str
    protocol: str
    criteria: str | None
    capabilities: frozenset
    point_table: Mapping[str, Point]
    # 该型号接受哪些设备级参数(如额定功率、容量),由设备实例给值。
    device_params: frozenset = frozenset()

    @property
    def reading_keys(self) -> Tuple[str, ...]:
        """这台型号能读到的全部读数键(就是点表项的键)。"""
        return tuple(self.point_table)


@dataclass(frozen=True)
class Manifest:
    models: Mapping[str, Model]
    devices: Tuple[dict, ...]
    criteria_profiles: Mapping[str, list]
    # 适用矩阵:型号 → 该型号实际跑哪些用例(用例编号前缀或 tag:标签)。
    # 不在矩阵里的型号不受限:它的用例全跑(漏测比多测危险)。
    test_matrix: Mapping[str, Tuple[Selector, ...]] = field(default_factory=dict)
    # 设置类条款:条款编号 → 条款。编号全清单唯一(它是证据与归因的身份)。
    setting_cases: Mapping[str, SettingCase] = field(default_factory=dict)
    # 派生判据:判据名 → 参与方与区间。区间在这里,公式在用例里。
    derived_criteria: Mapping[str, DerivedCriterion] = field(default_factory=dict)

    def ids_of_role(self, role: str) -> Tuple[str, ...]:
        """某个角色下的全部设备 id。"""
        return tuple(spec["id"] for spec in self.devices if spec["role"] == role)

    def ids_of_type(self, device_type: str, role: str = DEFAULT_ROLE) -> Tuple[str, ...]:
        """某个设备类型下、某个角色的实例 id。契约测试只对 dut 展开:
        周边设备不参与一致性判定。"""
        return tuple(
            spec["id"]
            for spec in self.devices
            if spec["role"] == role and self.models[spec["model"]].type == device_type
        )

    @property
    def ordered_devices(self) -> Tuple[dict, ...]:
        """装配顺序:via 指向的设备(终端)先、挂在上面的下游设备后。

        拓扑序在解析时就验证过无环无悬空引用,这里不会卡住;真卡住说明
        上面的校验掉了,立即报错而不是死循环。
        """
        ordered, remaining, placed = [], list(self.devices), set()
        while remaining:
            ready = [spec for spec in remaining if spec.get("via") is None or spec["via"] in placed]
            if not ready:
                raise ValueError(f"设备接入关系成环,无法排序: {[s['id'] for s in remaining]}")
            placed.update(spec["id"] for spec in ready)
            ordered.extend(ready)
            remaining = [spec for spec in remaining if spec["id"] not in placed]
        return tuple(ordered)
