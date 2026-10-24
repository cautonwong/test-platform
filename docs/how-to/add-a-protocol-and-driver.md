# 写一个协议 + 一个 driver

先把两件事分清楚,混在一起写出来的东西三个月后没人敢改。

| | protocol | driver |
|---|---|---|
| 是什么 | 纯函数:字节 ↔ 帧 ↔ 物理量 | 接线:transport + 点表 + 聚合根 |
| 认识什么 | 只认识字节、寄存器地址、数据标识 | 认识点表、聚合根、transport 接口 |
| **不认识**什么 | transport、domain、点表 | 帧怎么拼、CRC 怎么算 |
| 测试方式 | 给字节解出量、给量拼出帧(不需要任何假对象) | 注入假 transport / 假串口 |

先看一个判断:如果"**一个读数键 = 一次请求/应答**",就走通用读取路径 `PointTableDriver`,driver 只需要写一个 `read_raw`(约 15 行)。只有需要时序、分支、或者要下发设定值时才写专用 driver。

---

## 第一步:protocol 模块

新建 `src/testplatform/protocol/<规约>.py`。照 `modbus_rtu.py` / `dlt645.py` 的形状,提供四样东西:

```python
def build_read_request(addr, ...) -> bytes: ...          # 拼请求帧
def parse_read_response(frame, ...) -> bytes: ...         # 校验 + 解析,返回数据域原始字节
def response_length(frame: bytes) -> int | None: ...      # 可选:长度要先看应答头才知道时必须给
```

请求帧需要的参数可能来自点表项:Modbus 的 `function`(0x03/0x04)就是由点表项声明、驱动传给 `build_read_request` 与 `parse_read_response` 的 —— **两个方向共用一个来源**,不会一边 03 一边 04。

`response_length` 是**收帧**用的:transport 只负责"短读继续读,收满才返回",帧有多长由知道规约的这里说。还没看清头的时候返回 `None`。

### 硬规矩

1. **一律抛错,不猜**。CRC 错、从站地址不符、功能码不符、字节数对不上、从站返回异常码 —— 全部 `raise`。带着错帧往下解码只会得到"看着正常"的假读数。
2. **错误类型要分得开**:
   - 帧内容错 → `ValueError`(设备回了,但回的是坏东西)
   - 半帧 → `IncompleteFrameError`(transport 抛,不用你写)
   - 超时 → `TimeoutError`
   - 断链 → `ConnectionError`
   这四种在证据和归因里是不同结论,混成一种就判不出责任方。
3. **不 import `transport` / `domain`**。协议层不知道有传输这回事。
4. 常量放模块顶部(`READ_HOLDING_REGISTERS = 0x03`),不要散在函数里。

### 必须有的单测(`tests_unit/test_protocol_codec.py`)

| 用例 | 断言什么 |
|---|---|
| 给定字节 → 解出物理量 | 上游真实报文(见 `docs/upstreams.md`)解出来的数是对的 |
| 给定物理量 → 拼出帧 | 逐字节比对,含校验位 |
| 校验变体 | 上游存在多种校验(X25 / IBM / CRC 高低字节序)时各一条 |
| 每种错误各一条 | CRC 错 / 地址不符 / 长度不符 / 异常应答,都要 `pytest.raises` |

这几条是纯函数缝,毫秒级跑完,不需要任何设备 —— 这是新规约最省力的验证方式。

---

## 第二步:driver —— 走通用路径(大多数情况)

新建 `src/testplatform/drivers/<名字>_driver.py`,只需要写一个 `read_raw(point) -> bytes`:

```python
def _my_reader(transport: ByteStreamTransport, bus_address) -> ReadRaw:
    def read_raw(point: Point) -> bytes:
        transport.send(myproto.build_read_request(bus_address, point.address))
        # 收满一帧再交上来:帧长由 protocol 说,transport 不认识规约
        frame = transport.receive(timeout=2.0, expected=myproto.response_length)
        return myproto.parse_read_response(frame, bus_address)
    return read_raw


@register_device("meter", "my_protocol")           # (设备类型, 规约)
def _build(device_id, spec, model, transport, criteria) -> PointTableDriver:
    domain = Meter(device_id=device_id, name=spec.get("name", device_id), criteria=criteria)
    return PointTableDriver(domain, model, transport, _my_reader(transport, spec["bus_address"]))
```

`read_raw` 只做三件事:发一次请求、收一帧、校验解析。剩下的解码、倍率、字节序、字序、判据全部在通用路径里 —— **那个文件不用改**。

如果协议有"数据标识 → 长度/编码/倍率"的固有事实(比如 DL/T645 的 DI),把它做成清单的 `protocol_items` 目录(见 [`add-a-device.md`](add-a-device.md) 档 2b),而不是在驱动里建表:驱动只负责“怎么收一帧字节”。

如果聚合根的 `record_reading` 收的是具名参数而不是"读数键 → 物理量"映射,在两者之间加一个 `PointReadings(domain, {"读数键": "参数名"})`(见 `drivers/vendor_ascii.py`),它会在键对不上时直接报错。

## 第三步:driver —— 写专用驱动(少数情况)

出现下面任一条,才值得写专用 driver:

- 一次读数要多次交互(解锁、握手、先写使能位)
- 要**下发**东西(设定值、指令),不是只读
- 有无状态机的时序(等状态位翻转)
- 需要按键组合读(一个请求拿回多个量且拆不开)

参考 `drivers/grid_simulator_driver.py`:周边设备要下发设定值,所以它自己实现 `apply_grid()`,而不是走点表路径。写法与通用驱动一致(`connect` / `disconnect` / `device`),只是多了业务方法。

**判据不要写进 driver**。driver 只负责把物理量交给聚合根,合格与否由判据集判。

---

## 第四步:接上三处(漏一处就跑不到)

1. `protocol/__init__.py` 导出新模块。
2. `src/testplatform/config/manifest.py` 的 `PROTOCOL_ACCESS` 加一行:这个规约允许哪些 `access`(串口规约不会直接跑在裸 TCP 上)。
3. `src/testplatform/composition/root.py` 加一行 `from testplatform.drivers import <你的>_driver  # noqa: F401` —— 这一行触发 `@register_device` 注册。

然后清单里加型号 + 设备,见 [`add-a-device.md`](add-a-device.md) 档 3。

---

## 第五步:验证

```bash
python -m pytest -q tests_unit/test_protocol_codec.py    # 纯函数,最先跑
python -m pytest -q --select model:<新型号>              # 只跑这一台
python tools/mock_modbus_meter.py ...                    # 没真机时用独立从站造真实 socket 链路
python -m pytest -q && python tools/attribute_round.py    # 全量 + 归因
```

## 五个真踩过的坑

1. **按缓冲区大小当帧边界**。`read(256)` / `recv(4096)` 拿到的是"这次读到多少",不是一帧。必须给 `expected` 让 transport 收满。
2. **文本编码的 `length` 是字符数,不是字节数**。按字节截断会把 `100000.5` 截成 `100000.` 解出假读数。通用路径已经处理;自己写 driver 时要记住。
3. **帧间静默**。Modbus RTU 要求 ≥3.5 字符时间,背靠背发两帧会被严格的从站粘成一帧。真机配 `inter_frame_delay_ms`。
4. **迟到应答**。上一轮的应答在本轮请求之后才到,会让整帧错位。串口传输已经在换向前清输入缓冲。
5. **别把"设备回了但内容错"和"设备没回"合成一个异常**。前者是设备不合格,后者要再看是不是断链 —— 归因靠这个区分。

## 参考实现

| 文件 | 看它什么 |
|---|---|
| `protocol/modbus_rtu.py` | 定长帧 + 变长应答 + CRC;`response_length` 按字节数算 |
| `protocol/dlt645.py` | 长度字节 `L` 在头里;12 位 BCD 表地址 |
| `drivers/meter_driver.py` | 两个规约共用同一个通用驱动,只换 `read_raw` |
| `drivers/vendor_ascii.py` | 文本行协议 + `PointReadings` 键名对照 |
| `drivers/grid_simulator_driver.py` | 需要下发设定值时的专用驱动 |
| `tests_unit/test_real_transports.py` | 用假串口/假 socket 验真机行为的写法 |
