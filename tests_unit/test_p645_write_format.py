"""票 44:从上游 p645dict 反查出的写入格式,按它编出来的字节必须与线上字节一致。

不连设备:一侧是 `configs/import/writes.yaml`(DI / 长度 / 类型 / 图例换算),
另一侧是 `protocol/values.py` 与 `protocol/dlt645.py` 两个纯函数。两组装在一起才叫
"写入格式可用" —— 只抄 DI 不验证编码,等于又回到"地址填上了但写不对"。

上游出处由 tools/check_import_sources.py 机械核对(它连上游那一行比对);这里不管上游,
只管"按写下来的格式真的编得出这几个字节"。
"""
from pathlib import Path

import pytest
import yaml

from testplatform.protocol import dlt645, values

REPO = Path(__file__).resolve().parents[1]
WRITES = REPO / "configs" / "import" / "writes.yaml"


def _writes():
    return yaml.safe_load(WRITES.read_text(encoding="utf-8"))


def test_图例位数与长度交叉对得上():
    """图例给几位十进制,长度就得是几位的一半 —— 这两个数来自上游的两处,抄错一处就不相等。"""
    data = _writes()
    legend = data["legend"]
    for di, row in data["fields"].items():
        spec = legend[row["type"]]
        if spec["digits"] is None:
            # String / Integer:上游只给长度,没给字节布局。不许在这里补一个"看着合理"的换算。
            assert row["length"] > 0, di
            continue
        assert spec["digits"] == 2 * row["length"], (di, row["type"], spec["digits"], row["length"])


def test_Date_四字节_BCD():
    """时钟日期 `Date` = YYMMDDWW:年月日星期各两位十进制,一位两 nibble = 1 字节。

    2025-10-20 是星期一,上游名字写着"其中0代表星期天" ⇒ WW=01,拼成十进制 25102001。
    线上低字节先发(与地址域同一条规则),所以首个字节是 WW。
    """
    row = _writes()["fields"]["04000101"]
    assert (row["type"], row["length"]) == ("Date", 4)

    wire = values.encode_value(25102001, "bcd", row["length"], byteorder="little")
    assert wire == bytes.fromhex("01201025")
    # 读方向是写方向的逆运算:同一串字节解回来必须还是那个值,倍率不参与
    assert values.decode_value(wire, "bcd", byteorder="little") == 25102001


def test_String_六字节_通信地址():
    """表地址字段 `String` 6 字节:12 位十进制数字,每字节两位、低字节先发。

    这条比 Date 少一半证据,写在明处:上游图例只给"String"二字与长度 6,**没给每字节怎么编**。
    "每字节两位十进制、低字节先发"是本仓已有的口径(`dlt645.py` 地址域就是这么编的),
    不是从上游抄来的。所以这里证明的是:字段声明的 6 字节与线上那 6 个字节对得上。
    """
    row = _writes()["fields"]["04000401"]
    assert (row["type"], row["length"]) == ("String", 6)

    frame = dlt645.build_read_request("000000000001", "04000401")
    address = frame[1 : 1 + dlt645.ADDRESS_BYTES]
    assert len(address) == row["length"]
    assert address == bytes.fromhex("010000000000")


def test_同长度不同值_字节仍对得上():
    """长度不是从值推的:同一个 6 字节字段换个地址,只有非零那几位动。"""
    frame = dlt645.build_read_request("123456789012", "04000401")
    assert frame[1:7] == bytes.fromhex("129078563412")


def test_超范围的日期报错而不截断():
    """写路径对超范围的值报错,而不是截断后写一个"看着正常"的数。"""
    with pytest.raises(ValueError):
        values.encode_value(2510200100, "bcd", 4, byteorder="little")


def test_可写点的地址与编码全部来自_writes_yaml():
    """`dlt645_meter` 的可写点不是手写的:每个都得在 writes.yaml 里查到同样的 DI / 长度 / 类型。

    可写点比只读点多一条要求 —— 写路径要编得出那一串字节。上游图例对 `String` / `Integer`
    只给长度、没给字节布局(`layout: null`),这一档当可写点写下去靠猜:长度是对的,
    编出来的字节却不是表的那个意思,回读还可能"一致"(两边都是我们自己的算法)。
    所以编码能不能推出来由图例回答,不由点表作者回答。
    """
    manifest = yaml.safe_load((REPO / "configs" / "devices.yaml").read_text(encoding="utf-8"))
    fields = _writes()["fields"]
    legend = _writes()["legend"]
    points = manifest["models"]["dlt645_meter"]["point_table"]
    writable = {key: point for key, point in points.items() if point.get("writable")}

    assert writable, "试点型号至少要有一个可写点,否则 13通信 的试点条款没地方落"
    for key, point in writable.items():
        row = fields[point["address"]]
        assert point["length"] == row["length"], key
        spec = legend[row["type"]]
        assert spec["layout"] is not None, f"{key}: 上游图例对 {row['type']} 没给字节布局"
        assert spec["digits"] == 2 * point["length"], key
        wire = values.encode_value(1, point["encoding"], point["length"], byteorder=point["byteorder"])
        assert len(wire) == point["length"], key
