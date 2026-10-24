"""
协议级数据项目录:一个协议对应几万个数据项,而 DI 的长度 / 编码 / 字节序是协议固有信息。

    protocol_items.dlt645['00010000'] = {name: 正向有功总电能, length: 4, encoding: bcd, ...}
    models.dtsd1352.point_table.有功电量 = {item: '00010000', scale: 0.01}

点表项二选一:内联写全(现状,完全兼容),或 item 引用目录 + 只写型号差异。
200 个电表型号 × 每个 200 个点 = 40000 块内联,其中一半是同一个 DI 的第 N 次抄写;
抄错 encoding(bcd 写成 uint)在报告里是一个数量级错、看起来正常的假读数。

两条规矩在这一层:引用必须落在目录里(不存在的 DI 装配前报错),
同一个字段不许有两处真相(item + 内联 address,或与目录逐字相同的重复声明);
但允许显式覆盖 —— 写了的字段就是覆盖,encoding 和 scale 走同一条路。

目录只有几个真实 DI 做样例,取自上游 common/p645dict.py(00010000 / 02010100 / 02020100)。
仓库不是数据仓:真接入某型号时,把它用到的 DI 按需导入,不整份复制那 8 万行。
"""
import copy

import pytest
import yaml

from testplatform.composition.root import CompositionRoot
from testplatform.config.manifest import parse_manifest

DLT645 = "dlt645"
ENERGY = "00010000"  # 上游 p645dict.py: 计量单元电能量(当前)正向有功总电能,4 字节,×0.01
VOLTAGE = "02010100"  # 上游 p645dict.py: 计量单元变量A相电压,2 字节,×0.1
CURRENT = "02020100"  # 上游 p645dict.py: 计量单元变量A相电流,3 字节

PROTOCOL_ITEMS = {
    DLT645: {
        ENERGY: {
            "name": "正向有功总电能",
            "length": 4,
            "encoding": "bcd",
            "byteorder": "little",
            "unit": "kWh",
        },
        VOLTAGE: {
            "name": "A相电压",
            "length": 2,
            "encoding": "bcd",
            "byteorder": "little",
            "unit": "V",
        },
        CURRENT: {
            "name": "A相电流",
            "length": 3,
            "encoding": "bcd",
            "byteorder": "little",
            "unit": "A",
        },
    }
}

# 型号只声明差异:倍率。长度 / 编码 / 字节序 / 单位都在协议目录里,由 DI 决定。
REFERENCE_MODEL = {
    "type": "meter",
    "protocol": DLT645,
    "criteria": "meter_220v",
    "capabilities": ["有功电量", "电压"],
    "point_table": {
        "有功电量": {"item": ENERGY, "scale": 0.01},
        "电压": {"item": VOLTAGE, "scale": 0.1},
    },
}

# 同一个型号的内联写法:每一项自己抄一遍长度 / 编码 / 字节序。
INLINE_MODEL = {
    "type": "meter",
    "protocol": DLT645,
    "criteria": "meter_220v",
    "capabilities": ["有功电量", "电压"],
    "point_table": {
        "有功电量": {
            "address": ENERGY,
            "length": 4,
            "encoding": "bcd",
            "scale": 0.01,
            "byteorder": "little",
            "unit": "kWh",
        },
        "电压": {
            "address": VOLTAGE,
            "length": 2,
            "encoding": "bcd",
            "scale": 0.1,
            "byteorder": "little",
            "unit": "V",
        },
    },
}

METERS_ADDRESS = "000000000001"


def _manifest(model, point_table=None, protocol_items=None):
    return {
        "criteria_profiles": {
            "meter_220v": [
                {"quantity": "电压", "min": 198, "max": 242},
                {"quantity": "有功电量", "min": 0},
            ]
        },
        "protocol_items": copy.deepcopy(
            PROTOCOL_ITEMS if protocol_items is None else protocol_items
        ),
        "models": {
            "dtsd1352": {**copy.deepcopy(model), "point_table": point_table or model["point_table"]}
        },
        "devices": [
            {
                "id": "meter_01",
                "model": "dtsd1352",
                "access": "serial",
                "mode": "simulator",
                "bus_address": METERS_ADDRESS,
                "connection": {
                    "port": "/dev/ttyUSB9",
                    "baudrate": 2400,
                    # 模拟的是那条链路:数据标识 → 原始字节(低字节先、未加 0x33)
                    "simulated_645": {METERS_ADDRESS: {VOLTAGE: "0023", ENERGY: "56341200"}},
                },
            }
        ],
    }


def _build(tmp_path, manifest):
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return CompositionRoot.build(str(path))


def _error(manifest) -> str:
    with pytest.raises((KeyError, ValueError)) as excinfo:
        parse_manifest(manifest)
    return str(excinfo.value)


# ========================= 引用写法:装配后读数与点表项都对 =========================

def test_referenced_points_inherit_length_and_encoding_from_the_catalog(tmp_path):
    """型号只写 {item, scale}:长度 / 编码 / 字节序 / 单位从协议目录继承,读出来的值才对。
    模拟从站里是原始字节 0023 / 56341200,能读出 230.0 V / 1234.56 kWh 就证明
    length=2/bcd 与 length=4/bcd 都真的用上了(bcd 当成 uint 会得到 8960 × 0.1)。"""
    manifest = _manifest(REFERENCE_MODEL)
    root = _build(tmp_path, manifest)
    try:
        model = root.model_of("meter_01")
        voltage, energy = model.point_table["电压"], model.point_table["有功电量"]

        assert (voltage.address, voltage.length, voltage.encoding) == (VOLTAGE, 2, "bcd")
        assert (voltage.byteorder, voltage.unit) == ("little", "V")
        assert (energy.address, energy.length, energy.encoding) == (ENERGY, 4, "bcd")
        assert (energy.byteorder, energy.unit) == ("little", "kWh")
        # scale 是型号写的,不是目录里的
        assert (energy.scale, voltage.scale) == (0.01, 0.1)

        meter = root.get("meter_01")
        meter.connect()
        assert meter.read("电压") == pytest.approx(230.0)
        assert meter.read("有功电量") == pytest.approx(1234.56)
        assert root.reading_keys_of("meter_01") == ("有功电量", "电压")
    finally:
        root.teardown()


def test_inline_and_referenced_points_resolve_to_the_same_point():
    """两种写法解析出逐字段相同的 Point:引用写法不是另一套语义,只是少抄几行。"""
    referenced = parse_manifest(_manifest(REFERENCE_MODEL)).models["dtsd1352"].point_table
    inline = parse_manifest(_manifest(INLINE_MODEL)).models["dtsd1352"].point_table

    assert dict(referenced) == dict(inline)


# ========================= 覆盖:写了的字段就是覆盖,不是只有 scale 特判 =========================

def test_scale_override_on_a_referenced_point_takes_effect(tmp_path):
    point_table = {"有功电量": {"item": ENERGY, "scale": 1}, "电压": {"item": VOLTAGE, "scale": 0.1}}
    root = _build(tmp_path, _manifest(REFERENCE_MODEL, point_table))
    try:
        meter = root.get("meter_01")
        meter.connect()
        assert root.model_of("meter_01").point_table["有功电量"].scale == 1
        assert meter.read("有功电量") == pytest.approx(123456.0)
    finally:
        root.teardown()


def test_encoding_override_on_a_referenced_point_takes_effect(tmp_path):
    """覆盖 encoding 与覆盖 scale 走同一条路:合并后的字段整体过同一套校验。
    bcd → uint 后同一段原始字节 0023 读成 0x2300 = 8960(倍率没覆盖,目录也不写倍率,
    所以取缺省的 1.0);bcd 会读成 2300 —— 这两条分不出编码就是覆盖没生效。"""
    point_table = {
        "有功电量": {"item": ENERGY, "scale": 0.01},
        "电压": {"item": VOLTAGE, "encoding": "uint"},
    }
    root = _build(tmp_path, _manifest(REFERENCE_MODEL, point_table))
    try:
        point = root.model_of("meter_01").point_table["电压"]
        assert (point.length, point.encoding) == (2, "uint")  # 长度仍继承目录
        meter = root.get("meter_01")
        meter.connect()
        assert meter.read("电压") == pytest.approx(8960.0)
    finally:
        root.teardown()


# ========================= 闭合集合:DI 必须在目录里,字段必须拼对 =========================

def test_missing_di_fails_before_assembly():
    """引用了目录里没有的 DI:装配前报错,并带上型号、点表项、规约、目录规模与最接近的几个 DI。"""
    manifest = _manifest(REFERENCE_MODEL, {"电压": {"item": "02010101", "scale": 0.1}})

    message = _error(manifest)

    assert "dtsd1352" in message
    assert "电压" in message
    assert DLT645 in message
    assert "02010101" in message
    assert "已声明 3 个 DI" in message
    assert VOLTAGE in message  # 最接近的那个


def test_unknown_field_in_a_catalog_item_fails_before_assembly():
    """目录项的字段也是闭合集合:长度拼成 lengh 就报错,不会静默取默认值。"""
    items = {DLT645: {VOLTAGE: {**PROTOCOL_ITEMS[DLT645][VOLTAGE], "lengh": 2}}}

    message = _error(_manifest(REFERENCE_MODEL, protocol_items=items))

    assert "protocol_items" in message
    assert "lengh" in message
    assert "length" in message


def test_catalog_item_needs_the_fields_that_are_protocol_facts():
    """目录项没有 length / encoding 就没有意义:那正是从型号里搬进目录的东西。"""
    items = {DLT645: {VOLTAGE: {"name": "A相电压", "unit": "V"}}}

    message = _error(_manifest(REFERENCE_MODEL, protocol_items=items))

    assert "缺少字段" in message
    assert "length" in message
    assert "encoding" in message


def test_unknown_protocol_under_protocol_items_fails_before_assembly():
    """目录的协议名必须是已登记的规约:拼错一个规约就等于整段目录没生效。"""
    message = _error(_manifest(REFERENCE_MODEL, protocol_items={**PROTOCOL_ITEMS, "dlt645t": {}}))

    assert "dlt645t" in message
    assert DLT645 in message  # 已登记的规约里要列出正确的那个


# ========================= 不许两处真相 =========================

def test_item_plus_inline_address_is_rejected():
    """item 已经指明地址,再内联写一遍 address 就是同一个字段的两处真相。"""
    manifest = _manifest(
        REFERENCE_MODEL, {"电压": {"item": VOLTAGE, "address": VOLTAGE, "scale": 0.1}}
    )

    message = _error(manifest)

    assert "电压" in message
    assert VOLTAGE in message
    assert "address" in message
    assert "两处真相" in message


def test_field_repeated_from_the_catalog_is_rejected():
    """与目录项逐字相同的字段是重复声明(不是覆盖):同一个事实有两个地方可以改。"""
    manifest = _manifest(REFERENCE_MODEL, {"电压": {"item": VOLTAGE, "length": 2, "scale": 0.1}})

    message = _error(manifest)

    assert "电压" in message
    assert "length" in message
    assert "逐字相同" in message
