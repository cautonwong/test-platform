"""
点表两项与寄存器寻址有关的字段:function(Modbus 功能码)与 word_order(字序)。

两个都按"写了就必须对"的既有口径收紧:写在对它没有意义的规约或长度上,
帧照样能发出去、读数也看着正常,只是值是错的 —— 那比报错难查得多,所以在装配前拒绝。
点表是配置层数据,这一层用 parse_manifest 这条纯函数缝就够,不需要装配设备。
"""
import pytest

from testplatform.config.manifest import parse_manifest

MODBUS_POINT = {"address": 0, "length": 4, "encoding": "uint", "scale": 0.01, "unit": "kWh"}


def _manifest(protocol, point_entry, type_="meter"):
    return {
        "models": {
            "m": {
                "type": type_,
                "protocol": protocol,
                "criteria": None,
                "capabilities": ["有功电量"],
                "point_table": {"有功电量": point_entry},
            }
        }
    }


def _error(protocol, point_entry) -> str:
    with pytest.raises(ValueError) as excinfo:
        parse_manifest(_manifest(protocol, point_entry))
    return str(excinfo.value)


def test_defaults_keep_the_frames_and_readings_of_before():
    """不写这两个字段 = 读保持寄存器 0x03 + 高字先:和改动前的点表项逐字段一致。"""
    point = parse_manifest(_manifest("modbus_rtu", MODBUS_POINT)).models["m"].point_table["有功电量"]

    assert (point.function, point.word_order) == (0x03, "big")


def test_function_name_is_parsed_into_the_protocol_code():
    point = parse_manifest(
        _manifest("modbus_rtu", {**MODBUS_POINT, "function": "read_input_registers"})
    ).models["m"].point_table["有功电量"]

    assert point.function == 0x04


@pytest.mark.parametrize("protocol", ["vendor_ascii", "dlt645"])
def test_function_is_rejected_for_other_protocols(protocol):
    """功能码是 Modbus 请求帧里的一格:别的规约的帧里没有这个位置,写了也不会生效。"""
    point = {**MODBUS_POINT, "encoding": "bcd", "function": "read_input_registers"}

    message = _error(protocol, point)

    assert "有功电量" in message
    assert "function" in message
    assert protocol in message


def test_unknown_function_name_fails_before_assembly():
    message = _error("modbus_rtu", {**MODBUS_POINT, "function": "read_coils"})

    assert "read_coils" in message
    assert "read_input_registers" in message


def test_word_order_is_rejected_where_it_cannot_mean_anything():
    """length: 2 只占一个寄存器,没有"先拼哪个字"这回事:写在这里说明把字序当成了字节序。"""
    message = _error("modbus_rtu", {**MODBUS_POINT, "length": 2, "word_order": "little"})

    assert "有功电量" in message
    assert "word_order" in message
    assert "length: 2" in message


def test_word_order_is_rejected_for_ascii():
    """ascii 是文本 token,字序对它没有意义。"""
    point = {"address": 0, "length": 8, "encoding": "ascii", "word_order": "little"}

    message = _error("vendor_ascii", point)

    assert "ascii" in message
    assert "word_order" in message


@pytest.mark.parametrize("typo", ["functon", "wordorder"])
def test_misspelled_point_field_still_hits_the_closed_key_set(typo):
    """拼错的字段走既有的关闭键集合:报出未知字段,并列出正确的写法。"""
    message = _error("modbus_rtu", {**MODBUS_POINT, typo: "read_input_registers"})

    assert typo in message
    assert "function" in message
    assert "word_order" in message


# ====================== writable:平台侧的写许可 ======================

def test_writable_defaults_to_false_and_is_parsed_as_a_flag():
    """缺省不可写:只有显式写了 writable: true 的点才允许写。"""
    point = parse_manifest(_manifest("modbus_rtu", MODBUS_POINT)).models["m"].point_table["有功电量"]
    assert point.writable is False

    writable = parse_manifest(
        _manifest("modbus_rtu", {**MODBUS_POINT, "writable": True})
    ).models["m"].point_table["有功电量"]
    assert writable.writable is True


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_writable_must_be_a_boolean(bad):
    """写成字符串 "true" 在布尔上下文里恒为真,那样的点点点能写:装配前拒绝。"""
    message = _error("modbus_rtu", {**MODBUS_POINT, "writable": bad})

    assert "writable" in message
    assert "有功电量" in message
