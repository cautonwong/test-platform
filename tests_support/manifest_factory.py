"""造一份**能装配**的清单:型号、判据集、设备都已就位,用例只在需要的地方改一处。

为什么不去读 `configs/devices.yaml`:这些用例的价值正是不依赖出厂清单 ——
出厂清单加一台设备、换一个型号,测试就跟着变,测的就不再是这一段代码了。

    MANIFEST = manifest(inverters=2, meters=1)        # 2 台逆变器 + 1 台电表
    manifest(mutate=lambda m: m["devices"][0].pop("rated_power_w"))   # 只改一处
    build(tmp_path, manifest(inverters=2))            # 直接装配出组合根(用完 teardown)

故意写坏的清单也走这里:那类用例测的是校验分支,要的是"一份合法清单再掰坏一处",
那一处就写在 `mutate` 里,不另抄一份清单。`mutate` 收到的是**每次新造的副本**,
所以掰坏型号不会漏到别的用例身上。
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from testplatform.composition.root import CompositionRoot

INVERTER_MODEL = {
    "type": "inverter",
    "protocol": "vendor_ascii",
    "criteria": "grid_50hz",
    "capabilities": ["有功功率", "电压", "频率"],
    "device_params": ["rated_power_w"],
    "point_table": {
        "有功功率": {"address": 0, "length": 8, "encoding": "ascii", "unit": "W"},
        "电压": {"address": 1, "length": 7, "encoding": "ascii", "unit": "V"},
        "频率": {"address": 2, "length": 7, "encoding": "ascii", "unit": "Hz"},
    },
}

MODBUS_METER_MODEL = {
    "type": "meter",
    "protocol": "modbus_rtu",
    "criteria": "meter_220v",
    "capabilities": ["有功电量", "电压"],
    "point_table": {
        "有功电量": {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01, "unit": "kWh"},
        "电压": {"address": 2, "length": 2, "encoding": "uint", "scale": 0.1, "unit": "V"},
    },
}

GRID_SIMULATOR_MODEL = {
    "type": "grid_simulator",
    "protocol": "vendor_ascii",
    "capabilities": ["Ua", "Fre"],
    "point_table": {
        "Ua": {"address": 0, "length": 7, "encoding": "ascii", "unit": "V"},
        "Fre": {"address": 1, "length": 7, "encoding": "ascii", "unit": "Hz"},
    },
}

DLT645_TERMINAL_MODEL = {
    "type": "terminal",
    "protocol": "dlt645",
    # 不写 criteria:上游没有终端自身的验收判据,编一个阈值就是假结论。
    "capabilities": ["上电运行时间"],
    "point_table": {
        "上电运行时间": {
            "address": "02800085",
            "length": 4,
            "encoding": "int",
            "scale": 1,
            "byteorder": "little",
            "unit": "s",
        }
    },
}

DLT645_METER_MODEL = {
    "type": "meter",
    "protocol": "dlt645",
    "criteria": "meter_220v",
    "capabilities": ["电压", "正向有功总电能"],
    # 点表项的 address 是 4 字节数据标识,和上游 common/p645dict.py 的键同一个写法。
    "point_table": {
        "电压": {
            "address": "02010100",
            "length": 2,
            "encoding": "bcd",
            "scale": 0.1,
            "byteorder": "little",
            "unit": "V",
        },
        "正向有功总电能": {
            "address": "00010000",
            "length": 4,
            "encoding": "bcd",
            "scale": 0.01,
            "byteorder": "little",
            "unit": "kWh",
        },
    },
}

GRID_50HZ = [{"quantity": "grid_freq_hz", "min": 45, "max": 55}]
METER_220V = [
    {"quantity": "电压", "min": 198, "max": 242},
    {"quantity": "有功电量", "min": 0},
]


def _ids(prefix: str, count_or_ids) -> list[str]:
    """`2` → inv_01 / inv_02;给一串 id 就用给的(用例要按名字点名设备时)。"""
    if isinstance(count_or_ids, int):
        return [f"{prefix}_{index:02d}" for index in range(1, count_or_ids + 1)]
    return list(count_or_ids)


def _inverter(device_id: str, response: str = "3000.0 230.0 50.0") -> dict:
    return {
        "id": device_id,
        "model": "inverter_pv_series",
        "access": "tcp",
        "mode": "simulator",
        "rated_power_w": 5000,
        "connection": {"simulated_response": response},
    }


def meter_device(
    device_id: str,
    bus_address: int = 1,
    voltage: int = 2300,
    energy: tuple[int, int] = (0x0001, 0xE240),
) -> dict:
    """一台 Modbus 电表设备:模拟从站里的寄存器默认就是 configs 那一份
    (0x0001E240 × 0.01 = 1234.56 kWh,2300 × 0.1 = 230.0 V)。"""
    return {
        "id": device_id,
        "model": "dtsd1352_modbus",
        "access": "serial",
        "mode": "simulator",
        "bus_address": bus_address,
        "connection": {
            "port": "/dev/ttyUSB9",
            "baudrate": 9600,
            "simulated_registers": {0: energy[0], 1: energy[1], 2: voltage},
        },
    }


def _grid_simulator(device_id: str) -> dict:
    return {
        "id": device_id,
        "model": "grid_simulator_3ph",
        "role": "support",
        "access": "tcp",
        "mode": "simulator",
        "connection": {"simulated_response": "230.0 50.0"},
    }


def manifest(*, inverters=0, meters=0, support=False, mutate=None) -> dict:
    """合法清单。`inverters` / `meters` 是台数或 id 列表;`support` 追加一台周边设备(role: support)。

    `mutate(manifest)` 在交回去之前改一处 —— 用例要验的就是"改了这一处之后会怎样"。
    """
    devices = [_inverter(device_id) for device_id in _ids("inv", inverters)]
    devices += [
        meter_device(device_id, bus_address)
        for bus_address, device_id in enumerate(_ids("meter", meters), start=1)
    ]
    models = {}
    profiles = {}
    if inverters:
        models["inverter_pv_series"] = INVERTER_MODEL
        profiles["grid_50hz"] = GRID_50HZ
    if meters:
        models["dtsd1352_modbus"] = MODBUS_METER_MODEL
        profiles["meter_220v"] = METER_220V
    if support:
        devices.append(_grid_simulator("grid_sim_01"))
        models["grid_simulator_3ph"] = GRID_SIMULATOR_MODEL

    # 型号与判据集是模块常量:交出去之前必须复制,否则用例里 mutate 改一处型号(点表字段、
    # 协议)会永久改到常量上,后面所有用例看到的是被改过的点表 —— 顺序相关的假红/假绿。
    built = {"models": copy.deepcopy(models), "devices": devices}
    if profiles:
        built["criteria_profiles"] = copy.deepcopy(profiles)
    if mutate is not None:
        mutate(built)
    return built


def dlt645_bench(simulated_645: dict, *, meters=("meter_a",), energy=True, mutate=None) -> dict:
    """一条 DL/T645 总线:终端兼通路,下游电表用 `via:` 挂在它下面(自己没有物理连接)。

    每条请求帧带自己的 bus_address,由协议层选中从站,总线归终端;所以应答都从终端那条
    模拟链路来 —— `simulated_645` 是"地址 → 数据标识 → 原始字节"。

    `energy=False` 只声明「电压」一个点:证据类用例靠"一帧一条记录"断言,多一个点就多一帧。
    """
    meter_model = copy.deepcopy(DLT645_METER_MODEL)
    profile = [{"quantity": "电压", "min": 198, "max": 242}]
    if energy:
        profile.append({"quantity": "正向有功总电能", "min": 0})
    else:
        meter_model["point_table"].pop("正向有功总电能")
        meter_model["capabilities"].remove("正向有功总电能")

    devices = [
        {
            "id": "terminal_01",
            "model": "dlt645_terminal",
            "access": "serial",
            "mode": "simulator",
            "bus_address": "000000000001",
            "connection": {"port": "/dev/ttyUSB2", "baudrate": 2400, "simulated_645": simulated_645},
        }
    ]
    for index, meter_id in enumerate(meters, start=2):
        devices.append(
            {
                "id": meter_id,
                "model": "dlt645_meter",
                "via": "terminal_01",
                "access": "serial",
                "bus_address": f"{index:012d}",
            }
        )
    built = {
        "criteria_profiles": {"meter_220v": profile},
        "models": copy.deepcopy(
            {"dlt645_meter": meter_model, "dlt645_terminal": DLT645_TERMINAL_MODEL}
        ),
        "devices": devices,
    }
    if mutate is not None:
        mutate(built)
    return built


def write(tmp_path, manifest_dict) -> str:
    """清单落盘成 `devices.yaml`,返回路径。"""
    path = Path(tmp_path) / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest_dict, allow_unicode=True), encoding="utf-8")
    return str(path)


def build(tmp_path, manifest_dict, *, evidence=None) -> CompositionRoot:
    """清单 → 组合根。用完记得 `root.teardown()`。"""
    return CompositionRoot.build(write(tmp_path, manifest_dict), evidence=evidence)
