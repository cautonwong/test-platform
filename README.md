# testplatform —— 多协议/多设备自动化测试平台

被测对象是能源/配电领域的现场设备(电表、逆变器、电池、采集终端、电网模拟器……)。
术语见 `CONTEXT.md`,已经定过、不要再翻案的决策见 `docs/adr/`。

## 两个用法,先看你在哪一边

**① 写用例的人**(扩设备/协议/用例):照 `docs/how-to/` 抄。写的是 **Python**——驱动、编解码、用例、模拟器都是 `.py`。第一次写先看 `docs/how-to/write-your-first-case.md`。
**② 跑测试的人**(构建测试任务 → 跑 → 出报告):写一份 `tasks/<id>.yaml`,然后 `testplatform run <任务> --root . --evidence-dir reports/<任务>`,再看 `reports/<任务>/summary.json`,最后 `testplatform gate reports` 出 CI 判定。源码布局里 `testplatform run` / `testplatform gate` 与 `python tools/run_task.py` / `python tools/gate.py` 是同一个 main。

> 分发后的形态是 `npx @cautonwong/testplatform init|run|gate|scaffold`——**`npx` 只是外壳,写的仍然是 Python**。
> `pip install -e .` 之后 `testplatform run|gate|scaffold|new-case` 已经可用;`npx` 负责在项目里准备 Python 环境。

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

不跑用例,先清点项目里有什么:

```bash
testplatform cases --root . --tag 一致性          # 列出 @case:编号/条款/标签/文件行
testplatform cases --root . --json               # 同一份数据给 CI 或报告读
testplatform tasks --root . --kind acceptance    # 列出任务并逐个加载校验
```

`cases` 只读源码,不执行任何用例;`tasks` 会把每个任务真正加载一遍,缺字段、坏清单、未知规约当场报出来。
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
(目录名带 worker 名,见 [证据](docs/reference/evidence.md)),同一台设备的用例落在一个 worker 上,
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

型号、规约、设备类型、接入方式怎么加,见 [`docs/reference/extension-guide.md`](docs/reference/extension-guide.md)。

速记:同型号改 YAML;新规约加协议模块并在驱动里注册;新设备类型加聚合根和驱动;新接入方式加 transport 并注册。

## 写用例

第一次写:照 [`docs/how-to/write-your-first-case.md`](docs/how-to/write-your-first-case.md) 走一遍(10 分钟,从零到报告)。

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

每条用例自动落盘到 `evidence/<会话>/`;字段、失败与 `--no-evidence` 的行为见 [`docs/reference/evidence.md`](docs/reference/evidence.md)。

## 一轮的失败归因

`python tools/attribute_round.py` 把一轮证据归成四类,并写 `attribution.json`;判定口径见 [`docs/reference/attribution.md`](docs/reference/attribution.md)。

## 为什么 `tests_unit/` 可以完全不依赖真实设备或模拟器

`tests_unit/test_real_transports.py` 里的 `_FakeSocket` 是一个普通 Python 类,没有继承任何基类,
只实现被测代码真正调用的 `recv` / `sendall` / `settimeout` / `close` 就顶替了 socket。
这是"消费者定义接口"在 Python 里最直接的收益:验证收帧、超时、重连不需要真实链路,
