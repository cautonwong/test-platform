"""
北向 MQTT 裸 JSON 载荷的单测：纯函数，不需要 broker、不需要设备、不需要清单。

## 为什么值得单独测

北向载荷是**设备与平台之间的外部契约**，两边各写一份实现（EdgeC 是 C、平台是 Python）。
两边对"什么算合法"理解不一致时，现象是「平台发了命令、设备没反应」——
没有报错、没有日志，只有现场蹲着查。所以规则必须逐条锁死。

这些用例同时也是 C 侧 `driver_mqtt_json.c` 的对照表：改一边就要同步改另一边，
`DOWNLINK_RULES` 里写明了这条约束。
"""
import pytest

from testplatform.protocol import mqtt_json
from testplatform.protocol.mqtt_json import (
    BirthFrame,
    DownlinkCommand,
    MqttJsonFormatError,
    is_birth,
    parse_birth,
    parse_downlink,
)


# ============================== 正常路径 ==============================

def test_parses_a_plain_command():
    cmd = parse_downlink(b'{"p":"RELAY","v":1}')
    assert cmd == DownlinkCommand(point="RELAY", value=1.0)


def test_key_order_and_whitespace_do_not_matter():
    """主站实现差异很常见：键顺序、空白都不该影响解析。"""
    cmd = parse_downlink(b'{ "v" : 55 , "p" : "SOC" }')
    assert cmd.point == "SOC"
    assert cmd.value == 55.0


def test_extra_scalar_keys_are_ignored():
    """多余键忽略：不同主站会带 seq / ts / token 之类的字段。"""
    cmd = parse_downlink(b'{"seq":12,"p":"SOC","v":55,"token":"abc"}')
    assert cmd.point == "SOC"
    assert cmd.value == 55.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b'{"p":"X","v":0}', 0.0),
        (b'{"p":"X","v":-12.5}', -12.5),
        (b'{"p":"X","v":1e3}', 1000.0),
        (b'{"p":"X","v":1.23456789}', 1.23456789),
    ],
)
def test_numeric_forms(raw, expected):
    assert parse_downlink(raw).value == expected


def test_point_name_allows_identifier_chars():
    for name in ("VOLTAGE_A", "a.b-c_1", "TAG9"):
        assert parse_downlink(f'{{"p":"{name}","v":1}}'.encode()).point == name


# ============================== 拒绝路径 ==============================

def test_missing_p_is_rejected():
    """缺 p 就不知道该控制哪个点，必须报错而不是猜。"""
    with pytest.raises(MqttJsonFormatError, match="缺 p"):
        parse_downlink(b'{"v":1}')


def test_missing_v_is_rejected_not_treated_as_zero():
    """缺 v 绝不能当 0：'关到 0' 和 '没给值' 是两件完全不同的事。"""
    with pytest.raises(MqttJsonFormatError, match="缺 v"):
        parse_downlink(b'{"p":"RELAY"}')


@pytest.mark.parametrize(
    "raw",
    [
        b'{"p":"A B","v":1}',          # 空格
        b'{"p":"A\\"B","v":1}',        # 引号（拼进 JSON 会破坏结构）
        b'{"p":"","v":1}',             # 空名
        b'{"p":123,"v":1}',            # 非字符串
    ],
)
def test_unsafe_point_name_is_rejected(raw):
    with pytest.raises(MqttJsonFormatError, match="不是标识符"):
        parse_downlink(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"p":"X","v":"1"}',          # 字符串数字
        b'{"p":"X","v":true}',         # bool 是 int 子类，会被当成 1 静默打开开关
        b'{"p":"X","v":null}',
        b'{"p":"X","v":[1]}',
    ],
)
def test_non_numeric_value_is_rejected(raw):
    with pytest.raises(MqttJsonFormatError, match="不是数字"):
        parse_downlink(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'[]',                          # 数组
        b'"text"',                      # 标量
        b'not json',
        b'',                            # 空
    ],
)
def test_non_object_payload_is_rejected(raw):
    with pytest.raises(MqttJsonFormatError):
        parse_downlink(raw)


def test_nested_extra_key_is_rejected():
    """嵌套结构没有语义，出现了就是发错了；静默忽略会让错误往后飘。"""
    with pytest.raises(MqttJsonFormatError, match="嵌套结构"):
        parse_downlink(b'{"p":"X","v":1,"meta":{"a":1}}')


def test_invalid_utf8_is_rejected():
    with pytest.raises(MqttJsonFormatError, match="UTF-8"):
        parse_downlink(b'{"p":"\xff\xfe","v":1}')


def test_error_messages_name_the_rule():
    """报错必须能指向具体规则，否则现场只能靠猜。"""
    with pytest.raises(MqttJsonFormatError) as exc:
        parse_downlink(b'{"p":"X"}')
    assert "缺 v" in str(exc.value)


# ============================== 上线帧 ==============================

def test_birth_frame_parses():
    frame = parse_birth(b'{"online":true,"ts":1730000000000}')
    assert frame == BirthFrame(online=True, ts_ms=1730000000000)


def test_birth_without_ts_is_allowed():
    """ts 可选：设备刚上电可能还没同步时钟，不能因此判上线失败。"""
    assert parse_birth(b'{"online":true}').ts_ms is None


def test_is_birth_distinguishes_data_frames():
    assert is_birth(b'{"online":true}')
    assert not is_birth(b'{"p":"X","v":1}')
    assert not is_birth(b'garbage')


def test_birth_rejects_non_boolean_online():
    with pytest.raises(MqttJsonFormatError, match="online 必须是布尔"):
        parse_birth(b'{"online":"true"}')


def test_birth_rejects_non_integer_ts():
    with pytest.raises(MqttJsonFormatError, match="ts 必须是整数"):
        parse_birth(b'{"online":true,"ts":1.5}')


# ============================== 规则表本身 ==============================

def test_rules_are_documented():
    """规则表是给 C 侧对照用的，不能空着或变成占位符。"""
    assert len(mqtt_json.DOWNLINK_RULES) >= 5
    assert all(len(rule) > 10 for rule in mqtt_json.DOWNLINK_RULES)
