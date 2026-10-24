"""
台架型号 `grid_simulator_3ph` 的形状:量名、编码、占位地址、迁移提示。

上游条款「调整台体」块里的量是一整块三相设定(Ua/Ub/Uc/Ia/Ib/Ic/θa/θb/θc/QUaUb/QUaUc/Fre
加 接线方式、表常数)。这台型号以前只声明了 电压 / 频率 两个量 —— 前置写得进去也没有
东西能接。这里锁住三件事:

1. 量名沿用上游写法,三相电压 / 电流是独立的量(条款里 Ua 和 Ub 是两件事);
2. 接线方式是枚举、表常数是整数:点表用 uint,不为枚举新增 encoding 类型;
3. 地址是占位(TODO)时:mode: simulator 照常跑,mode: real 在装配期报错并指出是哪个量。
   真实台架的寄存器地址 / 编码 / 倍率还没拿到 —— 编一个"看着像"的地址会让回读一致
   变成假绿,所以宁可报缺。
"""
from pathlib import Path

import pytest
import yaml

from testplatform.config.manifest import PLACEHOLDER_ADDRESS, parse_manifest

REPO = Path(__file__).resolve().parents[1]
FACTORY_MANIFEST = REPO / "configs" / "devices.yaml"
GRID = "grid_sim_01"

# 型号点表里该有的量,按上游写法。顺序即条款里出现的顺序。
THREE_PHASE_QUANTITIES = [
    "Ua",
    "Ub",
    "Uc",
    "Ia",
    "Ib",
    "Ic",
    "θa",
    "θb",
    "θc",
    "QUaUb",
    "QUaUc",
    "Fre",
    "接线方式",
    "MeterConst",
]


def _factory_manifest() -> dict:
    return yaml.safe_load(FACTORY_MANIFEST.read_text(encoding="utf-8"))


def _grid_model(manifest) -> dict:
    return manifest["models"]["grid_simulator_3ph"]


def _grid_device(manifest) -> dict:
    return next(spec for spec in manifest["devices"] if spec["id"] == GRID)


# ===================== 型号声明:13 个核心量,三相各自独立 =====================

def test_the_grid_simulator_model_declares_every_core_quantity():
    model = _grid_model(_factory_manifest())

    assert list(model["point_table"]) == THREE_PHASE_QUANTITIES
    assert set(model["capabilities"]) == set(THREE_PHASE_QUANTITIES)


def test_the_three_phase_voltages_and_currents_are_separate_quantities():
    """条款里 Ua 和 Ub 是两件事:合成一个「电压」就把条款的信息丢在清单里了。"""
    point_table = _grid_model(_factory_manifest())["point_table"]

    assert {"Ua", "Ub", "Uc", "Ia", "Ib", "Ic"} <= set(point_table)
    assert not {"电压", "电流", "频率"} & set(point_table)


def test_the_wiring_mode_and_meter_constant_are_integers_not_floats():
    """接线方式是枚举(3P4W ↔ 某个整数),表常数是整数参数:编码是 uint,
    枚举名 → 整数的对照在 configs/import/params.yaml,不为它新增 encoding 类型。"""
    point_table = _grid_model(_factory_manifest())["point_table"]

    assert point_table["接线方式"]["encoding"] == "uint"
    assert point_table["MeterConst"]["encoding"] == "uint"
    assert point_table["Ua"]["encoding"] == "ascii"  # 模拟量仍是文本 token


# ===================== 占位地址:simulator 能跑,real 装配期拒绝 =====================

def test_every_quantity_is_still_a_placeholder_address():
    """地址留成 TODO 而不是编一个看着像的寄存器:编出来的地址写进去、回读还一致,
    那是最坏的一种假绿。"""
    point_table = _grid_model(_factory_manifest())["point_table"]

    assert {point["address"] for point in point_table.values()} == {PLACEHOLDER_ADDRESS}


def test_the_factory_manifest_parses_because_the_grid_runs_on_the_simulator():
    manifest = parse_manifest(_factory_manifest())

    assert next(spec for spec in manifest.devices if spec["id"] == GRID)["mode"] == "simulator"
    assert len(manifest.models["grid_simulator_3ph"].point_table) == len(THREE_PHASE_QUANTITIES)


def test_a_real_grid_simulator_with_placeholder_addresses_is_refused_before_assembly():
    manifest = _factory_manifest()
    _grid_device(manifest)["mode"] = "real"

    with pytest.raises(ValueError) as caught:
        parse_manifest(manifest)

    message = str(caught.value)
    assert GRID in message  # 是哪台设备
    assert "grid_simulator_3ph" in message  # 哪个型号
    assert "Ua" in message and "接线方式" in message  # 哪几个量
    assert "占位" in message and "缺真实寄存器定义" in message  # 缺的是什么
    assert "simulator" in message  # 现在可以怎么跑


def test_a_real_grid_simulator_with_real_addresses_passes():
    """地址补齐后 real 模式就能装配 —— 守卫只在"还是占位"时拦。"""
    manifest = _factory_manifest()
    _grid_device(manifest)["mode"] = "real"
    for point in _grid_model(manifest)["point_table"].values():
        point["address"] = 0

    assert parse_manifest(manifest).models["grid_simulator_3ph"].point_table["Ua"].address == 0


# ===================== 改名是清单契约变更:给迁移提示,不做别名 =====================

@pytest.mark.parametrize("legacy, replacement", [("电压", "Ua"), ("频率", "Fre")])
def test_the_legacy_quantity_names_are_refused_with_a_migration_hint(legacy, replacement):
    """旧清单里还写着 电压 / 频率:报错要说清改成什么。不做别名 —— 同一个量两个名字,
    迟早有一处改动没跟上,而那时读数看起来一切正常。"""
    manifest = _factory_manifest()
    model = _grid_model(manifest)
    model["point_table"] = {
        legacy: {"address": PLACEHOLDER_ADDRESS, "length": 7, "encoding": "ascii"}
    }
    model["capabilities"] = [legacy]
    _grid_device(manifest)["mode"] = "simulator"

    with pytest.raises(ValueError) as caught:
        parse_manifest(manifest)

    message = str(caught.value)
    assert legacy in message
    assert replacement in message
