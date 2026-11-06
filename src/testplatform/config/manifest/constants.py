"""清单的常量与协议访问白名单。"""
from __future__ import annotations

import re
from typing import Dict, Tuple

from testplatform.config import protocols as protocol_registry
from testplatform.protocol.modbus_rtu import READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS
from testplatform.protocol.values import DECODERS

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
    # 族 B：DUT 当 DL/T 698.45 从站，平台当主站去问属性。今天只走 tcp
    # （台架把产品的 unix socket 桥成 TCP）与 serial（现场串口直连）。
    "dlt698": frozenset({"tcp", "serial"}),
}


def _allowed_access(protocol: str) -> frozenset | None:
    """这个协议允许的接入方式:驱动注册的优先,静态表兜底。

    两边都有却不一致时直接报错:静默选一边会让同一种规约在不同入口表现不同。
    """
    static = PROTOCOL_ACCESS.get(protocol)
    registered = protocol_registry.access_of(protocol)
    if static is not None and registered is not None and static != registered:
        raise ValueError(
            f"协议 {protocol!r} 的接入方式在静态表与驱动注册里不一致: "
            f"{sorted(static)} vs {sorted(registered)}"
        )
    return registered if registered is not None else static


def _known_protocols() -> list[str]:
    return sorted(set(PROTOCOL_ACCESS) | set(protocol_registry.registered_protocols()))

# 传输已实现、但没有任何规约声明支持这三种接入方式:
# transport/real_transports.py 里有 RealHttpTransport / RealMqttTransport / RealSshTransport,
# registry.py 里注册为 http / mqtt / ssh。它们在清单里暂时不可达,不是因为没写,而是因为
# 今天没有设备用 REST / MQTT 上报 / 主机命令访问 —— 凭空造一个 http 规约放行,装配时只会
# 停在"未找到设备工厂",那是"能配但必失败"的假象。所以这里只把边界说清楚。
# 要放行:先定该设备用什么规约上这种接入(如 EMS 走 REST、HEMS 走 MQTT 上报),
# 再在对应驱动里 register_protocol(..., access={...}),并同时补 connection 的闭合键集合。
TRANSPORT_ONLY_ACCESS = frozenset({"http", "mqtt", "ssh"})

# 这些规约的请求帧里要带从站地址:设备实例必须写 bus_address。
# Modbus RTU 用 1–247 的整数;Modbus TCP 的这个字段是 MBAP 头里的单元号(常用来区分
# 同一网关后面的串口从站,同样 1–247);DL/T645 用 12 位 BCD 表地址(如 "000000000001")。
BUS_ADDRESS_PROTOCOLS = frozenset({"modbus_rtu", "modbus_tcp", "dlt645", "dlt698"})
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
