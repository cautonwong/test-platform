"""
DL/T 698.45 主站驱动（族 B：DUT 当 698 从站，平台当主站去问）。

## 为什么要这个驱动

族 B 的用例原先直接开 socket 拼帧 —— 能验，但读数不进设备模型，报告里没有设备级
归属（平台规定"没验到设备就不算验过"），而且每写一条用例就要抄一遍帧。
有了驱动，698 设备与 Modbus/DLT645 设备在用例里长得一样：`meter.read_all()`。

## 谁负责什么

- **帧**：`testplatform.protocol.dlt698`（纯编解码，向量抄自标准附录 H）
- **点位映射**：型号的 point_table，`address` 是 OAD 的十六进制串（如 `20000200`）
- **A-XDR 类型**：由点表的 `encoding` 决定（`float32` / `uint`），驱动不猜 ——
  698 的 `width=2` 到底是 long 还是 long-unsigned 只有点位模型知道

## 注册键

`("meter", "dlt698")`。与 `("meter", "modbus_rtu")`、`("meter", "dlt645")` 同层，
换规约只换点表与这个工厂，用例不动。
"""
from __future__ import annotations

from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.protocol import dlt698
from testplatform.transport.interfaces import ByteStreamTransport

from .point_table_driver import PointTableDriver, ReadRaw, ReadRawMany
from .registry import register_device

# 698 的 A-XDR 类型标签 → 点表 encoding。值字节本身按大端返回，缩放交给点表。
_TAG_TO_ENCODING = {
    0x12: "uint",  # long-unsigned
    0x11: "uint",  # unsigned
    0x16: "uint",  # double-long-unsigned
    0x05: "int",  # double-long
    0x17: "float32",  # float32
}


def _value_bytes(point, tag: int, raw: bytes) -> bytes:
    """校验 A-XDR 类型标签与点表声明是否一致，再返回值字节。

    **不一致必须报错**：字节长度可能刚好一样，解出来是个"看着正常"的错值，
    而帧校验（HCS/FCS）完全正常 —— 比报错难查得多。
    """
    want = getattr(point, "encoding", None)
    got = _TAG_TO_ENCODING.get(tag)
    if want and got and want != got:
        raise ValueError(
            f"点表项 {point.key!r}: 点表声明 {want}，从站回的类型标签 {tag:#04x}（{got}）"
        )
    return raw


def dlt698_reader_many(transport: ByteStreamTransport, sa: int) -> ReadRawMany:
    """一次 GET-Request-NormalList（[2]）读完整张点表。

    逐点读是 N 次往返；698 的 [2] 允许一帧交出多个 OAD，语义不变
    （还是全部点、还是那一次 record_reading），只是少了 N-1 次来回。
    **响应里的项数必须和请求一致**：少一项就报错，不能让主站拿到半张点表
    还判"通过"。
    """

    def read_many(points) -> dict:
        keys = list(points)
        oads = [int(str(points[k].address), 16) for k in keys]
        apdu = dlt698.get_request_list(0x01, oads)
        transport.send(dlt698.build_frame(apdu, sa=bytes([sa])))

        frame = transport.receive(timeout=2.0, expected=dlt698.response_length)
        res = dlt698.parse_get_response_list(dlt698.parse_frame(frame).apdu)
        if res.piid != 0x01:
            raise ValueError(f"应答 PIID {res.piid} 与请求 1 不符，串到别的响应了")
        if len(res.results) != len(oads):
            raise ValueError(
                f"请求 {len(oads)} 个 OAD，响应只有 {len(res.results)} 项 —— "
                "半张点表不能当读数交出去"
            )

        out = {}
        for key, oad in zip(keys, oads):
            item = res.by_oad(oad)
            scalars = item.scalars()
            if not scalars:
                raise ValueError(f"点表项 {key!r}：应答的 Data 段是空的")
            tag, raw = scalars[0]
            out[key] = _value_bytes(points[key], tag, raw)
        return out

    return read_many


def dlt698_reader(transport: ByteStreamTransport, sa: int) -> ReadRaw:
    """一个点表项 = 一次 GET-Request；`point.address` 是 OAD。"""

    def read_raw(point) -> bytes:
        oad = int(str(point.address), 16)
        apdu = dlt698.get_request(0x01, oad)
        transport.send(dlt698.build_frame(apdu, sa=bytes([sa])))

        frame = transport.receive(timeout=2.0, expected=dlt698.response_length)
        parsed = dlt698.parse_frame(frame)
        res = dlt698.parse_get_response(parsed.apdu)
        if res.oad != oad:
            raise ValueError(
                f"点表项 {point.key!r}: 请求 OAD {oad:#010x}，应答却是 {res.oad:#010x} —— "
                "从站回了别的对象，读数不能算数"
            )

        scalars = res.scalars()
        if not scalars:
            raise ValueError(f"点表项 {point.key!r}: GET-Response 没有数据段")
        tag, raw = scalars[0]

        return _value_bytes(point, tag, raw)

    return read_raw


@register_device("meter", "dlt698")
def _build_dlt698_meter(
    device_id: str, spec: dict, model: Model, transport: ByteStreamTransport, criteria: CriteriaProfile
) -> PointTableDriver:
    meter = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(
        meter,
        model,
        transport,
        dlt698_reader(transport, int(spec["bus_address"])),
        read_raw_many=dlt698_reader_many(transport, int(spec["bus_address"])),
    )
