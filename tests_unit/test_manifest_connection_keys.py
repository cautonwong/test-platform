"""serial / tcp / gateway_ssh 三族的 connection 键集合是闭合的。

拼错一个键(baudrate → baudrat)以前被静默忽略、装配时取缺省(19200 变 9600),
现场表现为"设备连上了但读数是乱的"——比连不上更难查。这里锁住三件事:
未知键报错并列出允许的键、real 模式缺必填键报错、模拟器专用键不被误伤。
"""
import pytest

from testplatform.config.manifest import parse_manifest

# vendor_ascii 的接入方式是 tcp / serial / gateway_ssh 三族,一份型号就能测遍三族。
_MODEL = {
    "type": "inverter",
    "protocol": "vendor_ascii",
    "capabilities": ["SOC"],
    "point_table": {"SOC": {"address": 0, "length": 6, "encoding": "ascii"}},
}


def _manifest(access: str, connection, mode: str) -> dict:
    return {
        "models": {"inv_series": _MODEL},
        "devices": [
            {"id": "inv_01", "model": "inv_series", "access": access, "mode": mode, "connection": connection}
        ],
    }


@pytest.mark.parametrize("mode", ["real", "simulator"])
def test_a_misspelled_connection_key_is_rejected_in_both_modes(mode):
    """拼错的键两种 mode 都要拒:模拟模式下一字不差地读同一份 connection。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest("serial", {"port": "COM3", "baudrat": 19200}, mode))

    message = str(caught.value)
    assert "inv_01" in message
    assert "baudrat" in message
    assert "baudrate" in message  # 允许的键要列出来,否则只看到一句"未知字段"


@pytest.mark.parametrize("mode", ["real", "simulator"])
def test_a_misspelled_inter_frame_delay_key_is_rejected_before_assembly(mode):
    """帧间隔拼成 inter_frame_delay(少了 _ms)时就静默丢掉配置:严格总线上依旧粘帧,
    现场只能归到"设备莫明其妙不回话"。装配前报错并列出可用键。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest("serial", {"port": "COM3", "inter_frame_delay": 4}, mode))

    message = str(caught.value)
    assert "inv_01" in message
    assert "inter_frame_delay" in message  # 拼错的那个键
    assert "inter_frame_delay_ms" in message  # 正确的那个键要在"可用键"里


def test_a_configured_inter_frame_delay_passes_the_closed_set():
    """合法的帧间隔不能被清单拒绝;缺省不写也不强制(它是可选硬件参数)。"""
    connection = {"port": "COM3", "baudrate": 9600, "inter_frame_delay_ms": 4}

    assert parse_manifest(_manifest("serial", connection, "real")).devices[0]["connection"] == connection
    assert parse_manifest(_manifest("serial", {"port": "COM3"}, "real")).devices[0]["connection"]


@pytest.mark.parametrize(
    "access, connection, required",
    [
        ("serial", {"baudrate": 19200}, "port"),
        ("tcp", {"port": 502}, "host"),
        ("gateway_ssh", {"gateway_host": "10.0.0.5", "gateway_user": "test"}, "local_bus"),
    ],
)
def test_a_missing_required_connection_key_is_rejected_before_assembly(access, connection, required):
    """real 模式缺必填键(串口没 port、TCP 没 host)就是清单写错了。
    留给装配时的一句 KeyError 说不出"该接入方式要写什么"。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest(access, connection, "real"))

    message = str(caught.value)
    assert "inv_01" in message
    assert required in message


@pytest.mark.parametrize(
    "access, connection",
    [
        ("serial", {"port": "/dev/ttyUSB0", "baudrate": 9600, "simulated_response": "76.5 51.2 -3.4"}),
        ("tcp", {"simulated_response": "3200.5 230.1 50.0"}),
        ("gateway_ssh", {"gateway_host": "10.0.0.5", "gateway_user": "test", "local_bus": "can0",
                         "gateway_key_path": "~/.ssh/id_rsa", "simulated_response": "6100.0 231.5 49.9"}),
        ("serial", {"simulated_645": {"000000000001": {"02010100": "0022"}}}),
        ("tcp", {"simulated_registers": {0: 0x0001, 1: 0xE240, 2: 2300}}),
    ],
)
def test_simulator_connection_keys_pass_the_closed_set(access, connection):
    """模拟器专用键是合法键:收紧键集合不能把现有模拟配置弄红。
    模拟模式也不强制 host / port —— 逼配置写一个没人读的地址不是校验。"""
    manifest = parse_manifest(_manifest(access, connection, "simulator"))

    assert manifest.devices[0]["connection"] == connection


# ---------------------- known_hosts 路径:只查“文件在不在”,不读内容 ----------------------

_GATEWAY = {"gateway_host": "10.0.0.5", "gateway_user": "test", "local_bus": "can0"}


@pytest.mark.parametrize("mode", ["real", "simulator"])
def test_a_known_hosts_path_that_is_not_an_existing_file_is_rejected(tmp_path, mode):
    """写错路径要在装配前就报出键名与值。连接时才读的后果是“真机连不上、报认证失败”
    —— 另一种难查的失败形态。两种 mode 都校验:模拟模式下写错的值真机才露头就太晚了。"""
    for value in (str(tmp_path / "missing_known_hosts"), str(tmp_path)):  # 不存在 / 是目录
        with pytest.raises(ValueError) as caught:
            parse_manifest(_manifest("gateway_ssh", {**_GATEWAY, "gateway_known_hosts": value}, mode))

        message = str(caught.value)
        assert "gateway_known_hosts" in message
        assert repr(value) in message  # 消息里的值按 repr 写,Windows 路径的反斜杠也是 spelled out
        assert "ssh-keyscan" in message  # 告诉人怎么弄出一份来


@pytest.mark.parametrize("mode", ["real", "simulator"])
def test_an_existing_gateway_known_hosts_file_passes_the_closed_set(tmp_path, mode):
    """存在的文件就是合法配置:装配期不读它,内容(主机密钥)留到连接时比对。"""
    path = tmp_path / "known_hosts"
    path.write_text("", encoding="utf-8")

    manifest = parse_manifest(
        _manifest("gateway_ssh", {**_GATEWAY, "gateway_known_hosts": str(path)}, mode)
    )

    assert manifest.devices[0]["connection"]["gateway_known_hosts"] == str(path)


def test_the_direct_ssh_key_is_validated_with_the_same_wording(tmp_path):
    """直连 ssh 的 known_hosts 与网关的 gateway_known_hosts 走同一个校验、同一套措辞:
    两处各写一套校验,下一个人改一处就会漏一处。"""
    with pytest.raises(ValueError) as caught:
        parse_manifest(_manifest("ssh", {"known_hosts": str(tmp_path / "nope")}, "real"))

    message = str(caught.value)
    assert "connection.known_hosts" in message
    assert "ssh-keyscan" in message
    # 字段错先于“这个接入方式今天不能用”报出来 —— 与 CAN / 字节流字段的优先级一致。
    assert "还没有任何规约声明支持它" not in message
