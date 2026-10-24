"""
点表驱动的通用读取路径:读数键进来,物理量出去。

    读数键 → 点表项 → (规约取字节) → 解码成物理量 → 聚合根一次领域方法调用

这里没有一行按设备类型分叉的代码:读哪些量、地址在哪、怎么编码,全在型号的点表里。
唯一和规约相关的部分是"怎么按点表收发字节",由构造时注入的 read_raw / read_raw_many 提供
(见 drivers/meter_driver.py)—— 所以第二规约接入时,这个文件不用改。

一次读全部点、一次领域方法调用,是刻意的:判据集要一次看全所有物理量才能判合格
(比如电压和电量各有一条判据),拆成"每读一个键就记一次"会让判据永远缺量。
所以 read_all() 是唯一写 history、唯一出判据结论、唯一落读数证据的入口;
read(key) 是另一条路:只读那一个点、只解那一个量,不进 history、不出判据结论
—— 它是"取值",不是"测量"。单读因此不是"缺量的读数",它压根不是一次读数。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Tuple

from testplatform.config.manifest import Model, Point
from testplatform.protocol import decode_value, encode_value

ReadRaw = Callable[[Point], bytes]
# 把一帧写请求发出去并校验回显:只证明"设备接受了这一写",不证明值生效了(那是回读的事)。
WriteRaw = Callable[[Point, bytes], None]
# 一次交出整张点表的原始字节:规约若能把相邻地址合并成一次请求,就注入这个
# (Modbus 的块读),否则只注入 read_raw 逐点读。没注入 == 逐点读,不需要伪造批量语义。
ReadRawMany = Callable[[Mapping[str, Point]], Mapping[str, bytes]]


class PointTableDriver:
    def __init__(
        self,
        device: Any,
        model: Model,
        transport: Any,
        read_raw: ReadRaw,
        read_raw_many: ReadRawMany | None = None,
        write_raw: WriteRaw | None = None,
    ) -> None:
        self._device = device
        self._model = model
        self._transport = transport
        self._read_raw = read_raw
        self._read_raw_many = read_raw_many
        self._write_raw = write_raw

    def connect(self) -> None:
        self._transport.connect()
        self._device.mark_connected()

    def disconnect(self) -> None:
        self._transport.disconnect()
        self._device.mark_disconnected()

    def read_all(self) -> Any:
        """读这个型号点表里的全部点,一次交给聚合根(返回聚合根的读数对象)。

        批量只省请求次数,不改语义:还是全部点、还是那一次 record_reading。
        """
        if self._read_raw_many is None:
            raw = {key: self._read_raw(point) for key, point in self._model.point_table.items()}
        else:
            raw = self._read_raw_many(self._model.point_table)
        readings = {
            key: self._decode(point, raw[key]) for key, point in self._model.point_table.items()
        }
        return self._device.record_reading(readings)

    def read(self, key: str) -> float:
        """按读数键取一个物理量:只读这个键的点,一次收发,不顺带读整张点表。

        **它是取值,不是测量**:不进 history、不出判据结论、不落读数证据 ——
        要一次可判的读数(全部量 + 判据结论 + 证据)用 read_all()。
        判据层因此只在 read_all 上求值,不会在"只有一半量的读数"上判合格;
        未连接不能读数这条不变量仍然由聚合根强制。
        """
        point = self._point(key)
        self._device.ensure_connected()
        return self._decode(point, self._read_raw(point))

    @property
    def device(self) -> Any:
        return self._device

    def writable_keys(self) -> Tuple[str, ...]:
        """这台型号声明了可写的点。只认点表,不是驱动方法名。"""
        return tuple(key for key, point in self._model.point_table.items() if point.writable)

    def write(self, key: str, value: float) -> float:
        """写一个点,返回**回读到的**物理量(不是刚写下去的那个数)。

        四步:编码(按点表 encoding / length / 字节序 / 字序反算成寄存器字节)→ 发帧 →
        校验回显 → 立刻读回一次。功能码由寄存器个数决定(1 个 0x06,更多 0x10),不由用例选。
        回读是刻意的一步:设备"接受写入"和"值真的生效"是两件事。回读不一致不抛错、
        不重试,照样返回读到的值,只在证据里留 verified: false —— 设备不合格由判据层判,
        写这一层只如实反映事实。
        """
        point = self._point(key)
        if not point.writable:
            raise ValueError(
                f"型号 {self._model.name!r} 的点 {key!r} 没有声明 writable,不能写;"
                f"可写的点: {list(self.writable_keys())}"
                f"(在设备的点表里给这一项加 writable: true)"
            )
        if self._write_raw is None:
            raise ValueError(
                f"型号 {self._model.name!r} 的驱动没有写路径(没注入 write_raw),"
                f"不能写点 {key!r}"
            )
        raw = self._encode_write(point, value)
        self._write_raw(point, raw)
        read_back_raw = self._read_raw(point)
        read_back = self._decode(point, read_back_raw)
        # 回读只比这个点自己的字节:奇数 length 的点写下去要补齐到整寄存器,
        # 补齐的那一个字节不归它 —— 设备在那儿放什么都不能算写错。
        self._report_write(key, value, read_back, read_back_raw[: len(raw)] == raw)
        return read_back

    def _point(self, key: str) -> Point:
        if key not in self._model.point_table:
            raise KeyError(
                f"型号 {self._model.name!r} 没有读数键 {key!r},"
                f"可用读数键: {sorted(self._model.point_table)}"
            )
        return self._model.point_table[key]

    def _encode_write(self, point: Point, value: float) -> bytes:
        """物理量按点表的 encoding / length / 字节序 / 字序反算成寄存器字节。

        用的就是读路径的逆运算(protocol/values.py 的 encode_value):两边共用同一个字序
        重排,所以读出来的值原样写回去,寄存器字节完全一致。越界值报错,不截断。
        """
        try:
            return encode_value(
                value,
                point.encoding,
                point.length,
                point.scale,
                point.byteorder,
                point.word_order,
            )
        except ValueError as exc:
            raise ValueError(f"点表项 {point.key!r}: {exc}") from None

    def _report_write(self, key: str, value: float, read_back: float, verified: bool) -> None:
        """写动作留痕。证据层是套在传输外面的一层装饰:套了就顺手写一条,没开记录就什么都不做。"""
        report = getattr(self._transport, "report_write", None)
        if report is None:
            return
        report(
            {"key": key, "value": value, "verified": verified, "read_back": read_back}
        )

    def _decode(self, point: Point, raw: bytes) -> float:
        if point.encoding == "ascii":
            # ascii 的 length 是字符数,不是字节数:按字节截断会把 "100000.5" 截成
            # "100000." 解出一个看着正常的假读数,所以文本 token 不截断,超长直接报错。
            token = raw.strip()
            if len(token) > point.length:
                raise ValueError(
                    f"点表项 {point.key!r}: ascii token {token!r} 长 {len(token)} 字符,"
                    f"超过声明的 length={point.length}"
                )
            return decode_value(
                token, point.encoding, point.scale, point.byteorder, point.word_order
            )
        # 规约按整寄存器应答,点表项的 length 是有效字节数(例如 float32 之外的奇数长度)
        return decode_value(
            raw[: point.length],
            point.encoding,
            point.scale,
            point.byteorder,
            point.word_order,
        )
