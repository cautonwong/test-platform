"""
清单各段的解析与校验:criteria_profiles(判据集)+ derived_criteria(跨设备派生判据)+
protocol_items(协议数据项目录)+ models(型号)+ devices(设备实例)。

型号把点表 / 能力 / 判据集声明一次,设备实例只写 id、型号名和接入位置。
整份清单在装配前校验完:型号引用、规约与接入方式、点表字段、判据键、设备角色、设备实例的键。
每条错误都要报出"是哪一条、错在哪"——静默忽略未知字段会变成假通过,
那是这个平台上最危险的 bug。

protocol_items 是**协议固有信息**的目录:一个数据标识(DI)的长度 / 编码 / 字节序 / 倍率
由规约决定,不由型号决定。一个协议对应几万个数据项,型号只声明差异:
点表项写 {item: <DI>, scale: <型号差异>},没写的字段从目录取。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

import yaml

from testplatform.cases import Selector, parse_matrix_entries
from testplatform.config import paths
# 设置类条款里 value / expect 能不能当数字用,与判据的 equals 同一份口径 —— 不另写一套。
from testplatform.domain.criteria import Criterion, coerce_number
from testplatform.protocol.modbus_rtu import READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS
from testplatform.protocol.values import DECODERS, encode_value

# 清单只允许这六段。多出来的段名多半是拼错,静默忽略就是下一个假通过。
TOP_LEVEL_KEYS = (
    "criteria_profiles",
    "derived_criteria",
    "protocol_items",
    "models",
    "devices",
    "test_matrix",
    "setting_cases",
)

MODEL_KEYS = {"type", "protocol", "criteria", "capabilities", "point_table", "device_params"}
MODEL_REQUIRED = ("type", "protocol", "capabilities", "point_table")

# 这些是型号级参数:写在设备实例上就是重复型号级声明,装配前拒绝。
MODEL_ONLY_DEVICE_KEYS = ("type", "protocol", "criteria", "capabilities", "point_table")

# 设备实例的角色:被测设备判合格;周边设备不判合格,它挂掉等于测试无效。
DUT_ROLE = "dut"
SUPPORT_ROLE = "support"
DEVICE_ROLES = (DUT_ROLE, SUPPORT_ROLE)
DEFAULT_ROLE = DUT_ROLE

# 设备实例的键是闭合集合:下面这些核心键,加上型号自己声明的设备参数。
# 拼错一个键(rated_power → rated_powerw)就会被拒绝,而不是静默取默认值。
CORE_DEVICE_KEYS = (
    "id",
    "model",
    "role",
    "access",
    "mode",
    "name",
    "via",
    "bus_address",
    "connection",
    "criteria_override",
    "initial_setpoint",
)

POINT_KEYS = {
    "address",
    "length",
    "encoding",
    "scale",
    "byteorder",
    "word_order",
    "function",
    "unit",
    # 平台侧的许可:只有显式声明可写的点才允许写。缺省不可写,没声明就写会报错,不猜。
    "writable",
}
POINT_REQUIRED = ("address", "length", "encoding")
# 占位地址:这个量的真实寄存器/字段还没定义(等台架手册或实测)。它不是一个地址,
# 所以在 mode: real 的装配期报错(见 _validate_real_mode_addresses),只在模拟台架上允许。
PLACEHOLDER_ADDRESS = "TODO"

# 台架量名沿用上游条款的写法(Ua/Ub/Uc/Fre……)。旧清单里的 电压 / 频率 不再认:
# 给了别名就是同一个量的两处真相,后面读数和设定一定会对不上,所以这里直接报迁移提示。
GRID_SIMULATOR_TYPE = "grid_simulator"
MIGRATED_GRID_QUANTITIES = {"电压": "Ua/Ub/Uc", "频率": "Fre"}
# 协议目录项:一个 DI 的固有事实。地址就是 DI 本身(映射的键),所以没有 address;
# name 是这个 DI 的语义名(如 正向有功总电能),只作文档,不参与读数。
PROTOCOL_ITEM_KEYS = POINT_KEYS - {"address"} | {"name"}
# 点表项的引用写法:{item: <DI>} 加要覆盖的字段。address 不在这张表里(见 _resolve_item):
# item 已经是地址,再写一遍就是同一个字段的两处真相。
POINT_REFERENCE_KEYS = POINT_KEYS - {"address"} | {"item"}
# 长度和编码是数据项的固有事实:目录项必填,引用写法从目录继承。
POINT_FIELD_REQUIRED = ("length", "encoding")
# 编码集合由解码器派生:加一种编码只改 protocol/values.py 的 DECODERS。
POINT_ENCODINGS = frozenset(DECODERS)
BYTEORDERS = {"big", "little"}
# 功能码写成语义名,不写默认读保持寄存器(0x03)。数字功能码写错一位在装配前无从判断,
# 名字拼错立刻掉进取值校验;两个名字对应协议层已有的 0x03 / 0x04。
POINT_FUNCTIONS: Dict[str, int] = {
    "read_holding_registers": READ_HOLDING_REGISTERS,
    "read_input_registers": READ_INPUT_REGISTERS,
}
DEFAULT_POINT_FUNCTION = "read_holding_registers"
# 功能码是 Modbus 的概念:DL/T645 的请求帧里没有这个位置。MBAP 帧里的 PDU 与 RTU 逐字节相同,
# 所以两种 Modbus 帧共用这一组功能码名。
FUNCTION_PROTOCOLS = frozenset({"modbus_rtu", "modbus_tcp"})
# 长度固定的编码:float32 必须是 4 字节。长度写错在运行时只会得到一句 struct.error
# (说不出是哪个点表项),所以在装配前按编码校验。
POINT_FIXED_LENGTHS = {"float32": 4}

# 设置类条款的步骤有两种,条件 / 循环 / 引用前一步读数都不表达 ——
# 有因果链的条款归场景层(tests_scenario/),这里只表达"写 → 读回 → 判相等"与人工操作。
#   write / value / expect  自动回环:写哪个点、写什么值、读回应该是什么
#   manual / expect         人工操作:做什么,由操作员按什么确认(expect 是文字说明,不是机读判据)
SETTING_WRITE_STEP_KEYS = ("write", "value", "expect")
SETTING_STEP_KEYS = SETTING_WRITE_STEP_KEYS + ("manual",)
SETTING_CASE_KEYS = {"clause", "steps", "bench"}

# 条款开头的台架设定(bench:)与导入器共用同一份对照表:同一个台架量两套拼写迟早漂开,
# 而漂开之后导入侧按 A 写、装配侧按 B 查,拼错的那一边永远绿。
# 位置按平台根解析(与执行器、条款清单同一条规则),不在 import 期按源码树层级推路径。
BENCH_PARAMS_RELATIVE = "configs/import/params.yaml"

# 派生判据:跨多台设备算出来的量(误差)的区间。清单只写"叫什么、谁参与、允许落在哪",
# 公式不写 —— 公式一旦进 yaml,就是 ADR 0002 / 0005 明确否决的那门 DSL 的开端;
# "算增量、求误差"是一段普通 Python,写在场景用例里(tests_scenario/)。
DERIVED_CRITERION_KEYS = {"participants", "limits"}
# 派生量的区间只有闭区间:它是算出来的数(误差百分比),开区间/等值在这里没有对应的条款。
DERIVED_LIMIT_KEYS = ("min", "max")

CRITERION_KEYS = {"quantity", "min", "max", "gt", "lt", "equals", "severity"}
# 与 equals 互斥的边界键:一个判据要么判区间,要么判等值。
CRITERION_RANGE_KEYS = ("min", "max", "gt", "lt")
SEVERITIES = {"reject", "warn"}

# 规约 → 允许的接入方式。这是协议知识,不随某台设备的配置变:
# DL/T645 是串口规约,不会直接跑在裸 TCP 上(TCP 负载的是串口服务器,那是另一个规约)。
# modbus_rtu 允许 tcp:那是"RTU 帧走 TCP"(串口服务器的常见形态,帧里只有从站地址 + CRC);
# Modbus TCP 是另一个规约(MBAP 头:事务号 / 协议号 / 长度 / 单元号),别用 modbus_rtu 去连它。
# 反过来也一样:MBAP 帧没有 CRC、靠 TCP 保证字节完整,串口上跑不了 —— 所以 modbus_tcp 只放行 tcp,
# 写 access: serial 在装配前就报错,而不是等串口上发出去的 MBAP 帧一个应答都收不到。
# CAN 也是一样:物理层是 CAN 总线,帧里的应用层规约是 CANopen(CiA 301;
# 电池 / 储能另有 CiA 418 / 419 行规),所以 canopen 只放行 access: can。
# 没有这个条目,CAN 接入在清单里根本走不到 —— 先淹在"未知规约"里,
# CAN 的接口白名单 / 平台缺省 / 未知值报错就都成了只有直接 new 传输才用得上的死代码。
PROTOCOL_ACCESS: Dict[str, frozenset] = {
    "vendor_ascii": frozenset({"tcp", "serial", "gateway_ssh"}),
    "dlt645": frozenset({"serial", "gateway_ssh"}),
    "modbus_rtu": frozenset({"serial", "gateway_ssh", "tcp"}),
    "modbus_tcp": frozenset({"tcp"}),
    "canopen": frozenset({"can"}),
}

# 传输已实现、但没有任何规约声明支持这三种接入方式:
# transport/real_transports.py 里有 RealHttpTransport / RealMqttTransport / RealSshTransport,
# registry.py 里注册为 http / mqtt / ssh。它们在清单里暂时不可达,不是因为没写,而是因为
# 今天没有设备用 REST / MQTT 上报 / 主机命令访问 —— 凭空造一个 http 规约放行,装配时只会
# 停在"未找到设备工厂",那是"能配但必失败"的假象。所以这里只把边界说清楚。
# 要放行:先定该设备用什么规约上这种接入(如 EMS 走 REST、HEMS 走 MQTT 上报),
# 再把该规约加进 PROTOCOL_ACCESS,并同时补 connection 的闭合键集合。
TRANSPORT_ONLY_ACCESS = frozenset({"http", "mqtt", "ssh"})

# 这些规约的请求帧里要带从站地址:设备实例必须写 bus_address。
# Modbus RTU 用 1–247 的整数;Modbus TCP 的这个字段是 MBAP 头里的单元号(常用来区分
# 同一网关后面的串口从站,同样 1–247);DL/T645 用 12 位 BCD 表地址(如 "000000000001")。
BUS_ADDRESS_PROTOCOLS = frozenset({"modbus_rtu", "modbus_tcp", "dlt645"})
DLT645_ADDRESS_DIGITS = 12

# CAN 接入的接口类型(python-can 的 bustype)。socketcan 是 Linux 内核专有:
# 写死在代码里会让 Windows 上接了 CAN 设备直接起不来,也换不成 pcan / vector / slcan,
# 所以它进清单,并在装配前校验。每个值都意味着这台机器上装了对应的驱动或硬件:
#   socketcan Linux 内核 SocketCAN(自带)
#   pcan      PEAK PCAN-USB 系列(Windows / Linux)
#   vector    Vector 板卡(Windows)
#   slcan     USB 转 CAN 串口适配器(跨平台,不用厂商驱动)
#   virtual   python-can 虚拟总线,没有硬件时自测用
CAN_INTERFACES = ("socketcan", "pcan", "vector", "slcan", "virtual")
CAN_CONNECTION_KEYS = ("channel", "bitrate", "interface")

# 不写 mode 时的缺省值,与组合根一致(composition/root.py: spec.get("mode", "real"))。
# 只决定必填键要不要查:模拟器不读主机名 / 端口。
DEFAULT_MODE = "real"

# 字节流三族(serial / tcp / gateway_ssh)的 connection 也是闭合键集合,
# 与实现真正读取的键一一对应:
#   serial      transport/real_transports.py::_build_serial        port(必填)/ baudrate(缺省 9600)/
#                                                                  inter_frame_delay_ms(缺省 0 = 不等待)
#   tcp         transport/real_transports.py::_build_tcp           host / port(都必填)
#   gateway_ssh transport/gateway_transport.py::_build_gateway_ssh gateway_host / gateway_user /
#                                                                  local_bus(必填)/ gateway_key_path /
#                                                                  gateway_known_hosts
# 模拟器键三族共用:transport/simulators.py 的通用字节流工厂只认 simulated_* 这几个。
# 拼错一个键(baudrate → baudrat)以前静默取缺省:19200 变 9600,现场表现为
# "设备连上了但读数是乱的"——比连不上更难查,所以这里不允许开口子。
# simulated_frames 选模拟从站产出哪种帧(modbus_rtu 缺省 / modbus_tcp):它必须与型号的
# protocol 一致,否则驱动发出的帧在对端解析不了 —— 这一维不在清单里写死,模拟从站就没法替
# Modbus TCP 设备应答。
# 第二个元组是必填键:可选的 hardware 参数只进第一个元组,不能变成必填。
STREAM_SIMULATOR_CONNECTION_KEYS = (
    "simulated_response",
    "simulated_645",
    "simulated_registers",
    "simulated_frames",
    "simulated_grid",
    # 静态故障注入(装配时定死,不支持运行中切换)。种类白名单在 transport/registry.py;
    # 具体某个模拟器造不造得出来由它自己判(装配期报错并列出支持项)。
    "simulated_faults",
)
STREAM_ACCESS_CONNECTION_KEYS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "serial": (("port", "baudrate", "inter_frame_delay_ms"), ("port",)),
    "tcp": (("host", "port"), ("host", "port")),
    "gateway_ssh": (
        ("gateway_host", "gateway_user", "local_bus", "gateway_key_path", "gateway_known_hosts"),
        ("gateway_host", "gateway_user", "local_bus"),
    ),
}

# 主机密钥比对用的 known_hosts 路径:直连 ssh 与经网关 gateway_ssh 各一个键,语义相同。
# 两条路都在装配前校验“它是个存在的文件”,内容在连接时由 paramiko 比对(见 _validate_known_hosts_path)。
KNOWN_HOSTS_KEYS: Dict[str, str] = {
    "ssh": "known_hosts",
    "gateway_ssh": "gateway_known_hosts",
}

# gateway_ssh 的 local_bus 会被拼进网关主机上执行的 shell 命令(transport/gateway_transport.py)。
# 清单是会被复制传播的数据,供应商回一份 yaml 就可能带进 `can0; id > /tmp/pwned #` 这种值,
# 而平台在网关主机上以 gateway_user(常是工程账号甚至 root)执行它。所以只收总线名与设备节点
# 路径这两种写法:`/` 要留着(/dev/ttyUSB0、/dev/serial/by-id/... 是 tty 的推荐写法),
# 长度上限放到 64 才装得下 by-id 路径。
# 前导 `-` 单独禁:shlex.quote 只防 shell 解释,不防 bus-agent 把 `-x` / `--data` 当成选项。
# (更彻底的做法是改成 --bus=<值> 单参数,但今天无法断言真网关上的 bus-agent 认不认这种写法,
#  改了就是“配置对了可网关不认”,所以只禁前导 `-`,见 50 号票 Comments。)
GATEWAY_LOCAL_BUS_CHARSET = r"A-Za-z0-9_.:/-"
GATEWAY_LOCAL_BUS_MAX_LENGTH = 64
GATEWAY_LOCAL_BUS_PATTERN = re.compile(rf"(?!-)[A-Za-z0-9_.:/-]{{1,{GATEWAY_LOCAL_BUS_MAX_LENGTH}}}")


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


def parse_manifest(config: dict) -> Manifest:
    """校验整份清单并解析成型号/设备,任何一条写错都在这里抛错。"""
    _reject_unknown_keys("清单顶层", config, TOP_LEVEL_KEYS)
    profiles = _parse_criteria_profiles(config.get("criteria_profiles"))
    catalog = _parse_protocol_items(config.get("protocol_items"))
    models = _parse_models(config.get("models"), profiles, catalog)
    devices = _parse_devices(config.get("devices"), models)
    _validate_topology(devices)
    derived_criteria = _parse_derived_criteria(
        config.get("derived_criteria"), devices, models
    )
    test_matrix = _parse_test_matrix(config.get("test_matrix"), models)
    setting_cases = _parse_setting_cases(config.get("setting_cases"), models)
    return Manifest(
        models=models,
        devices=devices,
        criteria_profiles=profiles,
        test_matrix=test_matrix,
        setting_cases=setting_cases,
        derived_criteria=derived_criteria,
    )


def _parse_derived_criteria(
    raw: Any, devices: Tuple[dict, ...], models: Mapping[str, Model]
) -> Mapping[str, DerivedCriterion]:
    """派生判据:判据名 → {participants, limits}。

    三件事在装配前查掉,它们写错只会在现场表现成"判了一个没人算过的量":
      - 参与方必须是清单里真实存在的设备 id,至少两台(一台设备能读到的量用判据集就够了,
        派生的意义正是跨设备);
      - 判据名不能与参与方的读数键重名 —— 撞上之后证据里分不清判的是读数还是派生量;
      - 区间只收 min / max 且必须是数字:派生量是跨设备算出来的数,
        没有"某台设备的额定功率"可以当基准换算(那是单台设备判据的写法)。

    参与方只查到这一步为止:清单里没有公式,所以查不出"计算时有没有真用到这些设备" ——
    那由场景用例的 Python 负责,清单不为它背书。

    shortcut: 同一个判据名写两遍(YAML 同一个键重复)只有后一份生效 —— 整份清单的映射段
    都是这个行为(models 段也一样),要治先治清单加载。
    """
    derived: Dict[str, DerivedCriterion] = {}
    by_id = {spec["id"]: spec for spec in devices}
    for name, entry in (raw or {}).items():
        where = f"derived_criteria 判据 {name!r}"
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: 判据名必须是非空字符串,拿到 {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}: 必须写成映射 {sorted(DERIVED_CRITERION_KEYS)},拿到 {entry!r}"
            )
        _reject_unknown_keys(where, entry, DERIVED_CRITERION_KEYS)
        participants = _parse_derived_participants(where, entry.get("participants"), by_id)
        _reject_derived_name_clash(where, name, participants, by_id, models)
        derived[name] = DerivedCriterion(
            name=name,
            participants=participants,
            limits=_parse_derived_limits(where, name, entry.get("limits")),
        )
    return MappingProxyType(derived)


def _parse_derived_participants(where: str, raw: Any, by_id: Mapping[str, dict]) -> Tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(
            f"{where}: participants 必须写成至少两台设备 id 的列表"
            f"(派生量是跨设备的量,一台设备的读数用判据集判),拿到 {raw!r}"
        )
    for device_id in raw:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError(f"{where}: participants 里的设备 id 必须是非空字符串,拿到 {device_id!r}")
        if device_id not in by_id:
            raise KeyError(
                f"{where}: participants 里的 {device_id!r} 不是清单里的设备,已声明的设备: {sorted(by_id)}"
            )
    repeated = sorted({device_id for device_id in raw if raw.count(device_id) > 1})
    if repeated:
        raise ValueError(
            f"{where}: participants 里 {repeated} 写了不止一次,同一台设备只参与一次"
        )
    return tuple(raw)


def _reject_derived_name_clash(
    where: str,
    name: str,
    participants: Tuple[str, ...],
    by_id: Mapping[str, dict],
    models: Mapping[str, Model],
) -> None:
    for device_id in participants:
        if name in models[by_id[device_id]["model"]].point_table:
            raise ValueError(
                f"{where}: 判据名与设备 {device_id!r} 的读数键重名;"
                f"派生量是算出来的量、不是读数,名字撞上读数键之后证据里分不清判的是哪一个"
                f"(换个名字,如 有功误差)"
            )


def _parse_derived_limits(where: str, name: str, raw: Any) -> Criterion:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{where}: limits 必须写成非空映射 {list(DERIVED_LIMIT_KEYS)}(如 {{min: -1.0, max: 1.0}}),"
            f"拿到 {raw!r}"
        )
    unknown = sorted(set(raw) - set(DERIVED_LIMIT_KEYS))
    if unknown:
        raise ValueError(
            f"{where}: limits 里的未知边界 {unknown},可用: {list(DERIVED_LIMIT_KEYS)}"
        )
    bounds: Dict[str, float] = {}
    for side in DERIVED_LIMIT_KEYS:
        if side not in raw:
            continue
        value = raw[side]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{where}: limits.{side} 必须是数字(派生量是算出来的数,没有设备参数可换算),"
                f"拿到 {value!r}"
            )
        bounds[side] = float(value)
    return Criterion(quantity=name, low=bounds.get("min"), high=bounds.get("max"))


def _parse_setting_cases(raw: Any, models: Mapping[str, Model]) -> Mapping[str, SettingCase]:
    """设置类条款:型号 → 条款编号 → {clause, steps}。

    装配前把能查的都查掉,不留到运行时:
      - 引用的型号必须存在(条款写在哪个点表上);
      - 条款编号全清单唯一 —— 它是证据与归因的身份,重号就分不清是谁的记录;
      - 步骤只有 write / value / expect / manual 四个词,缺一个、多一个都是清单写错;
        manual 步骤没有可写的点,所以不能再写 write / value,expect 只能是文字说明;
      - write 的键必须是该型号点表里 writable: true 的点;
      - value 必须与那个点的 encoding / length 相容(编码那一关就是写路径那一个函数);
      - expect 必须是单个值:区间判定归判据集(equals 那一套),不在这里表达。

    shortcut: 同一条款在一个型号下写两遍(YAML 同一个键重复)只有后一份生效 ——
    整份清单的映射段都是这个行为(models 段也一样),要治先治清单加载。
    """
    cases: Dict[str, SettingCase] = {}
    for model_name, clauses in (raw or {}).items():
        where = f"setting_cases 型号 {model_name!r}"
        if model_name not in models:
            raise KeyError(f"{where}: 引用了不存在的型号,已声明的型号: {sorted(models)}")
        if not isinstance(clauses, dict) or not clauses:
            raise ValueError(
                f"{where}: 必须写成非空映射(条款编号 → {{clause, steps}}),拿到 {clauses!r}"
            )
        model = models[model_name]
        for case_id, entry in clauses.items():
            where = f"setting_cases 条款 {case_id!r}"
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError(
                    f"{where}: 条款编号必须是非空字符串(如 M_00_00_01_000002_18),拿到 {case_id!r}"
                )
            if case_id in cases:
                raise ValueError(
                    f"{where}: 条款编号重复(型号 {cases[case_id].model!r} 下已有这一条)。"
                    f"编号是证据与归因的唯一身份,全清单只能出现一次"
                )
            cases[case_id] = _parse_setting_case(case_id, model, models, entry, where)
    return MappingProxyType(cases)


def _parse_setting_case(
    case_id: str, model: Model, models: Mapping[str, Model], entry: Any, where: str
) -> SettingCase:
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 必须写成映射 {sorted(SETTING_CASE_KEYS)},拿到 {entry!r}")
    _reject_unknown_keys(where, entry, SETTING_CASE_KEYS)
    clause = entry.get("clause")
    if not isinstance(clause, str) or not clause.strip():
        raise ValueError(
            f'{where}: clause 必须是条款清单里的 id(如 "DLT645-4.5.2"),拿到 {clause!r}'
        )
    steps = entry.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(
            f"{where}: steps 必须是非空列表(每项一步:写 → 读回,或一步人工操作),拿到 {steps!r}"
        )
    return SettingCase(
        id=case_id,
        model=model.name,
        clause=clause,
        steps=tuple(
            _parse_setting_step(model, case_id, index, step)
            for index, step in enumerate(steps)
        ),
        bench=_parse_bench(entry.get("bench"), models, where),
    )


def _parse_bench(raw: Any, models: Mapping[str, Model], where: str) -> Mapping[str, float]:
    """条款的台架前置:量名 → 值。量名过与导入器同一份对照表,值过台架点表的编码。

    它不进 steps,也不参与判定 —— 只是"让表能测"的前提(ADR 0006),所以这里只查三件事:
    这个名字是不是那份对照表里的台架量、台架型号点表里有没有这个量、这个值台架收不收得下。
    拼错一个量名(θa 写成 ta)会静默设了个没人读的东西,所以名字不对就在这里报错。
    """
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{where}: bench 必须写成非空映射(台架量名 → 值,如 {{Ua: 220, Fre: 50}}),拿到 {raw!r}"
        )
    bench_models = [model for model in models.values() if model.type == GRID_SIMULATOR_TYPE]
    if not bench_models:
        raise ValueError(
            f"{where}: 声明了 bench: 台架前置,但清单里没有 type: {GRID_SIMULATOR_TYPE} 的型号 —— "
            f"这些量没有台架可下发"
        )
    quantities = _bench_quantities()
    setpoints: Dict[str, float] = {}
    for name, value in raw.items():
        quantity = quantities.get(name) if isinstance(name, str) else None
        if quantity is None:
            close = difflib.get_close_matches(str(name), list(quantities), n=3, cutoff=0.6)
            raise KeyError(
                f"{where}: 台架量名 {name!r} 不在 {Path(BENCH_PARAMS_RELATIVE).name} 的台架量里"
                f"{f'(最接近的: {close})' if close else ''};可用的量: {sorted(quantities)}"
            )
        points = [
            model.point_table[quantity] for model in bench_models if quantity in model.point_table
        ]
        if not points:
            raise KeyError(
                f"{where}: 台架量 {name!r} 落到读数键 {quantity!r},但台架型号 "
                f"{[model.name for model in bench_models]} 的点表里没有它;"
                f"台架能设的量: {sorted({key for model in bench_models for key in model.point_table})}"
            )
        for point in points:
            if not point.writable:
                raise ValueError(
                    f"{where}: 台架点 {quantity!r} 没有声明 writable,不能下发"
                    f"(在台架型号的点表里给这一项加 writable: true)"
                )
        setpoints[quantity] = _parse_bench_value(value, f"{where} 台架量 {name!r}", points[0])
    return MappingProxyType(setpoints)


def _parse_bench_value(raw: Any, where: str, point: Point) -> float:
    """台架前置的值:必须是数值,不是名字。

    台架点现在按 ascii 占位(地址还是 TODO),写路径编不出 ascii —— 这一档就只查"是不是数值";
    真机编码到位后走与写路径同一个函数(_parse_setting_value),不另开一套口径。
    """
    value = coerce_number(raw)
    if value is None or isinstance(raw, bool):
        raise ValueError(
            f"{where}: 值必须是数值(台架要的是数而不是名字:接线方式这类枚举先在"
            f"configs/import/params.yaml 里落成整数编码),拿到 {raw!r}"
        )
    if point.encoding == "ascii":
        return value
    return _parse_setting_value(raw, where, point)


@lru_cache(maxsize=1)
def _bench_quantities() -> Mapping[str, str]:
    """台架量名 → 台架点表的读数键,读的是导入器用的同一份 configs/import/params.yaml。

    拼错在装配前报错而不是运行时:`Ua` 与 `Ub` 是两个量,写错一个字母就是另一件事。
    """
    bench_params_path = paths.resource(BENCH_PARAMS_RELATIVE)
    with bench_params_path.open("r", encoding="utf-8") as handle:
        params = yaml.safe_load(handle) or {}
    return MappingProxyType(
        {
            name: entry["quantity"]
            for name, entry in params.items()
            if isinstance(entry, dict) and entry.get("device") == "bench"
        }
    )


def _parse_setting_step(
    model: Model, case_id: str, index: int, step: Any
) -> SettingStep | ManualStep:
    where = f"setting_cases 条款 {case_id!r} 第 {index + 1} 步"
    if not isinstance(step, dict):
        raise ValueError(
            f"{where}: 必须写成映射,可用键: {list(SETTING_STEP_KEYS)},拿到 {step!r}"
        )
    _reject_unknown_keys(where, step, SETTING_STEP_KEYS)
    if "manual" in step:
        return _parse_manual_step(step, where)
    missing = [key for key in SETTING_WRITE_STEP_KEYS if key not in step]
    if missing:
        raise ValueError(
            f"{where}: 缺少 {missing},自动回环的每一步都要写全 {list(SETTING_WRITE_STEP_KEYS)}"
            f"(写哪个点、写什么值、读回来应该是什么;人工操作写成 {{manual, expect}})"
        )
    key = step["write"]
    point = model.point_table.get(key) if isinstance(key, str) else None
    if point is None:
        raise KeyError(
            f"{where}: 型号 {model.name!r} 没有点 {key!r},可用的读数键: {sorted(model.point_table)}"
        )
    if not point.writable:
        raise ValueError(
            f"{where}: 型号 {model.name!r} 的点 {key!r} 没有声明 writable,不能写;"
            f"可写的点: {[k for k, p in model.point_table.items() if p.writable]}"
            f"(在型号的点表里给这一项加 writable: true)"
        )
    expect = step["expect"]
    if not isinstance(expect, (bool, int, float, str)):
        raise ValueError(
            f"{where}: expect 必须是单个值(数字 / 字符串 / 布尔),不能是区间或列表"
            f"(区间判定归判据集),拿到 {expect!r}"
        )
    return SettingStep(
        write=key, value=_parse_setting_value(step["value"], where, point), expect=expect
    )


def _parse_manual_step(step: Mapping, where: str) -> ManualStep:
    """人工操作的一步:做什么 + 由操作员按什么确认。

    写了 write / value 就是清单写错:人工操作没有可写的点,而"写一半 + 人确认另一半"
    在报告里读不出到底哪半算数。反过来 expect 必须是文字说明 —— 一个人工步骤带着
    机读判据,下一步就会有人把它当自动步骤判过,那正是本票要防的假通过。
    """
    # expect 两种步骤都有;人工操作不共享的是写那两个(write / value)。
    extra = [key for key in ("write", "value") if key in step]
    if extra:
        raise ValueError(
            f"{where}: manual 步骤没有可写的点,不能同时写 {extra};"
            f"人工操作只写 manual(做什么)与 expect(由谁按什么确认),"
            f'如 {{manual: "切换上位机通信模式为 HDLC", expect: "由操作员确认已切换"}}'
        )
    action = step["manual"]
    if not isinstance(action, str) or not action.strip():
        raise ValueError(
            f'{where}: manual 必须是这一步人工操作做什么(非空字符串,如 "切换上位机通信模式"),'
            f"拿到 {action!r}"
        )
    if "expect" not in step:
        raise ValueError(
            f"{where}: manual 步骤缺少 expect(由谁按什么确认的文字说明)"
        )
    expect = step["expect"]
    if not isinstance(expect, str) or not expect.strip():
        raise ValueError(
            f'{where}: manual 步骤的 expect 必须是文字说明(非空字符串,如 "由操作员确认已切换"),'
            f"拿到 {expect!r} —— 人工操作没有机读判据,它不判通过也不判失败"
        )
    return ManualStep(action=action, expect=expect)


def _parse_setting_value(raw: Any, where: str, point: Point) -> float:
    """写下去的值:必须是数值,而且按这个点的 encoding / length 真能编码进去。


    "与 encoding 相容"这一关交给 encode_value —— 写路径用的就是同一个函数,
    所以装配期报错的就是运行时写不进去的那些值,不是另一套口径。BCD 点的 value 写成
    "120000" 也是这个路子:按数值收下,再按点的位数补前导零。
    """
    value = coerce_number(raw)
    if value is None:
        raise ValueError(
            f"{where}: {point.key!r} 的 value 必须是数值(按点表 encoding {point.encoding!r} "
            f"编码写下去;非数字字符没法编成寄存器字节),拿到 {raw!r}"
        )
    try:
        encode_value(
            value,
            point.encoding,
            point.length,
            point.scale,
            point.byteorder,
            point.word_order,
        )
    except ValueError as exc:
        raise ValueError(
            f"{where}: {point.key!r} 的 value {raw!r} 与点表项不相容,写不进去: {exc}"
        ) from None
    return value


def _parse_test_matrix(raw: Any, models: Mapping[str, Model]) -> Mapping[str, Tuple[Selector, ...]]:
    """适用矩阵:型号 → 用例编号 / 标签。型号写错在装配前就报错;
    编号和标签对不对要等收集到用例才知道(另一半校验在 testplatform/plugin.py)。"""
    matrix: Dict[str, Tuple[Selector, ...]] = {}
    for model_name, entries in (raw or {}).items():
        where = f"test_matrix 型号 {model_name!r}"
        if model_name not in models:
            raise KeyError(f"{where}: 引用了不存在的型号,已声明的型号: {sorted(models)}")
        if not isinstance(entries, list):
            raise ValueError(f"{where}: 必须写成列表(用例编号或 tag:<标签>),拿到 {entries!r}")
        matrix[model_name] = parse_matrix_entries(model_name, entries)
    return MappingProxyType(matrix)


def validate_criterion_entries(entries: Any, where: str) -> list:
    """判据条目 schema 校验(设备行内覆盖也要过这一关),返回原条目列表。"""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{where}: 判据必须写成条目列表,拿到 {entries!r}")
    for index, entry in enumerate(entries):
        _validate_criterion_entry(entry, f"{where} 第 {index + 1} 条")
    return entries


def _validate_criterion_entry(entry: Any, where: str) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 判据必须写成映射,拿到 {entry!r}")
    # 边界键只有 min/max(闭区间)、gt/lt(开区间),或者等值判定 equals(不与边界共存);
    # 写成 gte 之类在这里拦下来。
    _reject_unknown_keys(where, entry, CRITERION_KEYS)
    if "quantity" not in entry:
        raise ValueError(f"{where}: 缺少 quantity,可用的字段: {sorted(CRITERION_KEYS)}")
    if "equals" in entry:
        _validate_equals_entry(entry, where)
    severity = entry.get("severity", "reject")
    if severity not in SEVERITIES:
        raise ValueError(f"{where}: 未知的 severity {severity!r},可用: {sorted(SEVERITIES)}")


def _validate_equals_entry(entry: Mapping, where: str) -> None:
    """相等判定:期望值必须是一个具体的值,且不能和区间写在一起。

    两者同写说不清期望是什么(是要落在一个区间里,还是恰好等于某个值),
    而且求值顺序会变成一处沉默的取舍:其中一半被忽略就是这个平台上最危险的假通过。
    """
    written = [key for key in CRITERION_RANGE_KEYS if key in entry]
    if written:
        raise ValueError(
            f"{where}: 判据写的是 equals(相等判定),不能同时写区间 {written};"
            f"一个判据要么判区间({list(CRITERION_RANGE_KEYS)}),要么判等值(equals)"
        )
    equals = entry["equals"]
    if not isinstance(equals, (bool, int, float, str)):
        raise ValueError(
            f"{where}: equals 必须是数字、字符串或布尔值(参数类点常是编码后的字符串),"
            f"拿到 {equals!r}"
        )


def _parse_criteria_profiles(raw: Any) -> Mapping[str, list]:
    profiles: Dict[str, list] = {}
    for name, entries in (raw or {}).items():
        profiles[name] = validate_criterion_entries(entries, f"判据集 {name!r}")
    return MappingProxyType(profiles)


def _parse_protocol_items(raw: Any) -> Mapping[str, Mapping[str, dict]]:
    """协议级数据项目录:协议 → DI → 这一项的固有事实。

    目录不属于任何型号:同一个 DI 在所有型号上的长度 / 编码 / 字节序都一样,
    重复写在每个型号里就是几万份可以各自抄错的东西(scale: 0.01 抄成 0.1 是一个
    看起来正常的假读数)。这里只放"规约决定的东西",型号差异留在点表里覆盖。
    协议名和目录项字段都走闭合集合校验:拼错一个 DI 或字段名在这里就报错。
    """
    catalog: Dict[str, Mapping[str, dict]] = {}
    for protocol, items in (raw or {}).items():
        if protocol not in PROTOCOL_ACCESS:
            raise ValueError(
                f"protocol_items 协议 {protocol!r}: 未知规约,已登记的规约: {sorted(PROTOCOL_ACCESS)}"
            )
        if not isinstance(items, dict) or not items:
            raise ValueError(
                f"protocol_items 协议 {protocol!r}: 必须写成非空映射(DI → 目录项),拿到 {items!r}"
            )
        entries: Dict[str, dict] = {}
        for di, entry in items.items():
            where = f"protocol_items 协议 {protocol!r} 项 {di!r}"
            if not isinstance(di, str) or not di.strip():
                raise ValueError(f"{where}: DI 必须是非空字符串(如 \"00010000\"),拿到 {di!r}")
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
            _reject_unknown_keys(where, entry, PROTOCOL_ITEM_KEYS)
            missing = [field for field in POINT_FIELD_REQUIRED if field not in entry]
            if missing:
                raise ValueError(
                    f"{where}: 缺少字段 {missing},必填字段: {list(POINT_FIELD_REQUIRED)}"
                    f"(长度和编码由 DI 决定,正是从型号里搬进目录的那部分)"
                )
            name = entry.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError(f"{where}: name 必须是 DI 的语义名(非空字符串),拿到 {name!r}")
            _validate_point_fields(where, entry, protocol)
            entries[di] = entry
        catalog[protocol] = MappingProxyType(entries)
    return MappingProxyType(catalog)


def _parse_models(
    raw: Any, profiles: Mapping[str, list], catalog: Mapping[str, Mapping[str, dict]]
) -> Mapping[str, Model]:
    models: Dict[str, Model] = {}
    for name, entry in (raw or {}).items():
        where = f"型号 {name!r}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
        missing = [key for key in MODEL_REQUIRED if key not in entry]
        if missing:
            raise ValueError(f"{where}: 缺少字段 {missing},必填字段: {list(MODEL_REQUIRED)}")
        _reject_unknown_keys(where, entry, MODEL_KEYS)

        protocol = entry["protocol"]
        if protocol not in PROTOCOL_ACCESS:
            raise ValueError(
                f"{where}: 未知规约 {protocol!r},已登记的规约: {sorted(PROTOCOL_ACCESS)}"
            )
        criteria = entry.get("criteria")
        if criteria is not None and criteria not in profiles:
            raise KeyError(
                f"{where}: 引用了不存在的判据集 {criteria!r},已声明: {sorted(profiles)}"
            )
        models[name] = Model(
            name=name,
            type=entry["type"],
            protocol=protocol,
            criteria=criteria,
            capabilities=_parse_capabilities(name, entry["capabilities"]),
            point_table=_parse_point_table(
                name, entry["point_table"], protocol, catalog.get(protocol, {})
            ),
            device_params=_parse_device_params(name, entry.get("device_params")),
        )
        _validate_quantity_names(name, entry["type"], models[name].point_table)
    return MappingProxyType(models)


def _validate_quantity_names(model_name: str, device_type: str, point_table: Mapping[str, Point]) -> None:
    """台架量名就是上游条款的写法,不另造「电压」「频率」。

    旧清单里还写着这两个名字:报错直接说改成什么。不做别名 —— 同一个量两个名字,
    迟早有一处改动没跟上,而那时读数看起来一切正常。
    """
    if device_type != GRID_SIMULATOR_TYPE:
        return
    legacy = {key: MIGRATED_GRID_QUANTITIES[key] for key in point_table if key in MIGRATED_GRID_QUANTITIES}
    if legacy:
        detail = ";".join(f"{old} → {new}" for old, new in legacy.items())
        raise ValueError(
            f"型号 {model_name!r}: 台架量名已改为上游条款的写法,不再有 {sorted(legacy)}。"
            f"你的清单里还写着它们:{detail};三相电压 / 电流是独立的量,不要合成一个。"
            f"完整的量名: Ua/Ub/Uc/Ia/Ib/Ic/θa/θb/θc/QUaUb/QUaUc/Fre/接线方式/MeterConst"
        )


def _parse_capabilities(model_name: str, raw: Any) -> frozenset:
    if not isinstance(raw, list) or not all(isinstance(c, str) and c for c in raw):
        raise ValueError(f"型号 {model_name!r}: capabilities 必须写成能力名列表,拿到 {raw!r}")
    return frozenset(raw)


def _parse_device_params(model_name: str, raw: Any) -> frozenset:
    """型号声明它接受哪些设备级参数。声明了就必填:设备少给一个也是清单写错了。"""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ValueError(f"型号 {model_name!r}: device_params 必须写成参数名列表,拿到 {raw!r}")
    clash = sorted(set(raw) & set(CORE_DEVICE_KEYS))
    if clash:
        raise ValueError(
            f"型号 {model_name!r}: device_params 里的 {clash} 是设备实例的核心键,不能当设备参数声明"
        )
    return frozenset(raw)


def _parse_point_table(
    model_name: str, raw: Any, protocol: str, catalog: Mapping[str, dict]
) -> Mapping[str, Point]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"型号 {model_name!r}: point_table 必须是非空映射(读数键 → 点表项)")
    points: Dict[str, Point] = {}
    for key, entry in raw.items():
        points[key] = _parse_point(model_name, key, entry, protocol, catalog)
    return MappingProxyType(points)


def _parse_point(
    model_name: str, key: Any, entry: Any, protocol: str, catalog: Mapping[str, dict]
) -> Point:
    """点表项二选一:内联(写全 address/length/encoding),或 {item: <DI>} 引用协议目录。"""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"型号 {model_name!r}: 点表项的键是读数键,必须是非空字符串")
    where = f"型号 {model_name!r} 点表项 {key!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: 必须写成映射,拿到 {entry!r}")
    if "item" in entry:
        address, entry = _resolve_item(where, entry, protocol, catalog)
    else:
        missing = [field for field in POINT_REQUIRED if field not in entry]
        if missing:
            raise ValueError(
                f"{where}: 缺少字段 {missing},必填字段: {list(POINT_REQUIRED)}"
                f"(或写成 {{item: <DI>}} 引用 protocol_items 里的协议目录)"
            )
        _reject_unknown_keys(where, entry, POINT_KEYS)
        address = entry["address"]
        if not (
            isinstance(address, int) and not isinstance(address, bool) and address >= 0
        ) and not (isinstance(address, str) and address.strip()):
            raise ValueError(
                f"{where}: address 必须是非负整数或非空字符串(OBIS 逻辑名),拿到 {address!r}"
            )
    return Point(key=key, address=address, **_validate_point_fields(where, entry, protocol))


def _resolve_item(
    where: str, entry: Mapping, protocol: str, catalog: Mapping[str, dict]
) -> Tuple[str, Mapping]:
    """把 {item: <DI>, ...覆盖} 合上目录项,返回 (地址, 合并后的字段)。

    覆盖是通用的:写了哪个字段就覆盖哪个(encoding 和 scale 走同一条路,没有特判)。
    不许两处真相:address 与 item 说的是同一件事;与目录项逐字相同的字段是重复声明,
    两种都在装配前报错 —— 否则同一个事实有两个地方可以改,就一定有地方被改漏。
    """
    if "address" in entry:
        raise ValueError(
            f"{where}: item {entry['item']!r} 已经指明了这个数据项的地址,"
            f"又内联写了 address: {entry['address']!r}"
            f" —— 同一个字段两处真相。引用写法只写 item 和要覆盖的字段,地址由目录决定"
        )
    _reject_unknown_keys(where, entry, POINT_REFERENCE_KEYS)
    item = entry["item"]
    if not isinstance(item, str) or not item.strip():
        raise ValueError(
            f"{where}: item 必须是协议目录里的 DI(非空字符串,如 \"00010000\"),拿到 {item!r}"
        )
    catalog_item = catalog.get(item)
    if catalog_item is None:
        raise KeyError(
            f"{where}: 引用了协议 {protocol!r} 目录里不存在的 DI {item!r};该目录已声明"
            f" {len(catalog)} 个 DI{_closest_di_hint(item, catalog)}。DI 只在 protocol_items 里登记一次,"
            f"新用到的 DI 先写进协议目录"
        )
    overrides = {field: value for field, value in entry.items() if field != "item"}
    repeated = sorted(
        field for field, value in overrides.items() if catalog_item.get(field) == value
    )
    if repeated:
        raise ValueError(
            f"{where}: {repeated} 与 protocol_items 里 {protocol!r} 的 {item!r} 逐字相同,"
            f"是同一个字段的两处写法;删掉这些字段(只写与目录不同的那些,写了的字段就是覆盖)"
        )
    return item, {**catalog_item, **overrides}


def _closest_di_hint(item: str, catalog: Mapping[str, dict]) -> str:
    """拼错的 DI 报"最接近的几个"。目录有两万个 DI 时不把它们全打出来。"""
    close = difflib.get_close_matches(item, list(catalog), n=3, cutoff=0.8)
    return f"(最接近的: {close})" if close else ""


def _validate_point_fields(where: str, entry: Mapping, protocol: str) -> dict:
    """长度 / 编码 / 倍率 / 字节序 / 字序 / 功能码 / 单位:内联写法与引用写法共用这一关。

    引用写法先与目录项合并再走这里,所以覆盖 encoding 和覆盖 scale 经过同一套校验。
    """
    missing = [field for field in POINT_FIELD_REQUIRED if field not in entry]
    if missing:
        raise ValueError(f"{where}: 缺少字段 {missing},必填字段: {list(POINT_FIELD_REQUIRED)}")
    length = entry["length"]
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        raise ValueError(f"{where}: length 必须是正整数字节数,拿到 {length!r}")
    encoding = entry["encoding"]
    if encoding not in POINT_ENCODINGS:
        raise ValueError(f"{where}: 未知编码 {encoding!r},可用: {sorted(POINT_ENCODINGS)}")
    fixed_length = POINT_FIXED_LENGTHS.get(encoding)
    if fixed_length is not None and length != fixed_length:
        raise ValueError(
            f"{where}: encoding {encoding} 的 length 必须是 {fixed_length} 字节,拿到 {length}"
        )
    byteorder = entry.get("byteorder", "big")
    if byteorder not in BYTEORDERS:
        raise ValueError(f"{where}: 未知字节序 {byteorder!r},可用: {sorted(BYTEORDERS)}")
    if encoding == "ascii" and "byteorder" in entry:
        raise ValueError(
            f"{where}: encoding ascii 是文本 token,没有字节序;删掉 byteorder: {byteorder!r}"
        )
    scale = entry.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise ValueError(f"{where}: scale 必须是数字,拿到 {scale!r}")
    word_order = entry.get("word_order", "big")
    if word_order not in BYTEORDERS:
        raise ValueError(f"{where}: 未知字序 {word_order!r},可用: {sorted(BYTEORDERS)}")
    if "word_order" in entry:
        if encoding == "ascii":
            raise ValueError(
                f"{where}: encoding ascii 是文本 token,没有字序;删掉 word_order: {word_order!r}"
            )
        # 只有跨 2 个以上寄存器的值才有"先拼哪个字":length: 2 写了就是记错了轴。
        if length <= 2:
            raise ValueError(
                f"{where}: word_order 只对跨 2 个以上寄存器(≥4 字节)的值有意义,"
                f"length: {length} 没有字序;删掉 word_order: {word_order!r}"
            )
    function_name = entry.get("function", DEFAULT_POINT_FUNCTION)
    if function_name not in POINT_FUNCTIONS:
        raise ValueError(
            f"{where}: 未知功能码 {function_name!r},可用: {sorted(POINT_FUNCTIONS)}"
            f"(不写默认 {DEFAULT_POINT_FUNCTION!r})"
        )
    if "function" in entry and protocol not in FUNCTION_PROTOCOLS:
        raise ValueError(
            f"{where}: 功能码只对 Modbus RTU 有意义,型号的规约是 {protocol!r};"
            f"删掉 function: {function_name!r}"
        )
    unit = entry.get("unit", "")
    if not isinstance(unit, str):
        raise ValueError(f"{where}: unit 必须是字符串,拿到 {unit!r}")
    writable = entry.get("writable", False)
    # 非布尔值(比如写成字符串 "true")在布尔上下文里恒为真:那样的点点点能写。
    if not isinstance(writable, bool):
        raise ValueError(f"{where}: writable 必须是 true / false,拿到 {writable!r}")

    return {
        "length": length,
        "encoding": encoding,
        "scale": float(scale),
        "byteorder": byteorder,
        "word_order": word_order,
        "function": POINT_FUNCTIONS[function_name],
        "unit": unit,
        "writable": writable,
    }


def _parse_devices(raw: Any, models: Mapping[str, Model]) -> Tuple[dict, ...]:
    devices = []
    seen = set()
    for index, spec in enumerate(raw or []):
        where = f"devices 第 {index + 1} 条"
        if not isinstance(spec, dict):
            raise ValueError(f"{where}: 必须写成映射,拿到 {spec!r}")
        device_id = spec.get("id")
        if not device_id:
            raise ValueError(f"{where}: 缺少 id")
        where = f"设备 {device_id!r}"
        for key in MODEL_ONLY_DEVICE_KEYS:
            if key in spec:
                raise ValueError(
                    f"{where}: {key!r} 是型号级参数,必须写在 models 的那个型号上,"
                    f"设备实例只允许写核心键 {sorted(CORE_DEVICE_KEYS)} 和型号声明的设备参数"
                )
        if device_id in seen:
            raise ValueError(f"{where}: id 重复,设备 id 必须唯一")
        seen.add(device_id)

        model_name = spec.get("model")
        if model_name is None:
            raise ValueError(f"{where}: 缺少 model,设备实例只引用型号,不内联点表")
        if model_name not in models:
            raise KeyError(
                f"{where}: 引用了不存在的型号 {model_name!r},已声明的型号: {sorted(models)}"
            )
        model = models[model_name]
        _validate_device_keys(where, spec, model)
        role = spec.get("role", DEFAULT_ROLE)
        if role not in DEVICE_ROLES:
            raise ValueError(
                f"{where}: 未知角色 {role!r},可用角色: {list(DEVICE_ROLES)}"
                f"(不写 role 时缺省 {DEFAULT_ROLE!r})"
            )
        _validate_initial_setpoint(where, spec.get("initial_setpoint"), role)
        _validate_real_mode_addresses(where, model, spec.get("mode"))

        access = spec.get("access")
        if access is None:
            raise ValueError(f"{where}: 缺少 access(接入方式),如 tcp / serial / gateway_ssh")
        # connection 里的硬件事实先校验:CAN 的接口类型决定用哪个驱动(这台机器上装了什么),
        # 写错要在装配前报错并列出可用值 —— 不能等 python-can 抛 "Interface 'socket' not found"。
        # 放在"型号能不能走这个接入方式"之前:字段写错了就先报字段,不拿一句"不能配 can"糊过去。
        if access == "can":
            _validate_can_connection(where, spec.get("connection"))
        elif spec.get("via") is None:
            # 下游设备借上游总线、不建物理连接:它的 connection 归拓扑校验管
            # (_validate_topology 直接拒绝),这里不重复判。
            _validate_stream_connection(where, access, spec.get("connection"), spec.get("mode"))
        allowed = PROTOCOL_ACCESS[model.protocol]
        if access not in allowed and access in TRANSPORT_ONLY_ACCESS:
            raise ValueError(
                f"{where}: 接入方式 {access!r} 的传输已经实现并注册(transport/real_transports.py,"
                f"registry.py 里注册为 {access!r}),但还没有任何规约声明支持它;"
                f"型号 {model_name!r} 的规约是 {model.protocol!r},它跑在 {sorted(allowed)} 上,"
                f"不跑 {access!r}。要接这类设备:先定该设备用什么规约上 {access.upper()}"
                f"(如 EMS 走 REST、HEMS 走 MQTT 上报、工控机走 SSH 命令),"
                f"再把该规约加进 config/manifest.py 的 PROTOCOL_ACCESS"
            )
        if access not in allowed:
            raise ValueError(
                f"{where}: 型号 {model_name!r} 的规约是 {model.protocol!r},"
                f"不能配接入方式 {access!r};该规约允许的接入方式: {sorted(allowed)}"
            )
        if model.protocol in BUS_ADDRESS_PROTOCOLS:
            _validate_bus_address(where, spec.get("bus_address"), model_name, model.protocol)
        validate_criterion_entries(spec.get("criteria_override"), f"{where} criteria_override")
        # 角色在解析时补齐,下游(组合根 / 测试层)只读 spec["role"],不再各自猜缺省值。
        devices.append({**spec, "role": role})
    return tuple(devices)


def _validate_initial_setpoint(where: str, raw: Any, role: str) -> None:
    """周边设备的初始设定:teardown 把台架恢复到这里(如电网模拟器回到 230V/50Hz)。

    设定项名 → 数值,名字就是这台设备设定值对象的字段名(voltage_v / freq_hz),
    由该设备的驱动自己解释 —— 组合根不认识"电压/频率"这些词。

    只有周边设备能声明它。被测设备的收尾只有条款知道(这条用例进没进工厂模式),
    平台不替它猜,也不在整轮结束时往它身上写:整轮结束时设备被人动过,
    比留在工厂模式更难查。所以这里直接拒绝,而不是等 teardown 时乱写一个值。
    """
    if raw is None:
        return
    if role != SUPPORT_ROLE:
        raise ValueError(
            f"{where}: initial_setpoint 只能声明在 role: {SUPPORT_ROLE} 的设备上"
            f"(被测设备的收尾只由用例自己声明,平台不替它猜);这台设备的 role 是 {role!r}"
        )
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{where}: initial_setpoint 必须写成非空映射(设定项名 → 数值,如 "
            f"{{voltage_v: 230, freq_hz: 50}}),拿到 {raw!r}"
        )
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: initial_setpoint 的设定项名必须是非空字符串,拿到 {name!r}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{where}: initial_setpoint 的 {name!r} 必须是数值,拿到 {value!r}")


def _validate_real_mode_addresses(where: str, model: Model, mode: Any) -> None:
    """mode: real 时,点表里还标着占位地址的量上不了真机。

    占位地址写成字符串 TODO:它不是一个地址,真机上没有对应的寄存器 / 字段。等到第一次
    下发设定才在帧里遇到一个 "TODO" 就晚了 —— 那时台架已经收到一帧看不懂的报文,
    报告里只会说“设定失败”。所以在这里报出是哪几个量、缺的是什么。
    mode: simulator 允许:模拟台架按量名应答,不需要真实地址。
    """
    if (mode or DEFAULT_MODE) == "simulator":
        return
    pending = [
        key
        for key, point in model.point_table.items()
        if isinstance(point.address, str) and point.address.strip().upper() == PLACEHOLDER_ADDRESS
    ]
    if pending:
        raise ValueError(
            f"{where}: 型号 {model.name!r} 有 {len(pending)} 个量的地址还是占位("
            f"{PLACEHOLDER_ADDRESS}): {pending};这些量缺真实寄存器定义,不能上真机。"
            f"拿到台架手册或实测后把地址填上,或把该设备的 mode 设为 simulator"
        )


def _validate_can_connection(where: str, raw: Any) -> None:
    """CAN 接入的 connection:键集合是闭合的,接口类型必须在白名单里。

    不配 interface 时由传输层按当前平台给缺省值(real_transports.default_can_interface),
    所以这里只校验"写了就必须对"。
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: connection 必须写成映射,拿到 {raw!r}")
    unknown = sorted(set(raw) - set(CAN_CONNECTION_KEYS))
    if unknown:
        raise ValueError(
            f"{where}: connection 里的未知字段 {unknown},CAN 接入可用字段: {list(CAN_CONNECTION_KEYS)}"
        )
    interface = raw.get("interface")
    if interface is not None and interface not in CAN_INTERFACES:
        raise ValueError(
            f"{where}: 未知 CAN 接口类型 {interface!r},可用: {sorted(CAN_INTERFACES)}"
            f"(socketcan 只在 Linux 上有,Windows 用 pcan / vector / slcan 之一)"
        )


def _validate_stream_connection(where: str, access: str, raw: Any, mode: Any) -> None:
    """serial / tcp / gateway_ssh 的 connection:键闭合,必填键按 mode 校验。

    必填键只在 mode: real 下强制:模拟器不读主机名 / 端口,逼配置里写一个
    没人读的假地址不是校验,只是噪音。拼错的键两种 mode 都拒绝。
    完全没写 connection 也不在这里拒:那种配置在装配时拿不到键就直接 KeyError(吵,不静默),
    而"写了 connection 却少一个必填键"在这里能报出是哪个键、该写什么。
    """
    known_hosts_key = KNOWN_HOSTS_KEYS.get(access)
    if known_hosts_key and isinstance(raw, dict) and raw.get(known_hosts_key) is not None:
        _validate_known_hosts_path(where, known_hosts_key, raw[known_hosts_key])
    if access not in STREAM_ACCESS_CONNECTION_KEYS or raw is None:
        # TRANSPORT_ONLY_ACCESS(http / mqtt / ssh)会走到这里早退:它们的 connection 键集合
        # 还没定,只有上面那道 known_hosts 路径校验(直连 ssh 用得上)。
        # shortcut: 放行那三种接入时,必须同时补各自的闭合键集合,否则拼错的键会静默取缺省
        #           (见 .scratch/device-model/issues/19-http-mqtt-ssh-unreachable.md)。
        return
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: connection 必须写成映射,拿到 {raw!r}")
    real_keys, required = STREAM_ACCESS_CONNECTION_KEYS[access]
    allowed = tuple(real_keys) + STREAM_SIMULATOR_CONNECTION_KEYS
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(
            f"{where}: connection 里的未知字段 {unknown},{access} 接入允许的键: {sorted(allowed)}"
        )
    if (mode or DEFAULT_MODE) != "simulator":
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(
                f"{where}: {access} 接入的 connection 缺少必填字段 {missing},"
                f"该接入方式必填的键: {list(required)}"
                f"(mode: simulator 下这些键不强制写)"
            )
    # local_bus 两种 mode 都校验:模拟模式下写错的值等到切真机才露头。
    if access == "gateway_ssh" and "local_bus" in raw:
        _validate_gateway_local_bus(where, raw["local_bus"])


def _validate_gateway_local_bus(where: str, value: Any) -> None:
    """local_bus 是唯一被拼进 shell 命令的清单值,所以它只收总线名 / 设备节点这几种字符。

    这道白名单挡的是已知形态(分号、管道、`$()`、反引号、空格、换行,以及以 `-` 开头的值);
    拼命令那一侧还有 shlex.quote 兜底 —— 万一有形态混过来,也不会被 shell 解释。
    """
    if isinstance(value, str) and GATEWAY_LOCAL_BUS_PATTERN.fullmatch(value):
        return
    raise ValueError(
        f"{where}: connection.local_bus 的当前值 {value!r} 不能用 —— 它会被拼进网关主机上"
        f"执行的 shell 命令 bus-agent 的 --bus 参数,只允许 1–{GATEWAY_LOCAL_BUS_MAX_LENGTH} 个"
        f"字符,字符集为 [{GATEWAY_LOCAL_BUS_CHARSET}],且不能以 '-' 开头(bus-agent 会把它当成"
        f"选项)。总线名如 can0 / vcan0,设备节点如 /dev/ttyUSB0。"
        f"把 connection.local_bus 改成只含这些字符的值"
    )


def _validate_known_hosts_path(where: str, name: str, value: Any) -> None:
    """known_hosts 路径只查"存在且是文件",不读内容。

    内容(主机密钥)在连接时才由 paramiko 比对:装配期读它,配置写错就变成
    "真机连不上、报认证失败",又一个难查的失败形态。
    """
    path = Path(value).expanduser() if isinstance(value, str) else None
    if path is None or not path.is_file():
        raise ValueError(
            f"{where}: connection.{name} 必须是已存在的 known_hosts 文件路径,拿到 {value!r}"
            f"(支持 ~/ 开头;路径不存在或不是文件都不行)"
            f"—— 没有它主机密钥无法比对,先用 ssh-keyscan -p <端口> <主机> 登记网关指纹"
        )


def _validate_bus_address(where: str, value: Any, model_name: str, protocol: str) -> None:
    """从站地址:设备实例上必须写。Modbus 是 1–247 的整数,DL/T645 是 12 位 BCD 表地址。"""
    if value is None:
        raise ValueError(
            f"{where}: 型号 {model_name!r} 的规约需要从站地址,设备实例必须写 bus_address"
            f"(Modbus 为 1–247 的整数,DL/T645 为 12 位 BCD 表地址如 \"000000000001\")"
        )
    if protocol == "dlt645":
        if not (
            isinstance(value, str)
            and len(value) == DLT645_ADDRESS_DIGITS
            and value.isascii()
            and value.isdigit()
        ):
            raise ValueError(
                f"{where}: bus_address 必须是 {DLT645_ADDRESS_DIGITS} 位 BCD 表地址"
                f'(如 "000000000001"),拿到 {value!r}'
            )
        return
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 247:
        raise ValueError(f"{where}: bus_address 必须是 1–247 的整数(Modbus 从站地址),拿到 {value!r}")


def _validate_topology(devices: Tuple[dict, ...]) -> None:
    """接入从属关系:via 必须指向清单里真实存在的被测设备,且不能成环。

    下游设备的物理连接归上游终端(它共享终端那条总线),所以挂在上面的设备
    不能再写 connection —— 写了也不会生效,静默忽略就是下一个假通过。
    """
    by_id = {spec["id"]: spec for spec in devices}
    for spec in devices:
        upstream = spec.get("via")
        if upstream is None:
            continue
        where = f"设备 {spec['id']!r}"
        if upstream not in by_id:
            raise KeyError(
                f"{where}: via 指向不存在的设备 {upstream!r},已声明的设备: {sorted(by_id)}"
            )
        if by_id[upstream]["role"] != DUT_ROLE:
            raise ValueError(
                f"{where}: via 指向的 {upstream!r} 不是被测设备(role: {DUT_ROLE}),"
                f"不能当接入通路"
            )
        if "connection" in spec:
            raise ValueError(
                f"{where}: 挂在 {upstream!r} 下的设备共享它那条总线,物理连接归上游,"
                f"不能再写 connection"
            )
    for spec in devices:
        chain, current = [], spec
        while current is not None and current.get("via") is not None:
            chain.append(current["id"])
            if current["via"] in chain:
                raise ValueError(f"设备接入关系成环: {' → '.join(chain + [current['via']])}")
            current = by_id[current["via"]]


def _validate_device_keys(where: str, spec: dict, model: Model) -> None:
    """设备实例的键必须落在 核心键 ∪ 该型号的 device_params 里,且声明的参数一个不能少。"""
    allowed = set(CORE_DEVICE_KEYS) | model.device_params
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: 未知字段 {unknown},设备实例可用的键: {sorted(allowed)}"
            f"(核心键 + 型号 {model.name!r} 声明的设备参数 {sorted(model.device_params)})"
        )
    missing = sorted(model.device_params - set(spec))
    if missing:
        raise ValueError(
            f"{where}: 缺少型号 {model.name!r} 声明的设备参数 {missing},"
            f"该型号要求的设备参数: {sorted(model.device_params)}"
        )


def _reject_unknown_keys(where: str, mapping: Mapping, allowed) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ValueError(f"{where}: 未知字段 {unknown},可用字段: {sorted(allowed)}")
