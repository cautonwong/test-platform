"""生成 P0 规模验收用的合成清单 configs/devices_scale.yaml。

规模(生成脚本,不手写;幂等,重跑覆盖同一份文件):
    200 台被测设备  4 个型号 × 50 台(逆变器 / 电池 / Modbus 电表 / DL/T645 电表)
     63 台终端树    3 台采集终端,每台 via: 挂 20 块表(共享终端那条总线)
      2 台周边设备  role: support 电网模拟器
    合计 265 台

型号、判据集、点表全部从 configs/devices.yaml 读出来复用,只有 DL/T645 的电表与终端
型号是这里新加的(dlt645_meter / dlt645_terminal,与 tests/parallel_topology_check.py
里那份同形):现有清单里没有 645 的型号,而共享总线拓扑只有在 645 上才成立。

这份清单**不进默认 testpaths、不是默认 --device-config**,默认套件不受影响。
跑法:
    python tools/gen_scale_manifest.py
    python -m pytest -q --device-config configs/devices_scale.yaml

数值刻意与既有台架对齐:电表读 1234.56 kWh / 230.0 V,逆变器 3200.5 W / 230.1 V /
50.0 Hz,电池 SOC 76.5% —— 契约用例断言的期望值因此对全量设备同样成立,
规模测量不会混进"读数不对"的失败。
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BASE = REPO / "configs" / "devices.yaml"
OUTPUT = REPO / "configs" / "devices_scale.yaml"

INVERTERS = 50
BATTERIES = 50
MODBUS_METERS = 50
STANDALONE_645_METERS = 50
TERMINALS = 3
METERS_PER_TERMINAL = 20
SUPPORT = 2

# 645 电表的两个点表项:数据标识 → 地址。数值与 Modbus 那份等值(见文件头)。
DLT645_METER_MODEL = {
    "type": "meter",
    "protocol": "dlt645",
    "criteria": "meter_220v",
    "capabilities": ["有功电量", "电压"],
    "point_table": {
        "有功电量": {
            "address": "00010000",
            "length": 4,
            "encoding": "bcd",
            "scale": 0.01,
            "byteorder": "little",
            "unit": "kWh",
        },
        "电压": {
            "address": "02010100",
            "length": 2,
            "encoding": "bcd",
            "scale": 0.1,
            "byteorder": "little",
            "unit": "V",
        },
    },
}

DLT645_TERMINAL_MODEL = {
    "type": "terminal",
    "protocol": "dlt645",
    "capabilities": ["上电运行时间"],
    "point_table": {
        "上电运行时间": {
            "address": "02800085",
            "length": 4,
            "encoding": "int",
            "scale": 1,
            "byteorder": "little",
            "unit": "s",
        },
    },
}

# 645 表地址:12 位 BCD。三个号段分开,现场排障时一眼看出这台表挂在哪。
TERMINAL_BASE = 1          # 000000000001..000000000003
STANDALONE_BASE = 1_000    # 000000001001..
DOWNSTREAM_BASE = 2_000    # 000000002001..

METER_645_VALUE = {"02010100": "0023", "00010000": "56341200"}  # 230.0 V / 1234.56 kWh
TERMINAL_VALUE = {"02800085": "80510100"}  # 86400 s


def _dlt645_address(number: int) -> str:
    return f"{number:012d}"


def _inverters() -> list[dict]:
    return [
        {
            "id": f"inv_scale_{index:03d}",
            "model": "inverter_pv_series",
            "access": "tcp",
            "mode": "simulator",
            "name": f"逆变器-规模-{index:03d}",
            "rated_power_w": 5000,
            "connection": {
                "host": f"192.168.{index // 200}.{index % 200}",
                "port": 502,
                "simulated_response": "3200.5 230.1 50.0",
            },
        }
        for index in range(1, INVERTERS + 1)
    ]


def _batteries() -> list[dict]:
    return [
        {
            "id": f"bat_scale_{index:03d}",
            "model": "battery_lfp_series",
            "access": "serial",
            "mode": "simulator",
            "name": f"电池组-规模-{index:03d}",
            "capacity_ah": 100,
            "connection": {
                "port": f"/dev/ttyUSB{index}",
                "baudrate": 9600,
                "simulated_response": "76.5 51.2 -3.4",
            },
        }
        for index in range(1, BATTERIES + 1)
    ]


# 这座从站要覆盖型号点表里的**每一个**地址,否则块读会读到未声明的寄存器并报
# "模拟从站没有寄存器 N"。新增可写点(比如有功计量模式@3)时必须同步加在这里 ——
# build_manifest() 末尾有一条守卫,漏了会直接报错而不是生成一份跑不通的清单。
MODBUS_REGISTERS = {0: 0x0001, 1: 0xE240, 2: 2300, 3: 0}


def _modbus_meters() -> list[dict]:
    return [
        {
            "id": f"mbus_scale_{index:03d}",
            "model": "dtsd1352_modbus",
            "access": "serial",
            "mode": "simulator",
            "name": f"电表-Modbus-规模-{index:03d}",
            "bus_address": index,
            "connection": {
                "port": f"/dev/ttyS{index}",
                "baudrate": 9600,
                "simulated_registers": MODBUS_REGISTERS,
            },
        }
        for index in range(1, MODBUS_METERS + 1)
    ]


def _standalone_645_meters() -> list[dict]:
    return [
        {
            "id": f"mb645_scale_{index:03d}",
            "model": "dlt645_meter",
            "access": "serial",
            "mode": "simulator",
            "name": f"电表-645-规模-{index:03d}",
            "bus_address": _dlt645_address(STANDALONE_BASE + index),
            "connection": {
                "port": f"/dev/ttyS1{index:02d}",
                "baudrate": 2400,
                "simulated_645": {_dlt645_address(STANDALONE_BASE + index): METER_645_VALUE},
            },
        }
        for index in range(1, STANDALONE_645_METERS + 1)
    ]


def _terminal_tree() -> list[dict]:
    """3 台终端,每台挂 20 块表:表的应答都从终端那条总线来(模拟器里是同一个 dict)。"""
    devices: list[dict] = []
    for terminal_index in range(1, TERMINALS + 1):
        terminal_id = f"terminal_scale_{terminal_index:02d}"
        terminal_address = _dlt645_address(TERMINAL_BASE + terminal_index)
        meters = [
            (
                f"{terminal_id}_meter_{meter_index:02d}",
                _dlt645_address(DOWNSTREAM_BASE + (terminal_index - 1) * 100 + meter_index),
            )
            for meter_index in range(1, METERS_PER_TERMINAL + 1)
        ]
        simulated = {terminal_address: TERMINAL_VALUE}
        simulated.update({address: METER_645_VALUE for _, address in meters})
        devices.append(
            {
                "id": terminal_id,
                "model": "dlt645_terminal",
                "access": "serial",
                "mode": "simulator",
                "name": f"采集终端-规模-{terminal_index:02d}",
                "bus_address": terminal_address,
                "connection": {
                    "port": f"/dev/ttyUSB{100 + terminal_index}",
                    "baudrate": 2400,
                    "simulated_645": simulated,
                },
            }
        )
        devices.extend(
            {
                "id": meter_id,
                "model": "dlt645_meter",
                # 挂在上游终端下:不建物理连接,借终端那条总线(拓扑序由组合根保证)。
                "via": terminal_id,
                "access": "serial",
                "bus_address": address,
                "name": f"电表-645-{terminal_id}-{meter_index:02d}",
            }
            for meter_index, (meter_id, address) in enumerate(meters, start=1)
        )
    return devices


def _support() -> list[dict]:
    return [
        {
            "id": f"grid_sim_scale_{index:02d}",
            "model": "grid_simulator_3ph",
            "role": "support",
            "access": "tcp",
            "mode": "simulator",
            "name": f"电网模拟器-规模-{index:02d}",
            "connection": {
                "host": f"192.168.10.{index}",
                "port": 502,
                "simulated_response": "230.0 50.0",
            },
        }
        for index in range(1, SUPPORT + 1)
    ]


def build_manifest() -> dict:
    base = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    models = dict(base["models"])
    models["dlt645_meter"] = DLT645_METER_MODEL
    models["dlt645_terminal"] = DLT645_TERMINAL_MODEL
    devices = (
        _inverters()
        + _batteries()
        + _modbus_meters()
        + _standalone_645_meters()
        + _terminal_tree()
        + _support()
    )
    _assert_registers_cover_every_point(models, devices)
    return {
        # 不写 test_matrix:不进矩阵的型号不受限、全跑,规模测量才能覆盖全部契约用例。
        "criteria_profiles": base["criteria_profiles"],
        "models": models,
        "devices": devices,
    }


def _assert_registers_cover_every_point(models: dict, devices: list) -> None:
    """模拟从站声明的寄存器必须覆盖型号点表里的每个地址。

    漏一个的表现是块读读到未声明的寄存器:"模拟从站没有寄存器 3" —— 报错在装配后的
    第一次读数里,离真正的原因(生成器少写一行)很远。所以在生成阶段就拦。
    """
    for device in devices:
        declared = device.get("connection", {}).get("simulated_registers")
        if declared is None:
            continue
        points = models[device["model"]]["point_table"]
        needed = {
            point["address"]
            for point in points.values()
            if isinstance(point.get("address"), int)
        }
        missing = sorted(needed - set(declared))
        if missing:
            raise SystemExit(
                f"生成失败:{device['id']}(型号 {device['model']})的 simulated_registers "
                f"缺少地址 {missing};型号点表里有这些点,块读会读到未声明的寄存器。"
                f"把这些地址补进 MODBUS_REGISTERS。"
            )


HEADER = """\
# 由 tools/gen_scale_manifest.py 生成,不要手改:改规模就改脚本再重跑。
# P0 规模验收清单:200 台被测设备(4 型号 × 50)+ 3 台终端 × 每台 20 块表 + 2 台周边设备。
# 不进默认 testpaths、不是默认 --device-config,默认套件不受影响。
# 跑法: python -m pytest -q --device-config configs/devices_scale.yaml
"""


def main() -> int:
    manifest = build_manifest()
    body = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=200)
    OUTPUT.write_text(HEADER + body, encoding="utf-8")
    devices = manifest["devices"]
    roles = [spec.get("role", "dut") for spec in devices]
    print(
        f"{OUTPUT}: {len(devices)} 台设备 "
        f"(被测 {roles.count('dut')} / 周边 {roles.count('support')}),"
        f"型号 {len(manifest['models'])} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
