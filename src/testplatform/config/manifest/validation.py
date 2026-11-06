"""清单的校验:坏声明在装配前报错,不静默忽略。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from testplatform.domain.criteria import coerce_number
from testplatform.protocol.values import encode_value

from .constants import *  # noqa: F401,F403
from .constants import _allowed_access, _known_protocols
from .schema import Manifest, Model, Point

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
    if protocol == "dlt698":
        # 698 的 SA 是 1…16 字节地址；点表与用例目前只用单字节，这里按 1–255 校验。
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 255:
            raise ValueError(
                f"{where}: bus_address 是 698 的服务器地址 SA(单字节 1–255),拿到 {value!r}"
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
