"""
型号三段式走清单配置这条缝:CompositionRoot.build(清单路径) 装配出来的设备,
点表 / 能力 / 判据集声明在型号上,设备实例只引用型号名和接入位置。

清单写错一律在装配前报错,并指出是哪一条、错在哪——静默忽略未知字段
会变成假通过,那是这个平台上最危险的 bug。
"""
import copy

import pytest
import yaml

from tests_support.manifest_factory import build, manifest
from testplatform.cases import case
from testplatform.composition.root import CompositionRoot
from testplatform.config.manifest import parse_manifest

# 型号声明一次、两台实例只引用它:判据集里那条功率上限用的是型号参数 rated_power_w,
# 所以照着工厂的 50Hz 判据再加一条(派生边界不该另抄一份型号)。
BASE_MANIFEST = manifest(
    inverters=["inv_a", "inv_b"],
    mutate=lambda m: m["criteria_profiles"]["grid_50hz"].append(
        {"quantity": "output_power_w", "min": 0, "max": {"rated_power_w": 1.1}}
    ),
)


@pytest.fixture()
def manifest():
    return copy.deepcopy(BASE_MANIFEST)


def _build(tmp_path, manifest):
    return build(tmp_path, manifest)


def _build_error(tmp_path, manifest):
    """装配前报错:build 直接抛错,不会造出半个组合根。返回错误信息。"""
    with pytest.raises((KeyError, ValueError)) as excinfo:
        _build(tmp_path, manifest)
    return str(excinfo.value)


@pytest.fixture()
def root(tmp_path):
    root = _build(tmp_path, BASE_MANIFEST)
    yield root
    root.teardown()


# ========================= 三段式:型号声明一次,实例只引用 =========================

def test_same_model_instances_share_one_point_table(root):
    """两台同型号设备共享一份点表声明,读数键与能力从型号上问得到。"""
    assert root.model_of("inv_a") is root.model_of("inv_b")
    assert root.reading_keys_of("inv_a") == ("有功功率", "电压", "频率")
    assert root.reading_keys_of("inv_b") == root.reading_keys_of("inv_a")
    assert root.capabilities_of("inv_a") == frozenset({"有功功率", "电压", "频率"})
    # 配置里点表只写了一遍:同型号加实例不需要重复它
    assert yaml.safe_dump(BASE_MANIFEST, allow_unicode=True).count("point_table:") == 1


def test_device_type_comes_from_the_model(root):
    """设备类型在型号上声明,测试层的类型参数化按型号解析。"""
    assert root.ids_of_type("inverter") == ["inv_a", "inv_b"]
    assert root.capabilities_of("inv_b") == root.capabilities_of("inv_a")


@case(id="M_00_00_01_000003_19", clause="GBT34131-5.2.1", tags=["一致性", "读数"])
@pytest.mark.requires_capability("无功电量")
def test_device_declaring_capability_also_declares_the_reading_key(
    composition_root, inverter, inverter_id
):
    """按读数键取数的契约:设备说支持某能力,就得在点表里有对应的读数键。
    现在的逆变器型号没声明「无功电量」,所以这条在当前清单下自动 skip(不是失败);
    等电表型号声明该能力(票 5a)它会自动开始跑。"""
    assert "无功电量" in composition_root.reading_keys_of(inverter_id)


# ========================= 点表表达 OBIS 地址 ↔ 物理量名 =========================

def test_point_table_carries_obis_addresses_and_physical_names():
    """上游 legacy 平台(common/_meterObisDict.py)的映射语义:OBIS 地址 ↔ 物理量名。
    这里走清单解析这条纯函数缝:点表是配置层数据,不需要装配设备也不需要电表驱动
    (电表的通用读取路径属于票 5a)。"""
    manifest = parse_manifest(
        {
            "models": {
                "dtsd1352": {
                    "type": "meter",
                    "protocol": "dlt645",
                    "criteria": None,
                    "capabilities": ["有功电量", "无功电量"],
                    "point_table": {
                        "正向有功总电能": {
                            "address": "1.0.1.8.0.255",
                            "length": 4,
                            "encoding": "bcd",
                            "scale": 0.01,
                            "byteorder": "little",
                            "unit": "kWh",
                        },
                        "正向无功总电能": {
                            "address": "1.0.3.8.0.255",
                            "length": 4,
                            "encoding": "bcd",
                            "scale": 0.01,
                            "byteorder": "little",
                            "unit": "kvarh",
                        },
                    },
                }
            }
        }
    )

    model = manifest.models["dtsd1352"]
    assert model.reading_keys == ("正向有功总电能", "正向无功总电能")
    point = model.point_table["正向无功总电能"]
    assert (point.address, point.length, point.encoding, point.byteorder) == (
        "1.0.3.8.0.255",
        4,
        "bcd",
        "little",
    )
    assert (point.scale, point.unit) == (0.01, "kvarh")


# ========================= 清单校验:装配前就报错 =========================

def test_device_referencing_unknown_model_fails_before_assembly(tmp_path, manifest):
    manifest["devices"][0]["model"] = "dtsd1352"

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "dtsd1352" in message
    assert "inverter_pv_series" in message


def test_model_protocol_conflicting_with_access_fails_before_assembly(tmp_path, manifest):
    """DL/T645 是串口规约,配成裸 TCP 接入是清单写错了,不能静默装配。"""
    manifest["models"]["inverter_pv_series"]["protocol"] = "dlt645"

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "dlt645" in message
    assert "tcp" in message
    assert "serial" in message


def test_unknown_point_table_field_fails_before_assembly(tmp_path, manifest):
    manifest["models"]["inverter_pv_series"]["point_table"]["电压"]["unit2"] = "V"

    message = _build_error(tmp_path, manifest)

    assert "电压" in message
    assert "unit2" in message
    assert "unit" in message


def test_point_table_entry_missing_field_fails_before_assembly(tmp_path, manifest):
    del manifest["models"]["inverter_pv_series"]["point_table"]["频率"]["length"]

    message = _build_error(tmp_path, manifest)

    assert "频率" in message
    assert "length" in message


def test_ascii_point_with_explicit_byteorder_fails_before_assembly(tmp_path, manifest):
    """ascii 是文本 token,字节序对它没有意义:写了也会被解码器无声吞掉,所以装配前拒绝。"""
    manifest["models"]["inverter_pv_series"]["point_table"]["电压"]["byteorder"] = "big"

    message = _build_error(tmp_path, manifest)

    assert "电压" in message
    assert "ascii" in message
    assert "byteorder" in message


def test_float32_point_with_a_wrong_length_fails_before_assembly(tmp_path, manifest):
    """float32 一定是 4 字节:装配前就报出是哪个点表项,而不是运行时一句 struct.error。"""
    manifest["models"]["inverter_pv_series"]["point_table"]["频率"].update(
        {"length": 2, "encoding": "float32"}
    )

    message = _build_error(tmp_path, manifest)

    assert "频率" in message
    assert "float32" in message
    assert "4" in message


def test_float32_point_with_length_4_passes_assembly(tmp_path, manifest):
    """长度正好 4 是合法的:校验不能把正确的点表项也拒了。"""
    manifest["models"]["inverter_pv_series"]["point_table"]["频率"].update(
        {"length": 4, "encoding": "float32"}
    )

    root = _build(tmp_path, manifest)
    try:
        assert root.model_of("inv_a").point_table["频率"].encoding == "float32"
    finally:
        root.teardown()


# ============== 编码解码契约:ascii 是文本 token,不是二进制数值 ==============

def test_ascii_token_longer_than_the_declared_length_fails_instead_of_truncating(
    tmp_path, manifest
):
    """应荅变长(换量程、换单位、多一位整数)时截断会解出一个少一位的假读数:
    "230.0" 截成 "230." 照样能转成 230.0,报告里看不出来。所以超长必须报错。"""
    manifest["models"]["inverter_pv_series"]["point_table"]["电压"]["length"] = 4

    root = _build(tmp_path, manifest)
    try:
        inverter = root.get("inv_a")
        inverter.connect()
        with pytest.raises(ValueError) as excinfo:
            inverter.read("电压")
    finally:
        root.teardown()

    message = str(excinfo.value)
    assert "电压" in message  # 是哪个点表项
    assert "230.0" in message  # 实测 token
    assert "4" in message  # 声明的上限


def test_ascii_token_exactly_the_declared_length_is_accepted(tmp_path, manifest):
    """恰好等长是正常的:上限是「最长多少字符」,不是「必须等于」。"""
    manifest["models"]["inverter_pv_series"]["point_table"]["电压"]["length"] = 5

    root = _build(tmp_path, manifest)
    try:
        inverter = root.get("inv_a")
        inverter.connect()
        assert inverter.read("电压") == pytest.approx(230.0)
    finally:
        root.teardown()


@pytest.mark.parametrize(
    "bad_bound",
    [pytest.param({"gte": 45}, id="把gt写成gte"), pytest.param({"minimum": 45}, id="拼错的字段名")],
)
def test_unknown_criteria_key_fails_before_assembly(tmp_path, manifest, bad_bound):
    """边界键写错(比如 gte:)以前被静默忽略,变成另一个假通过;现在装配前就报错。"""
    entry = {"quantity": "grid_freq_hz", **bad_bound}
    manifest["criteria_profiles"]["grid_50hz"].append(entry)

    message = _build_error(tmp_path, manifest)

    assert "grid_50hz" in message
    assert list(bad_bound)[0] in message
    assert "min" in message


def test_unknown_criteria_key_in_inline_override_fails_before_assembly(tmp_path, manifest):
    manifest["devices"][0]["criteria_override"] = [{"quantity": "grid_freq_hz", "gte": 55}]

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "gte" in message


def test_device_instance_cannot_inline_model_level_parameters(tmp_path, manifest):
    """点表 / 能力 / 判据集写在型号上;设备实例上重复一遍会被拒绝,免得两份声明漂移。"""
    manifest["devices"][0]["capabilities"] = ["有功功率"]

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "capabilities" in message


def test_unknown_top_level_section_fails_before_assembly(tmp_path, manifest):
    manifest["test_matriks"] = [{"model": "inverter_pv_series", "items": ["电压精度"]}]

    message = _build_error(tmp_path, manifest)

    assert "test_matriks" in message


def test_unknown_device_instance_key_fails_before_assembly(tmp_path, manifest):
    """设备实例的键是闭合集合:拼错一个键(比如 criterria)以前被静默忽略,
    配置看着生效了,实际跑的是默认行为——那是最危险的假通过。"""
    manifest["devices"][0]["criterria"] = "grid_50hz"

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "criterria" in message
    assert "criteria_override" in message  # 可用键要列出来
    assert "rated_power_w" in message  # 型号声明的设备参数也是可用键


def test_misspelled_device_param_fails_instead_of_falling_back_to_default(tmp_path, manifest):
    """把 rated_power_w 拼成 rated_power 必须报错,而不是静默变默认值。"""
    device = manifest["devices"][0]
    device["rated_power"] = device.pop("rated_power_w")

    message = _build_error(tmp_path, manifest)

    assert "inv_a" in message
    assert "rated_power" in message
    assert "rated_power_w" in message


def test_device_missing_declared_param_fails_before_assembly(tmp_path, manifest):
    """型号声明了 device_params 就是必填:设备少给一个也算清单写错。"""
    del manifest["devices"][1]["rated_power_w"]

    message = _build_error(tmp_path, manifest)

    assert "inv_b" in message
    assert "rated_power_w" in message
    assert "inverter_pv_series" in message


def test_device_param_reaches_the_driver(tmp_path, root):
    """设备参数如实下发到驱动,不取默认值:两台逆变器各用各的额定功率。"""
    assert root.get("inv_a").device.rated_power_w == 5000

    manifest = copy.deepcopy(BASE_MANIFEST)
    manifest["devices"][0]["rated_power_w"] = 8000
    other = _build(tmp_path, manifest)
    try:
        assert other.get("inv_a").device.rated_power_w == 8000
    finally:
        other.teardown()
