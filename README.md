# testplatform —— 多协议/多设备自动化测试平台

被测对象是能源/配电领域的现场设备(电表、逆变器、电池、采集终端、电网模拟器……)。
术语见 `CONTEXT.md`,已经定过、不要再翻案的决策见 `docs/adr/`。

## 两个用法,先看你在哪一边

**① 写用例的人**(扩设备/协议/用例):照 `docs/how-to/` 抄。写的是 **Python**——驱动、编解码、用例、模拟器都是 `.py`。
**② 跑测试的人**(构建测试任务 → 跑 → 出报告):写一份 `tasks/<id>.yaml`,然后 `python tools/run_task.py <任务> --root . --evidence-dir reports/<任务>`,再看 `reports/<任务>/summary.json`,最后 `python tools/gate.py reports` 出 CI 判定。

> 分发后的形态是 `npx @cautonwong/testplatform init|run|gate|scaffold`——**`npx` 只是外壳,写的仍然是 Python**。
> 这一层正在做(票 09/09b),当前请用源码形态:`pip install -e .`。

**平台能保证**:跑起来、留证据(读数/帧/注入/死因分类)、四档结论、能归因到设备或步骤。
**平台不保证**:判据对不对。阈值和点表真值来自设备手册,平台只能保证"按你给的判据一致地判",不能替你判对错。

## 运行

```bash
pip install -e .                                  # 依赖只有 pytest 与 pyyaml
python -m pytest                                  # 按 configs/devices.yaml 里每台设备的 mode 字段跑
```

常用开关:

```bash
python -m pytest --force-simulator                        # 忽略 mode,全部设备强制模拟器(没有硬件时用这个)
python -m pytest --device-config configs/devices.yaml     # 换一份设备清单
python -m pytest --no-evidence                            # 不落证据
python -m pytest -m contract                              # 只跑契约测试
python -m pytest tests/test_meter.py                      # 只跑一个文件
python -m pytest --lf                                     # 只重跑上次失败的用例(pytest 自带)
```

按型号 / 能力 / 标签 / 编号挑用例,统一走 `--select`:

```bash
python -m pytest --select model:inverter_pv_series         # 只跑某型号
python -m pytest --select capability:电压                  # 只跑支持某能力的设备的用例
python -m pytest --select tag:一致性                       # 只跑某个标签的用例
python -m pytest --select case:M_00_00_01_000005           # 只跑某个编号前缀的用例
python -m pytest --select model:dtsd1352_modbus,tag:一致性  # 条件叠加,之间是"且"
```

筛完一条不剩会直接报错,并说清是"清单里没有这个型号"还是"没有用例带这个标签",不会报 0 passed 的绿。
`--lf` 按节点 id 缓存:设备清单改了(增删设备、改 id、改型号)就先跑一次全量,别拿旧缓存当结论。

接真实串口 / SSH / HTTP / MQTT / CAN 时,按 `pyproject.toml` 里的 extras 装对应的那一个
(`serial` / `ssh` / `http` / `mqtt` / `can`);只跑模拟器不需要。

写用例、查稳定性、跑覆盖率时装 dev extra,一次装齐:

```bash
pip install -e .[dev]        # 测试工具链:超时兜底 / 重复跑 / 随机顺序 / 覆盖率 / HTML 报告
```

默认套件仍然是那一条 `python -m pytest`,并且带 60 秒单用例超时:真机路径挂死时直接报出卡在哪条用例,
不再靠 CI 外部超时杀进程。`--count=3` 稳定性、`-p randomly` 顺序无关、`--cov` 纯逻辑覆盖率门槛
是三条独立的命令,各自验一件事,用法见 `docs/WRITING_TESTS.md` 的「跑之前先想清楚你这轮在验什么」。

**不要装 `pytest-rerunfailures`**:自动重跑正好掩盖「总线抢占导致的随机失败」——那是并行分组与稳定性验收
存在的理由。失败要看见,不要被重跑抹掉。

## 并行执行:按总线拓扑分组

```bash
pip install -e .[parallel]                                   # 装 pytest-xdist
python -m pytest -n 4 --dist loadgroup                        # 4 个 worker,按拓扑分组
python -m pytest -n 4 --dist loadgroup --no-evidence          # 同上,不落证据
```

分组键取自设备清单里的 `via:`:同一个终端下的所有设备(终端自己 + 它下面的表)是一组,
整组分给同一个 worker 串行跑 —— 一条总线不会被两条用例同时抓着,不会串话。
不同终端之间、以及没有 `via:` 的设备(按自己的 `id` 自成一组)之间并行。

不写 `-n` 时行为完全不变:一个进程、一条用例接一条。加了 `-n` 但忘了 `--dist loadgroup`
(xdist 默认 `load`)分组就不生效,同一台终端下的两条用例可能同时跑 —— 想并行就带上它。

每个 worker 是独立进程,各自把组合根从头装配一次、各自 teardown:设备连接、驱动、判据、
事件队列都在自己进程里,worker 之间不共享任何运行时状态。证据也按 worker 分开
(目录名带 worker 名,见「证据」一节),同一台设备的用例落在一个 worker 上,
不会有两条用例从两个 worker 写同一份证据文件。

分组不是默认相信的:跑 `python tests/parallel_topology_check.py` 会造一份两台终端、
各挂两块表的清单,用 `-n 4 --dist loadgroup` 跑一遍,打印 (worker, device, via) 实测分布,
并断言同一终端下的设备 worker 全相同、不同终端没被挤到同一个 worker 上。

分组防住的**不是"跑得慢",是抢总线**:同一条物理总线上同时有两个请求者,现场就是串话、
读到的数不对。这条结论用 mock 实测过,不靠"跑绿了"推断 —— `python tests/parallel_bus_check.py`
起两个 mock(一个 mock 进程 = 一条物理总线),每条总线上一台总线主站 + 两块用 `via:` 挂在它
下面的表(三台是同一组),用 `-n 4 --dist loadgroup` 跑一轮:

- 正常组:同一总线上的三台设备落在**同一个 worker**(实测分布打印出来)、两条总线落在不同 worker,
mock 的抢占记录 **0 条**;
- **对照组**:同一份清单、同一批用例,只用 `-p tests.parallel_split_group_plugin` 把其中一块表
额外标成另一个 `xdist_group` —— 分组被拆开、两块表被两个 worker 抢着访问同一条总线,mock 立刻
记录到 `session-overlap`(两条连接同时开着)与 `frame-overlap`(一帧还没答完又来一帧)。
没有这个对照,"零冲突"等于什么都没证明。

分组只保证"同一总线的用例落在同一个 worker 上、由那个 worker 去连这条总线";每个 worker 进程
都能装配整份清单,所以用例必须只连自己测的那条总线 —— 默认套件就是这么写的,
`tests/parallel_bus_cases.py` 里的连接助手也只沿 `via:` 链连自己那一条。

挂在上游终端下的设备自己不建连接:泛型 fixture(`tests/conftest.py`)交付设备前会调
`CompositionRoot.get_ready()` 沿 `via:` 链把上游连好,同一条总线只连一次,用例里只连自己那台设备
(`meter.connect()`)。上游连不上时结论是"测试无效"(skip + `测试无效:` 前缀),不是下游设备不合格;
直接 `composition_root.get()` 拿设备、绕过 fixture 时,上游没连仍然当场报 `DeviceNotConnectedError`。

## 设备清单:判据集 + 型号 + 设备

`configs/devices.yaml` 分以下几段,细节写在文件里的注释中:

- `criteria_profiles` —— 判据集:一组具名的验收判据,按整机声明一次;型号用 `criteria` 引用它。
- `derived_criteria` —— 派生判据:跨多台设备算出来的量(走字误差)的区间。只写参与方与区间,**公式不写**(写在场景用例的 Python 里,见 `docs/WRITING_TESTS.md` 第 8 节)。
- `protocol_items` —— 协议级数据项目录:一个协议下每个数据标识(DI)的长度 / 编码 / 字节序 / 单位。
  协议固有信息在这里写一次,型号的点表项用 `item` 引用它(见下一节)。
- `models` —— 型号:点表、能力、判据集名、设备参数在这里声明一次。点表项的键就是**读数键**。
- `devices` —— 设备实例:只写 `id`、型号名、接入位置(`access` / `connection` / `bus_address`)和型号声明的设备参数。
- `test_matrix` —— 适用矩阵:型号 → 该型号实际跑哪些测试项。不在矩阵里的型号不受限(全跑)。

```yaml
models:
  dtsd1352_modbus:
    type: meter              # 大类
    protocol: modbus_rtu     # 规约;设备类型 = 大类 × 规约
    criteria: meter_220v
    capabilities: [有功电量, 电压]
    point_table:
      有功电量: {address: 0, length: 4, encoding: uint, scale: 0.01, unit: kWh}

devices:
  - id: meter_01
    model: dtsd1352_modbus
    access: serial           # 接入方式
    mode: simulator          # 改成 real 就走真实串口,驱动代码不用动
    bus_address: 1
```

读数超出判据**不会**让平台抛异常:读数照旧记录,结论是"不合格",并写明超了多少。
真正不该发生的事(未连接就读数、SOC 越出 0–100、额定容量非正)才是异常。

### 相等判定:`equals`

区间判据判"落在范围内",等值判据判"恰好是这个值" —— 写回参数类点的验证就靠它
("写 1601 → 读回应等于 1601"就是一条判据,`tests/test_meter.py` 里那条用例跑的就是这条链):

```yaml
criteria_profiles:
  meter_220v:
    - {quantity: 电压, min: 198, max: 242}
    - {quantity: 有功计量模式, equals: "1601"}
```

- `equals` 收数字、字符串、布尔。**按值比,不按类型比**:清单写 `"1601"`、点表读回 `1601.0`
  (`uint` × 倍率 1.0)是同一个值;数字比较带 `isclose` 容差,倍率换算的浮点尾巴不算不相等。
  任一边不是数字(文本 token 点)就按文本比,不去猜一个编码字符串的数值。
- 判不过时与区间判据同一个口径 —— 只说期望什么、实测什么:
  `有功计量模式 期望 '1601',实测 0.0`。
- `equals` 与 `min` / `max` / `gt` / `lt` **互斥**:一个判据要么判区间、要么判等值,
  两者同写在装配前报错,而不是求值时沉默地只看一半。

Modbus 点表项还有两个字段,不写就是 `read_holding_registers` + 高字先(与旧清单逐字节相同):

- `function`:功能码的语义名,`read_holding_registers`(0x03)/ `read_input_registers`(0x04)。
  很多电表类设备只实现 0x04;只允许 `modbus_rtu` 型号写。
- `word_order`:字序,`big` / `little`。值跨 2 个以上寄存器时,它与字内字节序 `byteorder`
  是两条独立的轴。只对 `length > 2` 有意义,写了就报错的情况:ascii、`length <= 2`。
- `writable`:`true` 表示这个点允许被写,缺省 `false`。它是**平台侧的许可**:没声明就写会报错
  并列出可写的点,不猜哪个点能写(见下一节)。

一次读数读点表里的全部点,但**请求次数不是点的个数**:Modbus 有寄存器地址,所以
`drivers/meter_driver.py` 先把点按 `(功能码, 地址)` 排序,再把地址相邻的点合并成一次
“读 N 个寄存器”的请求(`_plan_read_blocks`):

- 两个点之间空隙 ≤ `MAX_GAP_TO_MERGE`(10 个寄存器)就合并 —— 空隙里的寄存器也占应答字节和往返,
  超过这个数就另发一次请求更划算。
- 合并后宽度 ≤ `MAX_REGISTERS_PER_REQUEST`(120)—— 协议 PDU 上限是 125 个寄存器,
  留 5 个余量给“实现更严的从站”(超限回来的是异常码 03,不是数据)。
- **不同功能码绝不合并**:0x03 与 0x04 是两次不同的请求。

效果:20 个地址连续的点从 20 帧降到 1 帧(9600 波特下一次往返十几毫秒,单台设备
从 ~300 ms 降到 ~15 ms,而且少 19 次抢占总线与帧错位的机会)。

语义不变:仍然是一次读数 = 一次 `record_reading(全部点)`,判据仍然一次看全所有物理量 ——
合并只省请求次数,不把一次读数拆成多条。**块读只对 `modbus_rtu` 生效**:
`vendor_ascii` 是文本行、DL/T645 的数据标识不是连续地址,它们仍逐点读,不伪造批量语义。
代价是证据里的帧变少(这是预期的,`tests/test_meter.py` 有帧数用例盯着它)。

### 块读被拒时降级为逐点重试

合并会顺带读空隙里"没人要的寄存器"。真实电表往往没实现这些地址,于是**整帧**回来的是
异常应答(异常码 `02` 非法数据地址 / `03` 非法数据值)—— 一个字都不给,这一块里本来读得到的点
也跟着没了,比合并前更差。所以:

- **只对异常应答降级**:把被拒的那一块拆开,一个点表项一次请求重试。能读到的点照常返回,
  一次读数仍是一次 `record_reading(全部点)`(主场景:空隙没实现、点都实现 → 全部读成功)。
- **超时 / CRC 错 / 从站地址不符不降级**:那是链路问题,重试只会把 2 秒超时乘以点数,
  还会把"线断了"掩盖成"某个点读不到"。
- 降级后还有点读不到 → 这一次读数**失败**,抛 `ModbusPointsUnreadable`,消息里写明哪些点读到了、
  哪些没读到、各自的异常码。**不填默认值、不拿上一次的值顶上**:缺量的判据只会给出看着正常的假结论。
- 代价是变慢:第一次降级这些帧的往返不再是 1 次,而是 1 + 点个数。
- **降级会被记住**:被拒的块区间记在驱动实例上(不进证据、不进清单),下一轮只合并真正
  相邻的点(空隙 0 个寄存器),不再扫那些空隙。真机电表的寄存器地址几乎总有空洞,
  两个点 + 一个空洞:第一轮 3 次往返,之后每轮 2 次 —— 冷启动不知道有没有空洞,所以
  第一轮总是先试合并。只关掉被拒过的那些空隙,别处的点照旧合并。
- 证据里多一种记录 `kind: "batch_fallback"`,带块的地址区间、异常码、读到的点与失败的点,
  **只在第一次降级时写一条**(否则一轮 1440 次读就是 1440 条同类记录,现场会以为设备一直在报异常):
  ```json
  {"kind": "batch_fallback", "function": 3, "address": 0, "count": 3, "exception_code": 3,
   "readable": ["有功电量", "电压"], "unreadable": []}
  ```
  没有它,现场只看到"这次读数突然慢了好几倍"却看不出为什么。块读成功时不写这条记录,
  帧数与降级前完全一样。降级是性能事件,不是设备不合格:结论不变红。

### 写:只有点表里声明过 `writable` 的点才能写

`PointTableDriver.write(key, value)` 是**最小闭环**,不是通用寄存器写:

```yaml
point_table:
  有功计量模式: {address: 3, length: 2, encoding: uint, writable: true}
  时钟: {address: 4, length: 6, encoding: bcd, writable: true}   # YYMMDDhhmmss,3 个寄存器
  功率给定: {address: 6, length: 4, encoding: float32, word_order: little, writable: true}
```

四步:按点表 `encoding` / `length` / `byteorder` / `word_order` 反算成寄存器字节(就是读路径的
`decode_value` 的逆运算 `encode_value`,两边共用同一个字序重排)→ 发写帧 → 校验回显 → **立刻读回一次**。
返回值是**回读到的物理量**,不是刚写下去的那个数:设备"接受写入"和"值真的生效"是两件事。
回读不一致不抛错、不重试,照样返回读到的值,只在证据里留 `verified: false` ——
判不合格是判据层的事,写这一层只如实反映事实。

功能码由**寄存器个数**决定,不由用例选(用例只说"写这个点的这个值"):

| 占几个寄存器 | 帧 | 应答 |
|---|---|---|
| 1 个(`length` 1–2) | `0x06` 单寄存器写 | 8 字节回显(地址 + 值) |
| 更多(`length` > 2) | `0x10` 写多个寄存器 | 8 字节回显(起始地址 + 寄存器数),**没有数据段** |

寄存器是 2 字节:奇数 `length` 的点(如 3 字节 BCD)发送前补齐到整寄存器。

越界值报错、不截断:2 字节的点写 70000 抛 `ValueError` 并说出 `0–65535`,
不会截成 4464 那个"看着合理"的值。奇数 `length` 的点补齐出来的那一个字节不参与回读比较。

写方向的编码只支持数值编码(`uint` / `int` / `bcd` / `float32`):
`ascii` 文本 token 没有唯一的写形态(补空格?补几个?),写了会在装配期报错。

**BCD 的写法**:值是十进制数字串当成的数,补几位前导零由点的 `length` 定,
所以密码写 `120000`(3 字节 → `12 00 00`)与写 `"00120000"` 是同一件事。
非数字字符在装配期报错;位数超过 `2 × length` 报错并说出范围(3 字节 → `0–999999`);
位数不足是正常的,补前导零 —— 读回来的值再写回去,寄存器字节完全一致(往返不动点)。

三类失败归因不同,异常类型也不同:

| 现场 | 抛什么 | 归到哪 |
|---|---|---|
| 设备 / 链路没回话 | `TimeoutError` | 写失败(链路),不是设备不合格 |
| 设备回异常应答(明确拒绝) | `SlaveExceptionResponse` | 写失败(设备拒了这一写) |
| 写成功但回读不一致 | 不抛,证据里 `verified: false` | 设备不合格(值没生效) |
| 点没声明 `writable` / 键不在点表 | `ValueError` / `KeyError`,消息列出可写键 / 可用键 | 清单或用例写错了 |

切片边界(刻意不做):一次写多个不同地址、写失败重试、写后复位 / 收尾。
模拟从站(`SimulatedModbusSlave`)与独立 mock 从站(`tools/mock_modbus_meter.py`)实现同一套
写语义:`0x06` / `0x10` 都把值写进寄存器表(写后同一实例再读能读到),应答是回显;
地址不在表里回异常码 `02`,寄存器数非法回 `03`。两份实现在
`tests_platform/test_mock_meter_end_to_end.py` 里用同一串请求帧对照应答字节,不许各自漂开。

整份清单在装配前校验:型号引用、规约与接入方式是否配套、点表字段、判据键、设备实例的键。
拼错一个键(`rated_powerw`)当场报错,不会静默落回默认值 —— 那是这个平台上最危险的 bug。

判据量与能力还要落在这台设备**真能判的量**里(装配期一次报全,按型号分组):判据集拼错一个
量名、`capabilities` 声明了点表读不到的能力,都在接台架之前报错,而不是等到第一条读数 ——
能力声明用于"没有就跳过用例",声明了却读不到就是让跳过机制说谎。“能判的量”是并集:
电表的判据量就是点表读数键,vendor_ascii 型号的判据量是驱动把读数键映射成的聚合根参数名
(`有功功率` → `output_power_w`),所以这项校验等驱动造出来才跑。`derived_criteria` 不在其中:
它的量是跨设备算出来的,不属于任何一台点表。

### 设置类条款:清单里一条一行,平台侧一个函数

上游 659 条条款里 527 条是"写 N 个参数 → 读回 → 判相等",没有因果链。这类条款不写进
`tests_scenario/`(写成场景函数就是把上游 350 行/条的文件换个外壳),它们留在清单里:

```yaml
setting_cases:
  dtsd1352_modbus:                     # 型号:点表在哪,决定哪些点可写
    M_00_00_10_000008_02:
      clause: "DL/T 645 4.5.2"
      steps:
        - {write: 有功计量模式, value: "1600", expect: "1600"}
        - {write: 有功计量模式, value: "1601", expect: "1601"}
```

用例侧只有 `tests/test_setting_cases.py::test_setting_roundtrip` 一个函数:一条条款一个节点,
节点 id 就是条款编号(`test_setting_roundtrip[M_00_00_10_000008_02]`),证据与归因里每条条款
各有自己的 `case.id` 与 `clause`(身份由参数给:`@case_from_param("case_id")`)。
新增一条条款 = 加几行 yaml,不用新建文件。装配前校验:型号存在、条款编号全清单唯一、
`write` 的键是该型号点表里 `writable: true` 的点、`value` 与那个点的
`encoding` / `length` 相容(BCD 点写密码 / 时钟形态的十进制串)、`expect` 是单个值。
不支持条件 / 循环 / 引用前一步读数;期望是区间、或参数依赖前一步读数的条款拆开或归场景层。

#### 台架前置:`bench:` 是让表能测的前提,不是步骤

上游 `13通信` 105 条条款前面都有一整块三相台架设定(接线方式 / `Ua=Ub=Uc=Un` /
`Ia=Ib=Ic=0` / θ / `Fre`)。它**每一条都一字不差**,而且不是被测行为 —— 所以写成条款的
一个 `bench:` 字段,不进 `steps`(ADR 0006):

```yaml
    M_00_00_13_0002FF_61:
      clause: "DL/T 645 4.5.2"
      bench: {Ua: 220, Ub: 220, Uc: 220, Ia: 5, Ib: 5, Ic: 5,
              θa: 60, θb: 60, θc: 60, QUaUb: 120, QUaUc: 240, Fre: 50}
      steps:
        - {write: 有功计量模式, value: "1601", expect: "1601"}
```

- **量名走同一份 `configs/import/params.yaml`**(导入器用的那一份),不另开一套拼写:
  拼错一个量名在装配前报错,并列出可用的量。
- **符号值按型号解析,不拿常数兜底**:源文件写的 `Ua=Un` / `Ia=In` / `Fre=Hz` 由
  `configs/import/symbols.yaml` 指到型号参数(`rated_phase_voltage_v` / `rated_current_a` /
  `rated_freq_hz`),值取型号声明的 `device_params` + 设备实例上那个数。`Un` 是**相电压**
  (三相四线表 `Ua=Un` 填 220,填 380 会把台架设成线电压);DL/T 645 的 `Ib`(基本电流)
  与 `In` 是两个量,**没有**映射 —— 确认之后参数名用 `basic_current_a`。型号没声明、实例没给
  值(或值还是 `TODO` 占位)、同型号几台实例给的值不一样,导入时都**拒收**并点名要改哪一行 ——
  拿 220 / 50 兜底会让台架按错的额定值跑,而且报表里看不出来。
- **值要与台架型号的点表相容**:台架型号没有那个量、那个量没声明 `writable`、值不是数
  (写成枚举名 `3P4W`)都在装配前报错并点名是哪个量。
- **执行顺序**:连接 → `bench:` 前置(经周边设备驱动 `apply_three_phase` 下发)→ steps →
  回读判定。前置没发下去 = **测试无效**(`skip`,和台架断链同一档);前置发下去了、
  表没反应 = **设备不合格**。证据里两种现场靠 `bench_setup` 记录与随后的 `reading` 分开:

```json
{"kind": "bench_setup", "bench": "grid_sim_01", "case": {"id": "M_00_00_13_0002FF_61"},
 "setpoints": {"Ua": 220.0, "Fre": 50.0}, "applied": {"Ua": 220.0, "Fre": 50.0}}
```

- 被观测的台架动作(把台架调到 X → 读表 → 判 Y、或条款中途两次调台体)不是前置:
  这类条款整条归场景层,`bench:` 跟着场景用例走。导入器把这条分流逐条写进覆盖率报表。

#### 人工步骤:`manual` 是第三种状态,不是通过

上游 1,921 条条款里 **7,589 步(17%)** 的操作类型是 `人工操作` / `人工操作/判断`:
切换上位机通信模式、目视看液晶、拔插卡。平台只能自动跑动作,所以这一档显式写进清单:

```yaml
    M_00_00_13_000101_01:
      clause: "DL/T 645 4.5.2"
      steps:
        - {write: 有功计量模式, value: "1601", expect: "1601"}
        - {manual: "切换上位机通信模式为 HDLC", expect: "由操作员确认已切换"}
```

`manual` 是做什么,`expect` 是**文字说明**(由谁按什么确认),不是机读判据 —— 写成数字
在装配前就被拒:一个人工步骤带着机读判据,下一步就会有人把它当自动步骤判过。
`manual` 步骤不能再写 `write` / `value`(人工操作没有可写的点,而"写一半 + 人确认另一半"
在报告里读不出哪半算数)。

结论语义:含人工步骤的条款按清单顺序**先跑前面的自动步骤**(写 → 读回 → 判相等),
全部通过之后碰到人工步骤才 `pytest.skip` —— 不判通过也不判失败,理由写“含 N 个待人工确认步骤
(前面的 M 个自动步骤已执行且通过)”(退出码仍是 0)。
自动步骤**失败**时这条条款就是 `failed`:真故障不许被“等人确认”盖住。

不整条不跑的理由是覆盖:7,589 个人工步骤分布在 1,921 条条款里(平均每条 3.9 步),
大量条款含人工步骤 —— 整条 skip 会把同一条款里的自动步骤(写参数、读回、判相等)一起丢掉,
而那些恰恰是能抓设备故障的部分。

它与“测试无效:”是不同一档:测试无效是台架断了、下一步查线;待人工确认是这一步本来
就要人动手、下一步找操作员。证据里每一步人工操作留一条占位(带 `executed_steps`:停在它之前
已执行并通过的自动步骤数),自动步骤自己的写入与读数记录照旧就在同一份文件里:

```json
{"kind": "write", "key": "有功计量模式", "value": 1601.0, "read_back": 1601.0, "verified": true}
{"kind": "manual_step", "case": {"id": "M_00_00_13_000101_01", "clause": "DL/T 645 4.5.2"},
 "action": "切换上位机通信模式为 HDLC", "expect": "由操作员确认已切换", "confirmed": null,
 "executed_steps": 1}
```

`confirmed` 留给回填(确认人 + 时间);回填工具本轮未做,但键先留对 —— 缺键与“填了没确认”
同义,分不清就会把没确认的步骤读成已确认。归因侧:这类记录归进「没有有效结论」,
逐条写“已执行 N 个自动步骤且全部通过、还有 M 步待人工确认”,并在规模行里单列“待人工确认 N 条”,
不算设备不合格、也不算证据损坏。自动步骤失败另归「设备不合格」,并点名是哪个键、期望什么、实测什么。

## 协议数据项目录:协议固有信息只写一次,型号只声明差异

`length` / `encoding` / `byteorder` / `unit` 是**由数据标识(DI)决定的,不是型号决定的**:
上游 DL/T645 字典里 `00010000` 就是 4 字节 BCD、低字节先、单位 kWh。
把它们抄进每个型号的每个点,就是几万份可以各自抄错的东西 —— 抄错 `encoding: bcd` 成 `uint`,
报告里是一个数量级错、看起来正常的假读数。

清单的 `protocol_items` 段按协议登记数据项,**型号的点表项只写差异**:

```yaml
protocol_items:
  dlt645:
    "00010000": {name: 正向有功总电能, length: 4, encoding: bcd, byteorder: little, unit: kWh}
    "02010100": {name: A相电压,       length: 2, encoding: bcd, byteorder: little, unit: V}
    "02020100": {name: A相电流,       length: 3, encoding: bcd, byteorder: little, unit: A}

models:
  dtsd1352:
    type: meter
    protocol: dlt645
    criteria: meter_220v
    capabilities: [有功电量, 电压]
    point_table:
      有功电量: {item: "00010000", scale: 0.01}   # 只剩型号差异
      电压:     {item: "02010100", scale: 0.1}
```

同一个型号两种写法(6 个点,`yaml.safe_dump` 的 block 形态,与规模清单的排版一致):
内联 `point_table` 42 行;**引用 18 行 + 目录 37 行一次**(目录被该协议下所有型号共用,
第 2 个型号起每型号只多 18 行而不是 42 行)。一行一个点的紧凑写法里,内联那一行 571 个字符、
引用那一行 223 个字符 —— 少掉的就是被重复抄写的协议事实。

规则(与其它段同一套"闭合集合 + 拼错就报"口径):

- 点表项**二选一**:内联写全(旧写法原样有效),或 `item` 引用目录。两种写法解析出逐字段相同的 `Point`。
  `length` 和 `encoding` 从目录继承,读数的解码路径完全一样(`tests/test_protocol_items.py`)。
- 引用时**写了的字段就是覆盖**:常见的是 `scale`;`encoding` / `byteorder` / `word_order` /
  `function` / `unit` / `length` 走同一条路,没有针对 `scale` 的特判。没写的字段从目录取。
- **不许两处真相**:`item` 与内联 `address` 同时写、或把目录项逐字相同的字段在型号里再抄一遍 →
  装配前报错。要改就改目录,要覆盖就写不同的值。
- `item` 指向的 DI 不在该协议目录里 → 装配前报错,消息带型号、点表项、规约、目录里已声明多少个 DI
  与最接近的几个(不把两万个 DI 打出来)。
- `protocol_items` 的协议名必须是已登记的规约;目录项的字段也是闭合集合(拼错 `lengh` 当场报错),
  并且 `length` / `encoding` 必填 —— 那正是从型号里搬进目录的部分。

**仓库不是数据仓**:上游 `common/p645dict.py` 有 4.9 万条数据项(去重后 24,712 个 DI),
不要把字典整份复制进来。真接入某个型号时,只把它用到的 DI 按需导入到 `protocol_items`;
样例只放几个真实 DI 够证明机制成立。

## 独立从站 mock:在接真实电表前把链路跑通

`transport/simulators.py` 里的 `SimulatedModbusSlave` 是进程内的假实现:它不经过 socket,
证明不了帧边界、超时、重连这些只有真实链路上才会出问题的地方。
`tools/mock_modbus_meter.py` 是一个独立的 Modbus RTU 从站进程(TCP 承载 RTU 帧),
平台用 `mode: real` + `access: tcp` 连它 —— 除了不接真表,链路上一处没省。

```bash
python tools/mock_modbus_meter.py --port 15020 --slave 1 --registers configs/mock_registers.yaml
python -m pytest -q --device-config configs/devices_mock.yaml   # 另开一个终端
```

帧的拼装、解析与 CRC 复用 `protocol/modbus_rtu.py`,mock 里没有第二套实现;收发只用标准库
`socket` / `threading`。功能码 03/04 读、06 写单个、10 写多个都支持:写进去的值落到
`--registers` 那张表里(所以写后立刻读能读到新值),应答按 Modbus 回显;
地址不在表里回 `02`(非法数据地址),个数不合法回 `03`(非法数据值)。
故障注入才是它相对进程内模拟器不可替代的部分:

```bash
... --fail timeout           # 收下请求、一个字不回:平台必须在 2.0 秒超时后报错,而不是读到旧值
... --fail bad-crc           # 回一帧 CRC 错的:证明帧校验真的在生效
... --fail slow --delay 2.0  # 延迟回应
... --fail write-reject      # 每个写都回异常码 03:证明"设备拒了这一写"与"链路没回话"分得开
```

一个 mock 进程 = 一条物理总线。`--slave` 可以给多次(这条总线上挂着几台从站,寄存器表共用一份),
`--contention-log <文件>` 把"同一条总线上同时来了两个请求者"每条记一行 JSON:
`{"kind": "session-overlap"|"frame-overlap", "at": …, "connection": n, "other": m}`。
并行分组实测(`tests/parallel_bus_check.py`)就靠这份记录判"防住了没有";不给 `--contention-log`
时这些记录打到 stderr。

停止:Ctrl+C;脚本里加 `--stop-on-stdin-close` 后关掉它的 stdin 就干净退出(退出码 0,不留监听端口)。
同一端口只允许一个 mock:第二个实例启动就非零退出并说明端口被占(Windows 下靠 `SO_EXCLUSIVEADDRUSE` 独占,不是 `SO_REUSEADDR`)。
启动行里带 pid,端口被占时拿它查出是哪个进程。

`modbus_rtu` 允许 `access: tcp`,指的是 **RTU 帧走 TCP**(串口服务器的常见形态,帧里只有从站地址、功能码、数据与 CRC);
**Modbus TCP 是另一个规约** —— 它的帧带 MBAP 头(事务号 / 协议号 / 长度 / 单元号),用 `modbus_rtu` 去连它只会解析失败。

`configs/devices_mock.yaml` 是 `configs/devices.yaml` 的副本,只把 `meter_01` 换成真实 socket。
上面那条「人工起 mock 再带 `--device-config` 跑」验证的是**这份出厂清单**(端口写死在清单里,
只能人填);整链(清单解析 → 装配 → 点表求值 → 判据 → 证据落盘)则默认就跑:
`tests_platform/test_mock_meter_end_to_end.py::test_mock_meter_round_trip_over_a_real_socket`
自己起 mock、拿系统分配的端口现造一份清单,不加任何命令行参数、不预先起 mock 也是真跑,
CI 因此不会因为缺这台 mock 而少测一段。出厂清单自己的可用性另有单独一条用例覆盖
(`test_factory_mock_manifest_assembles_without_a_running_mock`),不依赖有 mock 在跑。

## 目录结构与依赖方向

```
test_platform/
├── configs/devices.yaml          # 设备清单:新增/改设备只改这个文件
├── src/testplatform/
│   ├── domain/                   # 聚合根 Inverter / Battery / Meter / Terminal / GridSimulator + 判据值对象
│   ├── protocol/                 # 帧格式、CRC、编码解码;纯函数,不 import transport 与 domain
│   ├── transport/                # interfaces.py 定义各协议族 Protocol;real_transports / gateway_transport / simulators 被动满足
│   ├── drivers/                  # (设备类型, 规约) 注册表 + 点表驱动的通用读取路径 + 各设备驱动
│   ├── application/              # 编排:event_relay / event_sink
│   ├── config/                   # loader.py 读 yaml,manifest.py 校验三段式清单
│   ├── reporting/                # evidence.py 证据落盘,event_sinks.py 事件去处
│   ├── cases.py                  # @case 用例身份 + --select / 适用矩阵的筛选条件
│   └── composition/root.py       # 组合根
├── ...                          # pytest 插件在包里,入口是 src/testplatform/plugin.py
│                                #   (以 entry point `pytest11` 注册,不再放仓库根)
├── conftest.py                   # 注册插件
├── tests/                        # 一致性测试:按设备类型展开,每台被测设备独立判定
├── tests_platform/               # 平台自身的行为:归因、证据、拓扑分组、断链归属、端到端链路
├── tests_unit/                   # 平台自身单测:不需要任何设备清单
├── tests_integration/            # 平台自身在多设备台架上的行为(见该目录 README.md)
├── tests_support/                # 测试基础设施(清单工厂 / 嵌套 pytest / 具名设备),不做任何断言
├── tests_scenario/               # 场景测试层:纯代码 + 显式参与方,场景结论落 scenario-<用例名>.json
├── docs/WRITING_TESTS.md         # 写用例的四种形态与坑
├── docs/external-inputs.md       # 还缺什么(对外索取清单):带假设的确认题 + 拿到后填哪个字段
├── docs/how-to/                  # 按“你现在要干的那件事”找:加设备 / 写场景 / 读证据 / 上真机
├── docs/adr/                     # 已经定过的决策
├── tools/mock_modbus_meter.py    # 独立的 Modbus RTU 从站(TCP 承载 RTU 帧),带故障注入
├── tools/attribute_round.py      # 一轮的失败归因:哪些设备不合格 / 哪些测试无效 / 断在哪一步
└── evidence/<会话时间戳>/<设备id>.jsonl   # 每次运行的证据(默认开,--no-evidence 关;并行时目录名带 worker 名)
```

依赖方向:`domain/` 不认识任何协议;`drivers/` 只依赖 `domain/`、`protocol/` 的纯函数和
`transport/interfaces.py` 里的 Protocol;`transport/` 的各实现互不知道对方存在;
只有 `composition/root.py` 与 `src/testplatform/plugin.py` 认识具体实现。

## 三条原则分别落在哪

**消费者定义接口**(`transport/interfaces.py`)
各协议族形状完全不同(字节流式、帧式、请求响应式、命令执行式、发布订阅式),
刻意不做成大而全的 `Transport` 接口。接口的真正消费者是 `drivers/` 里的驱动代码,
`real_transports.py` / `gateway_transport.py` / `simulators.py` 只是被动去满足它们。

字节流的帧边界也由调用方定:`receive(timeout, expected)`,其中 `expected` 是字节数,
或者一个「已收到的字节 → 整帧长度」的函数(Modbus 应答的字节数、DL/T645 的 L 都得
先拿到应答头才知道)。`transport/` 不认识任何规约,只负责短读继续读、收满才返回;
超时还差字节就抛 `IncompleteFrameError`(`TimeoutError` 的子类,证据里仍记成 `error: timeout`),
半帧绝不交给上层 —— 半帧解出来的"看着正常"的假读数最难查。长度未知的文本行
(如 vendor_ascii)不传 `expected`:读到链路静默为止,上限 64 KiB,顶到上限报错而不静默截断。

**链路没了不是超时。** TCP 的 `recv` 返回 `b""` 只表示对端正常关闭(拔网线、设备断电、
串口服务器重启),这时抛 `ConnectionError`、证据记 `error: closed`;设备收下请求但一个字不回
才是超时(`error: timeout`)。两者现场处置完全不同:断链是台架问题(测试无效),
超时可能是设备问题(判不合格),所以底层不能合并。串口是个例外:`read` 超时返回 `b""`、
拔线抛 `SerialException`,硬件层面没有"对端关闭"这个信号,只能把两者都记成超时。

**断链归谁,只看设备清单里的 `role`。** 收帧阶段收到 `ConnectionError` 时:
`role: dut` 的异常原样上抛 —— 链路是它自身的一部分,断链就是它不合格;
`role: support` 的在组合根里就换成 `SupportLinkLost`,测试层把它转成
`skip("测试无效: …")`(和连接阶段的周边设备故障同一个前缀、同一个结论),
**整轮照常跑完、退出码不变**,但那一条用例不产出有效结论。同时留三处痕迹:
证据里一条 `kind: link_lost`(设备 id / 时间 / 失败种类 / 当时在收发哪一侧)、
会话结束时的终端小节「本轮存在断链 —— 结论需复核」、
以及 `evidence/<会话>/round-status.json` 里的 `link_lost` 列表与 `verdict`(`needs-review` / `clean`)。
没有断链时这份文件照样写、`link_lost` 为空 —— CI 读它就能判断这一轮要不要人复核,
不必解析终端文本。

`has_client()` 只回答"底层客户端/句柄对象还在吗"(connect 之后有、disconnect 之后没有),
**不代表链路健康**:TCP 对端拔线或 RST、USB 转串口被拔掉之后,对象都还在,它仍然是 `True`。
链路死没死以 `send` / `receive` 抛出的实际异常为准(证据层按异常记 `timeout` / `closed` / `disconnect`);
判断"要不要重连"只能看异常,不能看 `has_client()`。它刻意不叫 `is_connected` ——
那个名字会让人把它当链路判据。真机实现、模拟器、网关透传、证据装饰层用的是同一个名字、同一种语义。

**聚合根**(`domain/`)
每种设备保护自己的不变量;历史读数只能追加(`history` 返回 tuple,内部 dataclass `frozen=True`),而且只留最近 1000 条(`domain/device.py` 的 `HISTORY_WINDOW`)——`history` 是窗口,不是全量。要"这台设备一共记了多少条"看 `readings_recorded`(只增不减),证据层按它的差值取增量,不用 `len(history)` 当游标。
驱动只调领域方法,聚合根不认识协议细节。判据是值对象,阈值全在清单里,`domain/` 里没有一个默认阈值。

**组合根**(`composition/root.py` + `src/testplatform/plugin.py`)
全项目只有组合根顶部那几行 import 触发 `@register_transport` / `@register_device` 真正执行;
`composition_root` 这个会话级 fixture 是整个 pytest 会话里唯一一次 `CompositionRoot.build()`。

## 扩展指南

### 新增一个型号(同大类、同规约)

只改 `configs/devices.yaml`:在 `models` 里加一段,再在 `devices` 里加至少一条引用它。
点表、能力、判据集写一次,同型号的多台实例不重复。零 Python 文件;
已有的一致性测试会自动对新型号的每台设备展开一次(`test_xxx[新设备id]`)。

### 新增一个规约(同一个大类,比如给电表再加一种规约)

1. `protocol/` 下加一个模块:拼帧、校验、按点表项解码,纯函数。
2. 在对应驱动里加一个 `read_raw`(一个点表项 → 一次收发 → 原始字节),用 `@register_device("meter", "新规约")` 注册。
   若该规约的地址是连续的(能一次读一段),再给一个可选的 `read_raw_many` 做块读;
   给不出就不要给 —— 通用读取路径会逐点读,语义一样。
3. `config/manifest.py` 的 `PROTOCOL_ACCESS` 加一行"新规约 → 允许的接入方式"。
4. 清单里型号的 `protocol:` 换成新规约。

通用读取路径 `drivers/point_table_driver.py` 不改 —— DL/T645 接入时验证过这一点。

### 新增一个设备类型(新大类,比如充电桩)

1. `domain/` 下新建聚合根(参考 `battery.py`):只写物理量与不变量,不碰协议。
2. `drivers/` 下新建驱动(参考 `battery_driver.py`):用 `@register_device("新大类", "规约")` 注册工厂,
   并在 `composition/root.py` 顶部的 import 列表加一行,让装饰器真正执行。
3. 清单里加型号 + 设备。

测试层不用改:按类型展开的 fixture 由注册表生成(`tests/conftest.py` 末尾),
`@pytest.mark.requires_capability` 按型号声明的能力跳过。

### 新增一种周边设备类型

同"新增一个设备类型",清单里那条设备写 `role: support`。周边设备不判合格、不参与一致性测试展开;
它连不上时结论是"测试无效"(整轮 skip),不是被测设备不合格。

### 台架型号 `grid_simulator_3ph`:量名、占位地址、三相设定

上游条款的台架前置是一整块三相设定,量名按上游写法声明,**三相电压 / 电流是独立的量**(条款里
`Ua` 和 `Ub` 是两件事,合成一个「电压」就把信息丢在清单里了):

```yaml
  grid_simulator_3ph:
    type: grid_simulator
    protocol: vendor_ascii
    capabilities: [Ua, Ub, Uc, Ia, Ib, Ic, θa, θb, θc, QUaUb, QUaUc, Fre, 接线方式, MeterConst]
    point_table:
      Ua: {address: TODO, length: 7, encoding: ascii, unit: V, writable: true}
      ...
      接线方式: {address: TODO, length: 2, encoding: uint, writable: true}   # 枚举,不是模拟量
      MeterConst: {address: TODO, length: 2, encoding: uint, writable: true}  # 整数参数
```

- **地址是占位(`TODO`)。** 真实台架的寄存器地址 / 编码 / 倍率还没拿到。`mode: real` 时装配期
  直接报错并列出是哪几个量;`mode: simulator` 照常跑。编一个"看着像"的地址写进去、回读还一致,
  是最坏的一种假绿 —— 宁可报缺。
- **量名是清单契约。** 旧清单里的 `电压` / `频率` 不再认,报错直接给改法;
  不做别名(同一个量两个名字,迟早有一处改动没跟上)。
- **`接线方式` 的枚举名 → 整数在 `configs/import/params.yaml`**,不散在驱动里;驱动只接收整数
  (设成 `3.5` 是平台自身的不变量违反)。
- **三相设定是 `SET_GRID3 量名=值 …` 一帧**,应答按同一份量名回。模拟台架
  (`connection: {simulated_grid: true}`)按设定回读,用来验驱动的帧与量名;
  单相的 `apply_grid(电压, 频率)` 与 `initial_setpoint` 照旧。

### 一条用例失败之后台架怎么复位

写原语上线之后,"用例把参数改掉之后失败"会污染后面所有用例:下一个用例读到的值会被判成
**设备不合格**,而归因里没有"被上一条用例污染"这一类。所以收尾按 `role` 分层:

| 角色 | 收尾做什么 | 失败时 |
|---|---|---|
| 周边设备 `support` | 回到清单声明的 `initial_setpoint`(如 `{voltage_v: 230, freq_hz: 50}`) | **测试无效**,不是设备不合格 |
| 被测设备 `dut` | 只做**用例自己声明的**收尾(`scenario.on_cleanup(...)`) | 这条用例失败,证据标 `cleanup_failed` |
| 平台自身 | 补证据 + 断开 | — |

- 复位在**每条用例结束后**与整轮结束前各做一次(`tests/conftest.py` 与
  `tests_scenario/conftest.py` 的 autouse fixture 调 `CompositionRoot.restore_support()`),
  所以下一条用例拿到的台架总是在初始设定上。恢复失败 → `skip("测试无效: …")`,退出码不变。
- **平台从不在收尾里往被测设备写东西**:整轮结束时设备被人动过,比留在工厂模式更难查。
  `initial_setpoint` 只允许写在 `role: support` 的设备上(清单解析时拒绝),
  复位也只遍历这张表;`tests/test_bench_restore.py` 用"复位与 teardown 期间被测设备零发送帧"来钉这一条。
- 声明了 `initial_setpoint` 而驱动没有 `apply_initial_setpoint`(消费者定义的
  `SetpointRestorableDriver` 接口)时**装配期就报错**:否则复位只是"静默没发生",
  后面所有读数都被污染,报告里看不出来。

### 新增一种接入方式(比如新的串口服务器、WebSocket 通道)

1. `transport/` 下实现 `interfaces.py` 里某个 Protocol(总线类设备一般用 `ByteStreamTransport`)。
2. `@register_transport("名字", "real")` 注册真实实现,`simulators.py` 里注册 `"simulator"` 模式。
3. 清单里设 `access: 名字`;还要在 `config/manifest.py` 的 `PROTOCOL_ACCESS` 里给对应规约放行。

`gateway_transport.py` 是真实示例:作为新的字节流实现加入,逆变器和电池的驱动、聚合根一行未改。

**`http` / `mqtt` / `ssh` 三条传输已实现并注册,但清单里暂时不可达**:今天没有任何规约在
`PROTOCOL_ACCESS` 里放行这三种 access(没有设备用 REST / MQTT 上报 / 主机命令访问),
所以在 `devices.yaml` 里写 `access: http` 会被 `parse_manifest` 拒绝,并说明原因与下一步。
这不是坏代码、也不是漏实现:凭空造一个 `http` 规约只会得到「能配但装配必失败」的假象。
要接这类设备,先定它用什么规约上这种接入(如 EMS 走 REST、HEMS 走 MQTT 上报、工控机走 SSH 命令),
再在 `PROTOCOL_ACCESS` 里放行。三个类与三个注册工厂上的注释指向同一条边界(`TRANSPORT_ONLY_ACCESS`)。

### CAN 接入:接口类型不是 socketcan 专用

CAN 设备的型号规约写 `canopen`(CANopen 是 CAN 上的应用层规约,CiA 301;电池 / 储能另有
CiA 418 / 419 行规),它在 `PROTOCOL_ACCESS` 里只放行 `access: can`。所以 CAN 设备的清单长这样:

```yaml
models:
  bms_can:
    type: battery
    protocol: canopen
devices:
  - id: bat_can
    model: bms_can
    access: can
    connection: {channel: can0, interface: pcan, bitrate: 500000}
```

接口类型在清单里配,不写 `interface` 时按平台给缺省(Linux `socketcan`,
Windows `pcan`,其余平台 `slcan`)。可用值(白名单在 `config/manifest.py` 的
`CAN_INTERFACES`):`socketcan`(Linux 内核自带)、`pcan`(PEAK PCAN-USB)、
`vector`(Vector 板卡)、`slcan`(USB 转 CAN 串口适配器)、
`virtual`(没有硬件时自测)。`socketcan` 只有 Linux 有,写死在代码里会让 Windows 上接了
CAN 设备直接起不来;未知值在装配前就报错并列出可用值,connection 里拼错的键也一样拒绝。
`canopen` 目前没有设备驱动:装配会停在"未找到设备工厂",而不是停在清单校验。

### serial / tcp / gateway_ssh 的 connection 键是闭合的

这三族字节流的 `connection` 键与传输实现真正读取的键一一对应,拼错立即在装配前报错,
不会静默取缺省值(波特率把 `baudrate` 拼成 `baudrat`,真机上表现为"连上了但读数是乱的"):

```yaml
- id: bat_01
  access: serial
  connection: {port: COM3, baudrate: 19200}          # port 必填
- id: inv_01
  access: tcp
  connection: {host: 192.168.1.50, port: 502}        # host / port 都必填
- id: inv_02
  access: gateway_ssh
  connection: {gateway_host: 10.0.0.5, gateway_user: test,   # 这三个必填
               local_bus: can0, gateway_key_path: ~/.ssh/id_rsa}  # key_path 可选
```

模拟器专用键 `simulated_response` / `simulated_645` / `simulated_registers` / `simulated_grid`
四族都合法。
`local_bus` 会被拼进网关主机上执行的 shell 命令,所以只允许 64 个以内的字符,字符集
`A-Za-z0-9_.:/-`(总线名如 `can0`,设备节点如 `/dev/ttyUSB0`);带 `;` / `|` / `$()` / 反引号 /
空格 / 换行的值在装配前直接报错。
必填键只在 `mode: real` 下强制:模拟器不读主机名和端口,`mode: simulator` 下不逼你写一个
没人读的假地址。可用键在 `config/manifest.py` 的 `STREAM_ACCESS_CONNECTION_KEYS`。

### SSH 主机密钥:未配置 = 不校验

经工控机网关(`gateway_ssh`)或 SSH 命令(`ssh`)接真机时,平台默认**不校验对端主机密钥**
—— 与修复前完全一致(台架网络里有人冒充网关时,平台会把别人的回帧当设备数据,证据里看不出来)。
要挡这个,在 `connection` 里配上 known_hosts 路径:

```yaml
- id: inv_02
  access: gateway_ssh
  mode: real
  connection:
    gateway_host: 10.0.0.5
    gateway_user: test
    local_bus: can0
    gateway_known_hosts: ~/.ssh/known_hosts   # 直连 ssh 用键名 known_hosts
```

- **未配置** → 不校验主机密钥(与以前一样连得上)。这是当前缺省;建连时记一行
  `注意: <主机> 的主机密钥未校验(未配置 known_hosts…)`,让运行时看得出这次连接信任到哪。
- **配置且匹配** → 照常连接,并记一行 `SSH 主机密钥已校验: <主机> <算法> <base64>` 日志。
- **配置且不匹配**(或该主机根本没登记)→ 连接被拒绝,报错里有**期望指纹**、**实际指纹**
  和登记命令(`ssh-keyscan -p <端口> <主机> >> <known_hosts>`)。人工确认那台机器确实是
  你的网关之后再把指纹登记进去 —— 换个密钥的网关和冒充的网关在这一层长得一模一样。

装配期只校验**路径存在且是文件**,不读内容:内容写错就会变成“真机连不上、报认证失败”,
另一种难查的失败形态。第一次登记网关指纹:

```bash
ssh-keyscan -p 22 10.0.0.5 >> ~/.ssh/known_hosts   # 先人工核对输出里的指纹
```

比对与拒绝在 paramiko 的 `missing_host_key` 钩子里(`transport/real_transports.py` 的
`_VerifyHostKeyPolicy`):每次建连只算一次指纹,超时重连也必经它。

### 真串口的两个卫生参数(RS485 换向、帧间隔)

这两件事**归平台管,不归适配器管**,而且模拟器和假串口永远测不出来:真机上出问题时,
现象看起来都像"设备坏了"。

**换向前清残留缓冲(无需配置)。** `send` 在写出请求帧之前先 `reset_input_buffer()`,把上一帧
剩下的字节(从站多发的那个字节、上一次超时后迟到的应答)丢掉。不清的话下一帧从头就错位,
而错位解出来的往往是一个"看着正常"的**假读数**,比 CRC 失败难查得多。
它刻意放在 `send` 前、不放在 `receive` 前:清在 `receive` 前会把刚好先到的应答一起丢掉。

**最小帧间隔 `inter_frame_delay_ms`(可选,缺省 0 = 不等待)。** Modbus RTU 要求帧间静默
至少 3.5 个字符时间。平台现在总是背靠背发送;有些从站在严格总线上会因此把两帧粘成一帧,
表现为"设备不回话 / CRC 错"。遇到这种现场就配它:

```yaml
- id: meter_01
  access: serial
  connection: {port: COM3, baudrate: 9600, inter_frame_delay_ms: 4}
```

按波特率估:一个字符 11 位(8 数据位 + 起始位 + 校验位 + 停止位),3.5 字符 = 38.5 位,
所以 `inter_frame_delay_ms ≈ 38500 / baudrate` —— 9600 下约 4ms,19200 下约 2ms,
115200 下不到 1ms(取整到 0 就没意义了,老实的做法是至少配 1)。3.5 字符时间是协议下限,
裕量按现场给;配得过大只是让每帧慢一点。
缺省 0 时**一次都不调 sleep**,不给所有跑法平白加延迟。

**局限:帧间隔是按连接算的。** 计时器在一条 `transport` 实例里。同一台设备背靠背读多个点时
它生效;但如果同一条物理总线(同一条 RS485)上挂着多台设备、它们各自有自己的 transport
(比如两个 `access: serial` 的设备各写了一台串口适配器),**间隔不会被跨连接强制执行**,
两条连接之间的帧仍然可能粘在一起。这种情况真正的解法是让它们共享一条总线(用 `via:`
挂在同一台上游设备下),或者把 `inter_frame_delay_ms` 配得比单帧往返大,让背靠背概率降下来。


## 写用例

- 一致性测试:`tests/`,用按类型展开的 fixture(如 `inverter`)写契约用例。
- 平台自身单测:`tests_unit/`,直接 new 聚合根或用 Fake 驱动,不需要设备清单。
- 平台自身行为:`tests_platform/`,归因 / 证据 / 拓扑分组 / 断链归属 / 真实链路端到端。
- 多设备台架:`tests_integration/`,显式点名要哪几台设备;定位见 `tests_integration/README.md`。
- 场景层:`tests_scenario/`,参与方按 `id` 点名,场景结论落 `scenario-<用例名>.json`(ADR-0005),在默认 `testpaths` 里。
- 设置类条款(`写 → 读回 → 判相等`,没有因果链)写进清单的 `setting_cases` 段,不写测试函数:
  一条条款一行,节点 id 就是条款编号。人工步骤(`manual`)也写在这里:前面的自动步骤照跑,
  碰到人工步骤才 skip,不判通过。
  见上面「设置类条款」。
- 场景里动过设备(写参数、进工厂模式)就登记收尾,别指望平台猜:

  ```python
  def test_writes_then_restores(composition_root, scenario):
      scenario.on_cleanup(lambda: meter.write("有功计量模式", 0), label="写回出厂值")
  ```

  正常路径由 `record_scenario(..., cleanups=scenario)` 执行并把结果写进 `cleanup` 字段;
  用例抛异常时由 `scenario` fixture 的收尾执行,结果照样落进同一份文件(那次结论记 `invalid`)。
  收尾自己抛异常不会顶掉用例手上的原异常 —— 原异常是这条用例的结论,收尾失败是流程问题。
- 场景里的固定时长等待用 `reporting/scenario.py` 的 `settle(seconds, reason)`,别在用例里写 `time.sleep`:
  它把计划时长与量出来的实际时长一起落成场景记录里的一步。**只等固定时长,不是轮询到条件**
  (ADR-0005 拒绝的是后者),等待本身也不判通过或不通过。

写法与坑见 `docs/WRITING_TESTS.md`。

## 证据

默认落盘到 `evidence/<会话时间戳>/<设备id>.jsonl`,每行一条 `frame` / `reading` / `event` /
`write` / `bench_setup` / `link_lost`(周边设备不合作:收帧阶段断链、收尾没把台架恢复回初始设定、
或条款的台架前置没发下去,按 `operation` 分开写,见上)/ `evidence_gap`(见下)。`write` 记录带读数键、写下去的物理量、
回读值与 `verified`(回读是否与写入一致):
`{"kind": "write", "key": "有功计量模式", "value": 1601, "verified": true, "read_back": 1601.0}`。
`bench_setup` 是条款的台架前置下发了什么(写在被测设备那一份里,排在随后的 `reading` 之前)。
写失败(超时 / 被拒)不写这条 —— 它已经落在那条 `frame` 记录的 `error` 上。会话目录下另有 `round-status.json`:
这一轮有没有断链、涉及哪些设备、结论要不要复核,机器可读。
场景层另写一份 `scenario-<用例名>.json`:参与方与 role、引用的读数键、每步结果、场景结论
(`passed` / `failed` / `invalid`)、收尾结果(`cleanup` / `cleanup_failed`)与关联的 per-device 文件名
(见 ADR-0005)。`cleanup` 是一串 `{action, passed}`,失败的条目另带 `error` 与 `message`;
收尾失败会让结论变 `failed` 并在 `note` 里写明"流程问题,不是判据不合格"。
`settle` 那一步的 `detail` 带 `reason` / `target_s` / `actual_s`(`actual_s` 是量出来的,不是抄配置)。
两次运行不互相覆盖;失败的用例也留证据(报文在收发那一刻就写,用例中途崩了收尾还有一次兜底)。
每台设备一个长期打开的句柄,每条记录写完就 `flush`:记录格式与落盘时机不变(报文仍在收发那一刻进文件),
而 open/close 次数从「每条记录一次」降到「每台设备一次」—— 一轮 5940 条记录时这一项就是 8 秒。
句柄在组合根 `teardown()` 与 pytest `sessionfinish` 关闭;每条记录已提前 flush,所以进程被强杀也不丢已写的记录。
收不回应答也写一条:`receive` 抛异常时那条帧记录带 `error`(`timeout` / `closed` / `disconnect` / 异常类名)
与已等待时长;应答回来了、只是被帧校验拒掉的,则是没有 `error` 的那条 —— 两者在证据里一眼分得开。
`reading` 记录带 `mode`(`real` / `simulator`),一份证据自己就能说清结论是模拟器还是真机给的
(取组合根算好的实际模式,`--force-simulator` 下就是 `simulator`)。
`reading` 里的 `evaluation.breaches` 是结论的机器可读版:区间违约写 `limit` + `exceeded_by`;
等值违约(`equals`)没有边界,所以只写 `kind: "equals"` + `expected` + `value`,
`limit` / `exceeded_by` 不出现 —— 写成 `null` / `0.0` 会被读成“没越界”,与 `passed: false` 自相矛盾。
证据层目前只支持字节流规约:给非字节流设备(CAN / HTTP / SSH / MQTT)开记录会在装配期直接报错,
而不是等到第一次连接时爆成 AttributeError。
`--no-evidence` 关掉后,装配与行为与没有证据层时完全一致(断链归属仍按 role 走:
周边设备断链照样 skip 测试无效,只是没有链路记录、汇总小节与 round-status.json)。

证据只由 pytest 层写:用例身份靠 `@case` 串起来。直接用 Python 脚本调 driver/组合根跑一遍,
进程里没有装配好证据会话,`evidence/` 下什么也不会多出来 —— 想留证据就跑 pytest 用例。

并行(`-n 4`)时目录名带上 worker 名:`evidence/<会话时间戳>-gw0/`、`-gw1/` …
每个 worker 各开一个会话 —— 否则同一秒启动的几个 worker 会算到同一个目录名上,证据互相覆盖。
同一台设备的所有用例落在同一个 worker,所以一台设备的证据只在一个目录里非空;
其它 worker 目录下那份同名文件是空的(装配时占位,说明它也被装配过)。

## 一轮的失败归因

跑完一轮之后,一句话回答"这一轮到底哪些设备不合格、哪些是台架问题、断在哪一步":

```bash
python tools/attribute_round.py                              # 取 evidence/ 下最新的一轮
python tools/attribute_round.py evidence/20240101T093000      # 指定一轮
```

输出两份,同一份归因:终端分节输出(人读),以及写回同一个证据目录的 `attribution.json`
(字段与 `round-status.json` / `scenario-*.json` 同一风格)。四类分开列,一条不并:

| 分节 | 装什么 | 谁来消化 |
|---|---|---|
| 设备不合格 | 读数越过 `severity: reject` 的判据,或场景里有被测设备的一步不成立:设备 id、判据、实测值、超出多少、依据哪条用例(编号 + 条款) | 设备/型号 |
| 未落证据的失败 | pytest 报了失败、证据里却没有结论(用例在写证据之前就崩了):nodeid、用例编号、一行原因。既不算通过,也不许当设备不合格;这一栏非空时结论不许是 `clean` | 排障的人 |
| 测试无效 | 周边设备故障(`link_lost`:收帧阶段断链 / 台架复位失败 / 条款的台架前置没发下去,按 `operation` 分开写)、场景判 `invalid`、断在哪一步、涉及哪台周边设备 | 台架 |
| 平台错误 | 聚合根不变量被违反(`platform-errors.jsonl`):平台自身有 bug,与被测设备无关 | 平台 |
| 没有有效结论 | 被 `--select` / 适用矩阵筛掉、被能力或 `real_hardware` 跳过、收集到了但证据里没有读数、在范围内却没有读数 —— 一律不算通过 | 排计划的人 |

其中的「设备不合格」里的判据违约与证据层同构:区间违约写 `limit` + `exceeded_by`;
等值违约(`kind: "equals"`)没有边界,只写 `expected` + `value`,`limit` / `exceeded_by` 不出现 ——
写成 `null` / `0.0` 会被读成「没越界」,与「这条不合格」自相矛盾。

其中的「证据文件是空的」分三档,显示上分开:不在本轮范围的设备(没被任何用例点到)
只给一行计数(`另有 N 台设备不在本轮范围内`,名单在 `attribution.json`);用例点了它却根本没跑到
(`run-scope.json` 的 `executed` 里没有它的用例:被跳过 / `-x` 停下 / 会话被打断)同样只给一行计数,
一轮一条用例都没执行时写 `这一轮没有执行任何用例,N 台设备的读数无从判断`—— 规模下 264 行说的
其实是同一件事;设备在范围内、用例也执行了却没读数,才逐台列出(真该测没测时就靠这一档,
大规模下不能让它浸在范围外的噪声里)。
最后一档说得出口径:用例进了 `executed`、失败名单里也有它 → `它失败在写证据之前`(指回「未落证据的失败」);
否则就是 `选中的用例本来就不读数`(正常,不是问题)。两者下一步不同,不写同一句,
也不写"漏测"(`attribution.json` 里的 kind 是 `no_readings`,真跑过的 nodeid 在 `executed_nodeids`,
没跑到的设备名单在 `not_run_devices`)。执行条数未知(旧证据没有 `executed`)时退回中性说法,
不猜是哪一种。
设备范围从哪来:按设备参数化的用例在 `run-scope.json` 里带 `devices`,场景用例取
`scenario-*.json` 的 `participants`;两边都交不出设备时报告写"设备范围: 未知",仍逐台列出。

证据里丢掉的读数也要留下痕迹:取增量时若两轮之间记的读数超过 `history` 窗口(1000 条),
最旧的那几条已被裁掉、取不回来 —— 这时写一条 `evidence_gap`(`kind` / `missing` / `window` / `note`),
缺的条数明写:`{"kind": "evidence_gap", "missing": 5, "window": 1000, ...}`。
没有它,证据会看起来完整而实际少条,“失败必留证据”就无声地破了。没超出窗口的轮次不写这条
(否则每轮多一条噪声,真缺口反正浸在里面)。

证据里的时间戳一律带偏移:每条记录的 `at` 是 UTC(`2026-10-22T07:23:54.065+00:00`),
领域读数与事件的 `timestamp` / `occurred_at` 也带时区 —— 两台台架各跑一轮、把证据并到一起时
才排得出先后。排序用 `reporting.evidence.parse_at()`:旧证据(这条规则之前的目录)的 `at`
没有偏移,按本地时间理解 —— **已知限制**:这类旧记录只对写出它的那台机器所在时区准确。
改格式不改读法,否则跨台架的问题只是换成了跨版本的问题。会话目录名仍是不带偏移的时间戳,
跨时区机器共用同一个 `evidence/` 目录时“最新一轮”按目录名排会不准。

证据文件本身坏掉另算 `evidence_corrupt`(责任在写证据这一侧,既不算设备不合格也不算平台 bug)。
写证据时丢的读数也归这一栏(`evidence_gap`),而且报出缺了几条 —— 缺 5 条与缺 5000 条
不是同一件事。
每一条都带 `ref`,能直接指回具体的证据文件行(`inv_01.jsonl:14`)或 JSON 字段
(`scenario-x.json#steps[1]`、`run-scope.json#skipped[0]`),让人自己核对。

「被跳过」按原因聚合:同一原因只占一行 + `(×N)`,只展开前 3 个不同原因,
其余给一行计数 —— 规模清单下 100 条只差设备 id 的跳过逐条列会把真正的失败挤出屏幕。
完整名单一条不少地留在 `attribution.json` 的 `no_conclusion` 里。

"没跑"不是"通过":这一轮的范围记在 `evidence/<会话>/run-scope.json` 里
(收集了哪些用例、真正执行过哪些(`executed`)、每条用例落在哪些设备上、哪些被 `--select` /
适用矩阵筛掉、哪些被跳过以及为什么、哪些失败了(`failed` = call 阶段失败,`errored` = setup 阶段崩掉))。
`collected` / `deselected` / `skipped` / `executed` / `failed` / `errored`
是各不相同的事实,不互相推算 —— 少了这份文件时归因会明说"范围缺失",
不会把这一轮当成"只有这些用例"。
失败也要落进来:用例在写证据之前就崩了时证据里一条读数都没有,不记范围就等于从归因里消失。
它只记范围、不记判定 ——
判定仍然只有 pytest 报告和证据两处。

全绿一轮也必须说得清规模:输出会写"没有失败项"并给出"执行了 N 条(收集 C 条;其中跳过 S 条;还没跑到 X 条)",
不是把 0 条失败说成 0 条用例。这里的 N 只取自 `run-scope.json` 的 `executed`(真正执行过的用例):
`pytest -x` 在第一条失败后停下、或会话被 Ctrl-C 打断时,被收集却没执行的用例不算进去 ——
拿 `collected − skipped` 推算会正好把它们算成"跑过"。旧证据目录没有 `executed` 时,输出写
"执行条数未知",不拿任何减法冒充。`attribution.json` 那侧同样只有 `ran.executed` 这一个字段
说"跑了多少条"(未知时是 `null`),`ran.cases` 只表示证据里留下读数的条数。
退出码:`clean` 为 0,其余(`failed` / `invalid` / `platform-error` / `evidence-corrupt` /
`failed-without-evidence` / `incomplete`)为 1。
`failed-without-evidence` 就是上面「未落证据的失败」那一栏:它保证 pytest 红的时候归因不会说 `clean`,
`tests/test_attribution_meta_guard.py` 真跑一轮必失败的 pytest 来守这一条。

## 为什么 `tests_unit/` 可以完全不依赖真实设备或模拟器

`tests_unit/test_real_transports.py` 里的 `_FakeSocket` 是一个普通 Python 类,没有继承任何基类,
只实现被测代码真正调用的 `recv` / `sendall` / `settimeout` / `close` 就顶替了 socket。
这是"消费者定义接口"在 Python 里最直接的收益:验证收帧、超时、重连不需要真实链路,
