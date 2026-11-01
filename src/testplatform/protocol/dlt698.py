"""
DL/T 698.45（面向对象的用电信息数据交换协议）的链路层帧与应用层 APDU。

## 为什么平台侧要自己写一份

族 B（DUT 当从站）要靠平台**当主站**去问：GET 读属性、ACTION 操作方法。
这些字节和南向的 Modbus / DLT645 一样是外部契约，散在用例里就是每写一条用例
抄一遍十六进制，抄错一个字节的现象是"设备不回应" —— 最难查的一类。

集中在这里，改一处；而且全是纯函数，不连 socket 就能单测。

## 依据

Q/GDW《面向对象的用电信息数据交换协议》（报批稿）原文在
`/workspaces/edgeos/EdgeC/docs/698_parsed.md`，附录 H 带完整报文举例。
本模块的常量与测试向量全部对着原文抄，不靠推断。

## 帧格式（标准 5.1.1）

```
68 | L(2, 低 14 位) | C | SA | CA | HCS(2) | APDU | FCS(2) | 16
```

- `L` = **帧长 − 2**（"除起始字符和结束字符之外的帧字节数"）。
  附录 H.1 的登录帧是 32 字节、L=0x1E=30，可以一眼验出来。
- `HCS` 覆盖 `[L, CA]`，`FCS` 覆盖 `[L, APDU]`，都是 FCS16（初值 0xFFFF，结果取反）。

## APDU 的尾部

每个 APDU 都以**时间标签**结尾（本实现不用，填 `00` = 没有时间标签）。
响应还多一个 `FollowReport`（无 = `00`）。少写这两字节不会让帧"明显坏掉"，
而是让对端把最后一个数据字节当成时间标签，报"类型不匹配"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

__all__ = [
    "APDU_END",
    "DLT698_ERRORS",
    "Frame",
    "FrameError",
    "action_request",
    "action_response",
    "build_frame",
    "get_request",
    "get_response",
    "parse_action_response",
    "parse_frame",
    "response_length",
    "parse_get_response",
]

HEAD = 0x68
END = 0x16

# 服务标识（标准 6.1 / 附录 H 的服务码）
SVC_GET_REQ = 0x05
SVC_SET_REQ = 0x06
SVC_ACTION_REQ = 0x07
SVC_GET_RES = 0x85
SVC_SET_RES = 0x86
SVC_ACTION_RES = 0x87
SVC_REPORT = 0x88

# APDU 尾部两个固定字节
APDU_END = {"time_tag": 0x00, "follow_report": 0x00}

# DAR（数据访问结果，标准 6.3.3.11）。只列现场会遇到的；其余按编号显示。
DLT698_ERRORS = {
    0: "成功",
    1: "硬件失效",
    2: "暂时失效",
    3: "拒绝读写",
    4: "对象未定义",
    5: "对象接口类不符合",
    6: "对象不存在",
    7: "类型不匹配",
    8: "越界",
    15: "密码错/未授权",
    20: "安全认证不匹配",
    22: "ESAM 验证失败",
    23: "安全认证失败",
    27: "地址异常",
    33: "请求超时",
    255: "其它",
}


class FrameError(ValueError):
    """帧或 APDU 不合法。消息里必须写清是哪一处，否则现场只能靠猜。"""


# ---------------------------------------------------------------------------
# FCS16
# ---------------------------------------------------------------------------

_FCS_TAB = (
    0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
    0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
    0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
    0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876,
    0x2102, 0x308B, 0x0210, 0x1399, 0x6726, 0x76AF, 0x4434, 0x55BD,
    0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
    0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C,
    0xBDCB, 0xAC42, 0x9ED9, 0x8F50, 0xFBEF, 0xEA66, 0xD8FD, 0xC974,
    0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
    0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3,
    0x5285, 0x430C, 0x7197, 0x601E, 0x14A1, 0x0528, 0x37B3, 0x263A,
    0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
    0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9,
    0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5, 0xA96A, 0xB8E3, 0x8A78, 0x9BF1,
    0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
    0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70,
    0x8408, 0x9581, 0xA71A, 0xB693, 0xC22C, 0xD3A5, 0xE13E, 0xF0B7,
    0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
    0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036,
    0x18C1, 0x0948, 0x3BD3, 0x2A5A, 0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E,
    0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
    0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD,
    0xB58B, 0xA402, 0x9699, 0x8710, 0xF3AF, 0xE226, 0xD0BD, 0xC134,
    0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
    0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3,
    0x4A44, 0x5BCD, 0x6956, 0x78DF, 0x0C60, 0x1DE9, 0x2F72, 0x3EFB,
    0xD68D, 0xC704, 0xF59F, 0xE416, 0x90A9, 0x8120, 0xB3BB, 0xA232,
    0x5AC5, 0x4B4C, 0x79D7, 0x685E, 0x1CE1, 0x0D68, 0x3FF3, 0x2E7A,
    0xE70E, 0xF687, 0xC41C, 0xD595, 0xA12A, 0xB0A3, 0x8238, 0x93B1,
    0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
    0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330,
    0x7BC7, 0x6A4E, 0x58D5, 0x495C, 0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
)


def fcs16(data: bytes) -> int:
    fcs = 0xFFFF
    for byte in data:
        fcs = (fcs >> 8) ^ _FCS_TAB[(fcs ^ byte) & 0xFF]
    return fcs ^ 0xFFFF


# ---------------------------------------------------------------------------
# 链路层
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    sa: bytes
    ctrl: int
    apdu: bytes
    raw: bytes


def _addr_bytes(sa: bytes) -> bytes:
    """SA 域 = 长度/类型描述符 + 地址字节。

    描述符低 4 位是"地址长度 − 1"（标准 5.1.4），所以 1 字节地址写成 0x00。
    """
    if not 1 <= len(sa) <= 16:
        raise FrameError(f"地址长度必须在 1…16 之间，收到 {len(sa)}")
    return bytes([(len(sa) - 1) & 0x0F]) + bytes(sa)


def build_frame(apdu: bytes, sa: bytes = b"\x01", ca: int = 0x10, ctrl: int = 0x43) -> bytes:
    """把 APDU 包成链路帧。

    缺省 `ctrl=0x43`（客户机发起的请求帧）、`ca=0x10`：与标准附录 H 的举例一致。
    """
    if not apdu:
        raise FrameError("APDU 不能为空")

    body = bytes([ctrl]) + _addr_bytes(sa) + bytes([ca])

    # L = 除起始符与结束符之外的帧字节数（标准 5.1.2）：
    #   自己(2) + C/SA/CA(body) + HCS(2) + APDU + FCS(2)
    l_field = 2 + len(body) + 2 + len(apdu) + 2
    if l_field > 0x3FFF:
        raise FrameError(f"帧太长：L={l_field} 超过 14 位上限")

    # HCS 覆盖"帧头除起始字符和 HCS 本身之外的所有字节"（标准 5.1.5）——
    # **含长度域**。漏掉 L 算出来的校验值自洽，但对端一律判坏帧；
    # 而本仓库的从站不校验 HCS，于是这个错能一路跑到"主站说帧坏"才暴露。
    head = l_field.to_bytes(2, "little") + body
    hcs = fcs16(head)

    frame = bytes([HEAD]) + head + hcs.to_bytes(2, "little") + apdu
    return frame + fcs16(frame[1:]).to_bytes(2, "little") + bytes([END])


def parse_frame(data: bytes) -> Frame:
    """校验并拆开一帧。任何一处不对都抛 FrameError，不返回"半个帧"。"""
    if len(data) < 12:
        raise FrameError(f"帧太短：{len(data)} 字节")
    if data[0] != HEAD:
        raise FrameError(f"起始符不是 0x68：0x{data[0]:02X}")
    if data[-1] != END:
        raise FrameError(f"结束符不是 0x16：0x{data[-1]:02X}")

    l_field = int.from_bytes(data[1:3], "little") & 0x3FFF
    if l_field != len(data) - 2:
        raise FrameError(f"长度域 {l_field} 与帧长 {len(data)} 不符（应为 {len(data) - 2}）")

    want_fcs = fcs16(data[1:-3])
    got_fcs = int.from_bytes(data[-3:-1], "little")
    if want_fcs != got_fcs:
        raise FrameError(f"FCS 不符：算得 0x{want_fcs:04X}，帧里是 0x{got_fcs:04X}")

    # 布局：data[0]=68, [1:3]=L, [3]=C, [4]=SA 长度描述符, [5:]=SA, 然后 CA
    sa_len = (data[4] & 0x0F) + 1
    sa_start = 5
    sa = data[sa_start:sa_start + sa_len]
    ca_idx = sa_start + sa_len
    # HCS 覆盖 L + C + SA + CA（标准 5.1.5，含长度域）
    want_hcs = fcs16(data[1:ca_idx + 1])
    got_hcs = int.from_bytes(data[ca_idx + 1:ca_idx + 3], "little")
    if want_hcs != got_hcs:
        raise FrameError(f"HCS 不符：算得 0x{want_hcs:04X}，帧里是 0x{got_hcs:04X}")

    return Frame(sa=sa, ctrl=data[3], apdu=data[ca_idx + 3:-3], raw=data)


# ---------------------------------------------------------------------------
# APDU 构造
# ---------------------------------------------------------------------------


def get_request(piid: int, oad: int) -> bytes:
    """GET-Request-Normal：`05 | 01 | PIID | OAD(4) | 时间标签`（附录 H.3）。"""
    return bytes([SVC_GET_REQ, 0x01, piid & 0xFF]) + oad.to_bytes(4, "big") + bytes(
        [APDU_END["time_tag"]]
    )


def action_request(
    piid: int, oi: int, method: int, *, mode: int = 0, param: bytes | None = None
) -> bytes:
    """ACTION-Request-Normal（操作一个对象方法）。

    `07 | 01 | PIID | OMD(4) | Data | 时间标签`，OMD = OI(2) | 方法 | 模式。
    附录 H.5.1「执行电能量复位方法」= `07 01 05 00 10 01 00 0F 00 00`。

    `param` 必须是**已编码的 A-XDR**（含类型标签），例如 integer(0) 是 `0F 00`。
    这里不做类型推导：类型是点位模型的知识，由调用方给。
    """
    if not 1 <= method <= 255:
        raise FrameError(f"方法标识必须在 1…255，收到 {method}")
    omd = oi.to_bytes(2, "big") + bytes([method, mode & 0xFF])
    return (
        bytes([SVC_ACTION_REQ, 0x01, piid & 0xFF])
        + omd
        + (param or b"")
        + bytes([APDU_END["time_tag"]])
    )


def action_response(
    piid: int, oi: int, method: int, dar: int, *, mode: int = 0, data: bytes | None = None
) -> bytes:
    """ACTION-Response-Normal（设备侧实现用；平台侧主要用来构造"预期"做比对）。

    `87 | 01 | PIID-ACD | OMD(4) | DAR | Data OPTIONAL | FollowReport | 时间标签`
    """
    omd = oi.to_bytes(2, "big") + bytes([method, mode & 0xFF])
    body = bytes([SVC_ACTION_RES, 0x01, piid & 0xFF]) + omd + bytes([dar & 0xFF])
    body += (b"\x01" + data) if data else b"\x00"
    return body + bytes([APDU_END["follow_report"], APDU_END["time_tag"]])


def get_response(piid: int, oad: int, data: bytes) -> bytes:
    """GET-Response-Normal：`85 | 01 | PIID-ACD | OAD(4) | 01 | Data | 00 | 00`。"""
    return (
        bytes([SVC_GET_RES, 0x01, piid & 0xFF])
        + oad.to_bytes(4, "big")
        + b"\x01"
        + data
        + bytes([APDU_END["follow_report"], APDU_END["time_tag"]])
    )


# ---------------------------------------------------------------------------
# APDU 解析
# ---------------------------------------------------------------------------


def _iter_axdr(data: bytes) -> Iterator[tuple[int, bytes]]:
    """按 A-XDR 类型标签切分（只处理标量与定长串，够读响应里的值）。"""
    i = 0
    # 定长类型：标签后面直接跟数据，**没有长度字节**（SIZE 是编译期确定的）。
    # float32/float64 属于这一类（标准 6.3.2：octet-string(SIZE(4/8))），
    # 早先把它们当变长处理，会多吃一个字节 —— 解出来的浮点值整体错位。
    fixed = {
        5: 4, 6: 4, 15: 1, 16: 2, 17: 1, 18: 2, 20: 8, 21: 8, 22: 1,
        23: 4, 24: 8, 25: 10, 26: 5, 27: 3, 28: 7,
    }
    while i < len(data):
        tag = data[i]
        i += 1
        if tag in (0x00, 0x03):  # NULL / bool
            yield tag, data[i:i + (1 if tag == 0x03 else 0)]
            i += 1 if tag == 0x03 else 0
        elif tag in fixed:
            n = fixed[tag]
            yield tag, data[i:i + n]
            i += n
        elif tag in (0x09, 0x0A, 0x0C):  # 变长串：带一个长度字节
            if i >= len(data):
                raise FrameError(f"类型 0x{tag:02X} 缺长度字节")
            n = data[i]
            i += 1
            yield tag, data[i:i + n]
            i += n
        elif tag in (0x01, 0x02):  # array / structure
            if i >= len(data):
                raise FrameError(f"类型 0x{tag:02X} 缺元素个数")
            count = data[i]
            i += 1
            for _ in range(count):
                for item in _iter_axdr(data[i:]):
                    yield item
                    i += 1 + len(item[1])
                    break
        else:
            raise FrameError(f"不认识的 A-XDR 类型 0x{tag:02X}")


@dataclass(frozen=True)
class ActionResult:
    piid: int
    oi: int
    method: int
    mode: int
    dar: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.dar == 0

    @property
    def error(self) -> str:
        return DLT698_ERRORS.get(self.dar, f"未定义结果 {self.dar}")


def response_length(frame: bytes) -> int | None:
    """按长度域算出整帧应有多少字节，还没收满 3 字节时返回 None。

    L 是"除起始字符和结束字符之外的帧字节数"（标准 5.1.2），所以整帧 = L + 2。
    收帧的一方靠它知道该收多少，而不是"先收一段再猜" —— 半帧解出来的假读数
    比报错难查得多。
    """
    if len(frame) < 3:
        return None
    l_field = int.from_bytes(frame[1:3], "little") & 0x3FFF
    total = l_field + 2
    return total if total >= 12 else None


def parse_action_response(apdu: bytes) -> ActionResult:
    """解析 ACTION-Response-Normal。

    **DAR 必须如实看**：设备"执行失败"却回 0 是最危险的一类缺陷（主站以为继电器动了），
    所以这里把 DAR 单独暴露成 `.ok` / `.error`，让用例必须显式断言它。
    """
    if len(apdu) < 9:
        raise FrameError(f"ACTION-Response 太短：{len(apdu)} 字节")
    if apdu[0] != SVC_ACTION_RES:
        raise FrameError(f"不是 ACTION-Response：服务码 0x{apdu[0]:02X}")
    if apdu[1] != 0x01:
        raise FrameError(f"只支持 ActionResponseNormal([1])，收到 [{apdu[1]}]")

    piid = apdu[2]
    oi = int.from_bytes(apdu[3:5], "big")
    method, mode = apdu[5], apdu[6]
    dar = apdu[7]
    rest = apdu[8:]

    data = b""
    if rest and rest[0] == 0x01:
        # 去掉末尾 FollowReport + 时间标签
        data = rest[1:-2] if len(rest) >= 3 else b""
    return ActionResult(piid=piid, oi=oi, method=method, mode=mode, dar=dar, data=data)


@dataclass(frozen=True)
class GetResult:
    piid: int
    oad: int
    data: bytes

    def scalars(self) -> list[tuple[int, bytes]]:
        return list(_iter_axdr(self.data))


def parse_get_response(apdu: bytes) -> GetResult:
    """解析 GET-Response-Normal。DAR 非 0 时标准里是 `[DAR]` 而不是数据，这里直接报错。"""
    if len(apdu) < 9:
        raise FrameError(f"GET-Response 太短：{len(apdu)} 字节")
    if apdu[0] != SVC_GET_RES:
        raise FrameError(f"不是 GET-Response：服务码 0x{apdu[0]:02X}")
    if apdu[1] != 0x01:
        raise FrameError(f"只支持 GetResponseNormal([1])，收到 [{apdu[1]}]")

    piid = apdu[2]
    oad = int.from_bytes(apdu[3:7], "big")
    flag = apdu[7]
    if flag != 0x01:
        # 0x00 表示这次读失败（DAR 跟在后面）。把它当数据会读出垃圾值。
        dar = apdu[8] if len(apdu) > 8 else None
        raise FrameError(
            f"GET 失败：DAR={dar}"
            + (f"（{DLT698_ERRORS.get(dar, '未定义')}）" if dar is not None else "")
        )
    return GetResult(piid=piid, oad=oad, data=apdu[8:-2])
