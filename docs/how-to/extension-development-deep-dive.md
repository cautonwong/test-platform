# 扩展开发深究:设备、协议与用例

本文回答三个问题:

1. 怎样为 testplatform 增加一台设备或一种设备类型?
2. 怎样接入一种新规约?
3. 怎样写一条真正产生判据结论和证据的用例?

结论先行:**设备型号差异尽量进 YAML,协议知识进纯函数,驱动只做接线,用例只表达行为与断言。**
不要从“新建一个测试函数并直接拼字节”开始,也不要为每个型号新增一个驱动方法。

## 1. 先建立执行模型

一次装配和执行的链路如下:

```text
configs/devices.yaml
  -> config/loader.py + config/manifest.py   解析并整份校验
  -> CompositionRoot.build()                 唯一组合根
     -> transport(access, mode)              字节管道 / 请求接口 / 总线
     -> driver(type, protocol)               把传输、点表、领域对象接起来
        -> protocol/                         帧、校验、编码解码:纯函数
        -> PointTableDriver                  读数键 -> 点表项 -> 原始字节
        -> domain/                           聚合根、判据、读数、不变量
  -> pytest                                 用例只面对 driver
  -> evidence/                              报文、读数、判据、场景与归因
```

它是在**一个单一有界上下文**内使用分层 DDD,不是一组互相独立的 bounded context。
准确的原则组合是:

- **聚合根**:`domain/device.py` 的 `Device` 及各具体设备保护连接生命周期、历史和领域不变量。
- **值对象**:`Criterion`、`CriteriaProfile`、`PowerReading`、`MeterReading` 等。
- **组合根**:`composition/root.py` 的 `CompositionRoot.build()` 是唯一装配具体实现的位置。
- **消费者定义接口**:`transport/interfaces.py` 按驱动需要定义 `ByteStreamTransport` 等最小接口。
- **单一有界上下文**:概念词义集中在根目录 `CONTEXT.md`,架构决策集中在 `docs/adr/`。

项目文档里没有 “user alloc” 这个架构原则。最接近的事实是“组合根选择并分配具体传输,
驱动工厂创建领域对象”,不要把它包装成另一个 DDD 术语。

三条必须记住的边界:

- 设备类型 = `type × protocol`。同一个 `meter` 接 Modbus 与 DL/T645 是两个驱动注册键。
- 型号差异 = 地址、长度、编码、倍率、字节序、能力、判据;应落在清单,不是 Python 分支。
- `read_all()` 才是一次完整测量;`read(key)` 只是取值,不写历史、不判判据、不产生读数证据。

依据:

- `CONTEXT.md`
- `README.md` 的“目录结构与依赖方向”“三条原则分别落在哪”
- `docs/adr/0001-device-type-is-category-times-protocol.md`
- `docs/adr/0004-protocol-knowledge-is-its-own-layer.md`
- `src/testplatform/composition/root.py`
- `src/testplatform/transport/interfaces.py`

## 2. 编写设备

“编写设备”有四个层次,先选最低成本的那一层。

| 需求 | 要改什么 | 是否写 Python |
|---|---|---|
| 已有型号加一台实例 | `devices:` 加一条 | 否 |
| 同规约新型号 | `models:` 加点表,再加实例 | 否 |
| 新设备类型 | domain + driver + 注册 + 清单 | 是 |
| 新接入方式 | transport + 注册 + 协议放行 | 是 |

### 2.1 已有型号加实例

设备实例的键是闭合集合:

```yaml
devices:
  - id: meter_02
    model: dtsd1352_modbus
    access: serial
    mode: simulator
    name: "2 号电表"
    bus_address: 2
    connection:
      port: /dev/ttyUSB1
      baudrate: 9600
      simulated_registers: {0: 1, 1: 0xE240, 2: 2300}
```

要点:

- `id` 必须全清单唯一。
- `mode` 不写时缺省是 `real`;首次接入应先写 `simulator`。
- `access` 是接入方式,不是规约;必须在该 `protocol` 允许的集合里。
- Modbus 与 DL/T645 需要 `bus_address`;Modbus 为 1–247,DL/T645 为 12 位数字表地址。
- 设备级键只能是核心键加上型号 `device_params` 声明的参数;多写、少写、拼错都会在装配前报错。
- 新增实例后,所有使用泛型 fixture 的契约用例会自动多一个 `[设备id]` 节点。

### 2.2 同规约新型号

这是最常见的设备扩展。只增加型号与实例:

```yaml
criteria_profiles:
  meter_220v:
    - {quantity: 电压, min: 198, max: 242}
    - {quantity: 有功电量, min: 0}

models:
  acme_meter_v1:
    type: meter
    protocol: modbus_rtu
    criteria: meter_220v
    capabilities: [有功电量, 电压]
    device_params: [rated_phase_voltage_v]
    point_table:
      有功电量: {address: 0, length: 4, encoding: uint, scale: 0.01, unit: kWh}
      电压: {address: 2, length: 2, encoding: uint, scale: 0.1, unit: V}

devices:
  - id: acme_meter_01
    model: acme_meter_v1
    access: serial
    mode: simulator
    bus_address: 7
    rated_phase_voltage_v: 220
    connection:
      port: /dev/ttyUSB1
      baudrate: 9600
      simulated_registers: {0: 1, 1: 0xE240, 2: 2300}
```

点表字段:

| 字段 | 含义 |
|---|---|
| `address` | Modbus 寄存器地址、DL/T645 DI,或文本行字段序号 |
| `length` | 有效字节数;`ascii` 时是字符数 |
| `encoding` | `uint` / `int` / `bcd` / `float32` / `ascii` |
| `scale` | 解码后乘的倍率 |
| `byteorder` | 每个字内部字节序 |
| `word_order` | 多个寄存器之间的字序,只用于至少 4 字节的值 |
| `function` | Modbus 0x03 / 0x04,写语义名 |
| `writable` | 是否允许写,缺省为否 |
| `unit` | 报告展示单位,不参与换算 |

`capabilities` 必须能从点表读数键达到。能力声明的用途是让用例可以按能力跳过,
声明了却读不到会在装配期报错,而不是让跳过机制说谎。

如果同一个协议的多个型号重复声明同一 DI 的固有事实,把它放到 `protocol_items`,
点表只写型号差异:

```yaml
protocol_items:
  dlt645:
    "00010000": {name: 正向有功总电能, length: 4, encoding: bcd,
                 byteorder: little, scale: 0.01, unit: kWh}

models:
  dlt_meter_v2:
    type: meter
    protocol: dlt645
    criteria: meter_220v
    capabilities: [有功电量]
    point_table:
      有功电量: {item: "00010000"}
```

`item` 与 `address` 不能并列;与目录逐字相同的覆盖字段也会被拒绝。同一事实只保留一处真相。

### 2.3 新设备类型

只有业务语义和实现都不属于已有类型时才新增类型。例如“充电桩”不应复用电表的聚合根,
只因为两者都能读电压。

#### 第一步:领域聚合根

在 `src/testplatform/domain/<type>.py` 中继承 `Device`,通过公开方法保护不变量:

```python
from dataclasses import dataclass
from typing import Tuple

from testplatform.domain.criteria import Breach, CriteriaProfile
from testplatform.domain.device import Device
from testplatform.domain.exceptions import InvariantViolation


@dataclass(frozen=True)
class ChargingReading:
    power_w: float
    temperature_c: float
    breaches: Tuple[Breach, ...] = ()


class Charger(Device):
    def __init__(self, device_id: str, name: str, criteria: CriteriaProfile) -> None:
        super().__init__(device_id, name)
        self.criteria = criteria

    def record_reading(self, power_w: float, temperature_c: float) -> ChargingReading:
        self.ensure_connected()
        if temperature_c < -100 or temperature_c > 200:
            raise InvariantViolation(f"温度不是可信物理量: {temperature_c}")
        evaluation = self.criteria.evaluate({
            "power_w": power_w,
            "temperature_c": temperature_c,
        })
        reading = ChargingReading(power_w, temperature_c, evaluation.breaches)
        self._record(reading)
        return reading
```

聚合根只放“绝不应该发生”的不变量。阈值越界是设备不合格,应通过判据集记录;
不要把它改成异常。未连接读写、SOC 越出 0–100 这类平台自身状态错误才应抛不变量异常。

#### 第二步:驱动工厂

优先复用 `PointTableDriver`:

```python
from testplatform.config.manifest import Model
from testplatform.domain.criteria import CriteriaProfile
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import register_device
from testplatform.drivers.vendor_ascii import PointReadings, vendor_ascii_reader
from testplatform.transport.interfaces import ByteStreamTransport

QUANTITIES = {"功率": "power_w", "温度": "temperature_c"}


@register_device("charger", "acme_ascii")
def build_charger(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    charger = Charger(device_id, spec.get("name", device_id), criteria)
    return PointTableDriver(
        PointReadings(charger, QUANTITIES),
        model,
        transport,
        vendor_ascii_reader(transport, b"READ\n"),
    )
```

`PointReadings` 只在聚合根 `record_reading()` 接收具名参数而不是映射时使用。
它把读数键映射成参数名,任何多键或少键都会立即报错。

如果是平台内置设备类型,还要让注册副作用执行。当前源码的正确入口是
`src/testplatform/composition/registrations.py`:

```python
from testplatform.drivers import charger_driver  # noqa: F401
```

`composition/root.py` 已经导入 `registrations`,因此不需要再改组合根。
旧版 `docs/how-to/add-a-protocol-and-driver.md` 仍写“在 root.py 加 import”,
这与当前源码不一致;以 `registrations.py` 为准。

如果是用户项目里的自定义驱动,应在项目 `conftest.py` 导入它:

```python
import charger_driver  # noqa: F401
```

装饰器只在模块被导入时执行。漏掉这行常见结果是
`未找到设备工厂: 设备类型='charger', 规约='acme_ascii'`。

#### 第三步:清单与能力

```yaml
criteria_profiles:
  charger_default:
    - {quantity: power_w, min: 0, max: 220000}
    - {quantity: temperature_c, min: -20, max: 60}

models:
  acme_charger:
    type: charger
    protocol: acme_ascii
    criteria: charger_default
    capabilities: [功率, 温度]
    point_table:
      功率: {address: 0, length: 8, encoding: ascii, unit: W}
      温度: {address: 1, length: 7, encoding: ascii, unit: C}

devices:
  - id: charger_01
    model: acme_charger
    access: tcp
    mode: simulator
    connection:
      host: 192.0.2.10
      port: 9000
      simulated_response: "7000 32"
```

注册成功后,`tests/conftest.py` 会按 `registered_types()` 自动生成 `charger` 和
`charger_id` 泛型 fixture。测试层不需要按新类型再加分支。

### 2.4 周边设备

电网模拟器、标准源、可调负载也走同一套型号、驱动和清单机制,区别是:

- `role: support`;不参与契约测试自动展开,也不判合格。
- 通常不写 `criteria`。
- 可写 `initial_setpoint`;每条用例后组合根会恢复它。
- 驱动若声明 `initial_setpoint`,必须实现 `apply_initial_setpoint()`;否则装配期报错。
- 周边设备故障应转换为“测试无效”,不是被测设备不合格。

依据:

- `src/testplatform/config/manifest.py`
- `src/testplatform/config/loader.py`
- `src/testplatform/drivers/registry.py`
- `src/testplatform/drivers/point_table_driver.py`
- `src/testplatform/composition/registrations.py`
- `src/testplatform/drivers/grid_simulator_driver.py`
- `configs/devices.yaml`
- `docs/adr/0003-terminal-and-support-equipment-share-the-device-model.md`

## 3. 编写协议

### 3.1 先分清四层

| 层 | 认识什么 | 不认识什么 |
|---|---|---|
| transport | socket、串口、HTTP、CAN 等收发机制 | 帧怎么解释 |
| protocol | 字节、帧头尾、校验、地址、DI | socket、设备状态、点表 |
| driver | 点表、transport、聚合根 | 协议的字节级细节 |
| domain | 物理量、判据、不变量 | 任何协议细节 |

帧边界属于 protocol,不属于 transport。transport 的 `receive(expected=...)` 可以接收
固定字节数或 `response_length(frame)` 函数;长度未定前该函数返回 `None`。

### 3.2 写纯编解码模块

在 `src/testplatform/protocol/<protocol>.py` 中提供驱动实际需要的最小 API:

```python
HEADER = b"\x5a\xa5"


def response_length(frame: bytes) -> int | None:
    """已收部分 -> 整帧长度;还不足 4 字节头时返回 None。"""
    if len(frame) < 4:
        return None
    return 6 + frame[3]


def build_read_request(bus_address: int, data_id: str) -> bytes:
    data = bytes.fromhex(data_id)
    if len(data) != 2:
        raise ValueError(f"data_id 必须 4 位十六进制,收到 {data_id!r}")
    return HEADER + bytes([bus_address]) + data


def parse_read_response(frame: bytes, bus_address: int, data_id: str) -> bytes:
    if len(frame) < 6 or frame[:2] != HEADER:
        raise ValueError(f"帧头或长度错误: {frame.hex()}")
    if frame[2] != bus_address:
        raise ValueError(f"应答地址 {frame[2]} 不是请求地址 {bus_address}")
    if frame[4:6] != bytes.fromhex(data_id)[::-1]:
        raise ValueError("应答数据标识与请求不一致")
    payload = frame[6:]
    if len(payload) != frame[3]:
        raise ValueError("长度字节与实际数据长度不一致")
    return payload
```

真实实现还要处理协议规定的校验、异常应答与转义。不要让错误帧继续解码成一个
“看起来合理”的读数。错误语义尽量区分:

- 半帧:transport 的 `IncompleteFrameError`
- 完全没回: `TimeoutError`
- 对端关闭: `ConnectionError`
- 对端回了坏帧: `ValueError`
- 从站显式拒绝: 专用异常,并携带错误码

### 3.3 写 driver

若“一个读数键 = 一次请求/应答”,只写 `read_raw`,复用 `PointTableDriver`:

```python
from testplatform.config.manifest import Model, Point
from testplatform.domain.criteria import CriteriaProfile
from testplatform.domain.meter import Meter
from testplatform.drivers.point_table_driver import PointTableDriver
from testplatform.drivers.registry import register_device
from testplatform.protocol import acme_protocol
from testplatform.transport.interfaces import ByteStreamTransport


def _reader(transport: ByteStreamTransport, bus_address: int):
    def read_raw(point: Point) -> bytes:
        transport.send(acme_protocol.build_read_request(bus_address, point.address))
        frame = transport.receive(timeout=2.0, expected=acme_protocol.response_length)
        return acme_protocol.parse_read_response(frame, bus_address, point.address)

    return read_raw


@register_device("meter", "acme_protocol")
def build_meter(
    device_id: str,
    spec: dict,
    model: Model,
    transport: ByteStreamTransport,
    criteria: CriteriaProfile,
) -> PointTableDriver:
    meter = Meter(device_id, spec.get("name", device_id), criteria)
    return PointTableDriver(
        meter,
        model,
        transport,
        _reader(transport, int(spec["bus_address"])),
    )
```

只需改这一层时,不要去改 `point_table_driver.py`。它会统一完成:

```text
读数键 -> Point -> read_raw(Point) -> 原始字节
       -> encoding / scale / byteorder / word_order
       -> 一次聚合根 record_reading
       -> 判据结论、历史与证据
```

如果协议支持连续寄存器块读,再提供可选 `read_raw_many`;没有把握就不要实现,
逐点读的语义仍然正确。若一次读数需要解锁、握手、状态机或写入,才写专用驱动类;
参考 `drivers/grid_simulator_driver.py`,但仍保持协议函数纯化。

### 3.4 接上注册

新协议至少检查四个位置:

1. `protocol/__init__.py` 导出模块。
2. `config/manifest.py` 的 `PROTOCOL_ACCESS` 声明该规约允许的 `access`。
3. 驱动模块使用 `@register_device(type, protocol)`。
4. `composition/registrations.py` 导入驱动模块;用户项目则在 `conftest.py` 导入。

若接入方式本身也是新的,还要实现 `transport/interfaces.py` 中合适的 Protocol,
并用 `@register_transport(access, mode)` 注册真实与模拟实现。

### 3.5 协议必须怎么测

`tests_unit/test_protocol_codec.py` 是最便宜的验证缝:

1. 已知请求对象 -> 逐字节断言请求帧。
2. 捕获的真实应答 -> 解出预期值。
3. 编码/倍率/字节序/字序的往返。
4. 坏校验、错地址、错功能码、错长度、异常应答各一条。
5. 半帧和短读行为,用假 transport 验证。

然后再做设备级测试:

```bash
python3 -m pytest -q tests_unit/test_protocol_codec.py
python3 -m pytest -q --select model:<新型号>
```

协议接入后,先跑模拟器,再切 `mode: real`。真实串口还要配置帧间隔,并在并行时使用
`--dist loadgroup`,避免同一总线被多个 worker 抢占。

依据:

- `src/testplatform/protocol/modbus_rtu.py`
- `src/testplatform/protocol/dlt645.py`
- `src/testplatform/protocol/values.py`
- `src/testplatform/drivers/meter_driver.py`
- `src/testplatform/drivers/vendor_ascii.py`
- `src/testplatform/transport/interfaces.py`
- `tests_unit/test_protocol_codec.py`
- `docs/how-to/add-a-protocol-and-driver.md`

## 4. 编写用例

### 4.1 先选测试层

| 目录 | 回答的问题 |
|---|---|
| `tests_unit/` | 聚合根、判据、编解码、编排逻辑本身对不对 |
| `tests/` | 这一类设备的每个 DUT 是否都满足通用契约 |
| `tests_platform/` | 平台自身行为,如证据、归因、拓扑、断链 |
| `tests_integration/` | 多个具体设备放在一个台架上是否互相干扰 |
| `tests_scenario/` | 多台设备联动后,场景整体过没过 |

纯写参数再读回比较的条款应写成清单中的 `setting_cases`,不要复制成大量测试函数。

### 4.2 一致性契约用例

新增设备类型或型号后,通常复用已有契约用例,不需要为每台实例复制函数。
需要新增规则时,写一个 pytest 函数:

```python
import pytest

from testplatform.cases import case


@case(
    id="M_00_00_02_000001_01",
    clause="DLT645-4.5.2",
    tags=["一致性", "读数"],
)
@pytest.mark.contract
@pytest.mark.requires_capability("电压")
def test_meter_voltage_is_within_the_profile(meter):
    meter.connect()

    measurement = meter.read_all()
    evaluation = meter.device.latest_reading

    assert measurement.readings["电压"] > 0
    assert evaluation.passed, [b.describe() for b in evaluation.breaches]
```

其中:

- `meter` 是注册表自动生成的泛型 fixture,会对清单里的每台 `type: meter`, `role: dut`
  设备各展开一次。
- `@case.id` 是证据与归因的身份,同一个函数参数化出的多个节点共用一个编号。
- `clause` 必须是 `configs/clauses.yaml` 中的受控 ID。
- `requires_capability` 在型号未声明能力时于收集阶段跳过。
- `real_hardware` 只在真实设备上才有意义;`--force-simulator` 时平台自动跳过。

**关键区别:**

- `read("电压")` 只读取该点并返回数值,不写历史、不求判据、不落读数证据。
- `read_all()` 读取型号点表的全部点,调用一次聚合根 `record_reading()`,
  产生历史、判据结论与可判证据。

所以一致性测试和“本次是否合格”的断言应优先用 `read_all()`。`read(key)` 适合探测某个点
或做驱动层精确测试,不能用来证明一次完整测量已经发生。

### 4.3 编号、矩阵和标签

- 一个测试函数一个 `@case.id`;不同函数重复编号会在收集期报错。
- 参数化的多个设备节点共用一个编号,具体设备由 nodeid 的 `[device_id]` 区分。
- 用例编号里不要再拼参数值。
- `test_matrix` 决定型号实际跑哪些测试项,写 `tag:<标签>` 或编号前缀。
- 没有出现在 `test_matrix` 的型号默认不受限;出现但引用不存在条目会在收集期报错。
- 按设备参数化的用例必须带 `@case`,且要被某个型号矩阵覆盖。

### 4.4 设置类条款

“写 `N` 个参数 -> 读回 -> 判相等”、没有因果链的条款只改 YAML:

```yaml
setting_cases:
  acme_meter_v1:
    M_00_00_10_000008_02:
      clause: DLT645-4.5.2
      steps:
        - {write: 有功计量模式, value: "1600", expect: "1600"}
        - {write: 有功计量模式, value: "1601", expect: "1601"}
```

要求:

- `write` 必须是该型号点表中 `writable: true` 的点。
- `value` 要能被点的编码/长度表示。
- `expect` 是单个值;区间应进判据集,跨设备计算应进场景。
- 不支持条件、循环或引用前一步读数。
- 台架前置用 `bench:`;人工步骤用 `manual:`;人工步骤既不算通过,也不算失败。

### 4.5 场景用例

场景不是 YAML 步骤表,而是显式点名参与设备的 pytest 函数:

```python
from testplatform.cases import case, case_of
from testplatform.reporting.scenario import ScenarioStep, record_scenario


@case(id="S_00_00_01_000010_01", clause="GBT37408-5.2.1", tags=["场景"])
def test_grid_frequency_reaches_inverter(composition_root, request):
    grid = composition_root.get("grid_sim_01")
    inverter = composition_root.get("inv_01")

    grid.connect()
    setpoint = grid.apply_grid(230.0, 50.0)

    inverter.connect()
    measurement = inverter.read_all()
    evaluation = inverter.device.latest_reading

    steps = [
        ScenarioStep(
            "grid_sim_01",
            "电网输出 230V / 50Hz",
            abs(setpoint.freq_hz - 50.0) < 0.1,
            {"freq_hz": setpoint.freq_hz},
        ),
        ScenarioStep(
            "inv_01",
            "逆变器读数满足判据集",
            evaluation.passed,
            {
                "readings": dict(measurement.readings),
                "breaches": [b.describe() for b in evaluation.breaches],
            },
        ),
    ]

    path = record_scenario(
        composition_root.evidence,
        name=request.node.name,
        identity=case_of(request.node),
        participants={
            "grid_sim_01": composition_root.role_of("grid_sim_01"),
            "inv_01": composition_root.role_of("inv_01"),
        },
        reading_keys={"inv_01": measurement.readings.keys()},
        steps=steps,
    )
    assert path is not None
```

规则:

- 参与方按 id 显式点名,role 从清单查。
- 每一步 `passed` 必须来自实际比较或判据求值,不能写死 `True`。
- 只有 DUT 步骤失败 -> `failed`;只有 support 步骤失败 -> `invalid`(测试无效)。
- 固定物理时长等待用 `settle(seconds, reason)`,不要 `time.sleep`。
- 动过 DUT 参数要 `scenario.on_cleanup(...)`,并在 `record_scenario(cleanups=scenario)` 执行。
- 分支、循环、轮询、步骤重试、场景调场景都不支持;多个取值用 `parametrize`,不同路径拆场景。

### 4.6 证据与归因

以下内容由 pytest 层自动落盘:

- 原始帧与解码读数;
- 判据、超界量、用例编号与条款;
- 写入和回读验证;
- 事件、断链、台架前置;
- 场景整体结论;
- 本轮收集、执行、跳过、失败范围。

证据不是测试代码手工拼的。直接写 Python 脚本调用驱动不会产生 pytest 会话证据;
要保留证据就跑 pytest 或用任务执行器。

验证顺序:

```bash
pip install -e .[dev]
python3 -m pytest -q tests_unit/test_protocol_codec.py
python3 -m pytest -q --select case:<用例编号前缀>
python3 -m pytest -q --select model:<型号>
python3 -m pytest -q
python3 -m pytest -q -n 4 --dist loadgroup
python3 -m pytest -q --count=3 tests/test_meter.py
python3 tools/attribute_round.py
```

依据:

- `docs/WRITING_TESTS.md`
- `docs/how-to/add-a-scenario.md`
- `tests/conftest.py`
- `tests/test_meter.py`
- `tests/test_setting_cases.py`
- `tests_scenario/test_grid_frequency.py`
- `src/testplatform/cases.py`
- `src/testplatform/plugin.py`
- `src/testplatform/reporting/scenario.py`

## 5. 推荐实施顺序

对一种全新设备,按以下顺序推进:

1. **模拟器/协议字节向量**:先把链路和帧跑通,不接真机。
2. **YAML 型号与实例**:先把点表、能力、判据、设备参数声明完整。
3. **驱动注册**:确认 `CompositionRoot.build()` 能找到 `(type, protocol)`。
4. **领域不变量**:只写真正“不该发生”的规则。
5. **契约测例**:用泛型 fixture 验连接、读数全链路和判据。
6. **设置类条款**:无因果链的写读回只进 `setting_cases`。
7. **场景用例**:跨设备联动才进入 `tests_scenario/`。
8. **真机切换**:改 `mode`,补帧间隔、总线和硬件依赖,再跑归因。

## 6. 最容易犯的错

1. 默认 `mode` 是 `real`,新设备忘了写 `simulator`。
2. 把型号地址差异写进 Python 分支,而不是点表。
3. 把 `byteorder` 和 `word_order` 当同一件事。
4. 用缓冲区大小当帧边界,而不是使用协议 `response_length`。
5. 协议校验失败后仍返回默认值,制造“看起来正常”的假读数。
6. 新驱动没被 import,导致注册表里没有工厂。
7. 用 `read(key)` 写一致性判据,却没有完整读数、历史和证据。
8. 按设备参数化的用例忘写 `@case` 或没被 `test_matrix` 覆盖。
9. 把设置回环或单设备规则写进场景层。
10. 场景中的 `passed` 写死,或参与方 role 写错。
11. 并行执行漏掉 `--dist loadgroup`,让两个 worker 抢同一条总线。
12. 直接脚本跑驱动,却期待 `evidence/` 自动出现。
