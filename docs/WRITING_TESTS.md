# 用例编写指南

## 先分清楚你要写的是哪一层

| 目录 | 测什么 | 要不要设备 | 典型耗时 |
|---|---|---|---|
| `tests_unit/` | 聚合根不变量、判据求值、协议编解码、编排逻辑 | 不需要,不 import 任何具体实现 | 毫秒级,几百条几秒跑完 |
| `tests/` | 一类被测设备的通用规则,按类型展开,每台实例独立判定 | 需要(真实或模拟) | 秒级/分钟级,取决于协议延迟 |
| `tests_platform/` | **平台自身**的行为:归因、证据落盘、拓扑分组、断链归属、端到端链路 | 不碰真硬件,清单现造,多处起子进程真跑 | 秒级 |
| `tests_integration/` | 平台自身在多设备台架上的行为:装配顺序、资源独占、初始化互相干扰、收尾 | 需要 | 视场景而定 |
| `tests_scenario/` | 跨设备场景 | 需要 | 视场景而定 |

**判断标准**:这条规则是"这类设备的每一个实例都该遵守",还是"这几个具体设备凑在一起才有意义"?
前者是 `tests/` 里的契约用例(用泛型 fixture 自动遍历实例),后者是 `tests_integration/` 或场景层(显式点名设备)。

不测设备、测平台自己(`--select` 筛得对不对、证据里留了什么、断链算谁的)的用例放 `tests_platform/`:
它们跟着设备清单一起被 `--select model:` 筛、被算进一致性测试的统计口径都是错的。
`tests_integration/` 的定位与写法见 `tests_integration/README.md`。
如果这条规则不需要任何协议参与就能验证(比如"SOC 不能超过 100%"),直接下沉到 `tests_unit/`,
别浪费协议握手的时间。

## 一致性测试:新增设备不用写新用例

`tests/conftest.py` 末尾按 `drivers/registry.py` 里注册的设备类型生成泛型 fixture,
`pytest_generate_tests` 在收集阶段读设备清单,把用了 `inverter` 这类参数的测试函数
展开成"每台该类型、`role: dut` 的设备一个节点"。

```python
@case(id="M_00_00_01_000005_02", clause="GBT37408-5.2.1", tags=["一致性", "读数"])
@pytest.mark.contract
def test_inverter_reads_physical_value_by_reading_key(inverter):
    inverter.connect()

    power = inverter.read("有功功率")     # 读数键进,物理量出
    assert 0 <= power <= inverter.device.rated_power_w * 1.1
```

清单里有几台逆变器,这条用例就变成几个节点(`..._by_reading_key[inv_01]`、`[inv_03]`……),
报告里逐台分别显示通过/失败,互不影响。**新增一台设备、新增一个同规约型号,只改配置,不改测试代码。**

新增一种设备类型也不用改测试层:泛型 fixture 由注册表生成(见 `docs/adr/0001`)。
四条声明各管一件事:

| 标记 | 作用 |
|---|---|
| `@case(id=..., clause=..., tags=[...])` | 用例身份。证据里自动带上;`--select` 与清单里的 `test_matrix` 也读它 |
| `@pytest.mark.contract` | 契约用例,`python -m pytest -m contract` 只跑这一类 |
| `@pytest.mark.requires_capability("无功电量")` | 型号没声明这项能力就在收集阶段跳过,用例里不写 if |
| `@pytest.mark.real_hardware` | 加 `--force-simulator` 时全局跳过(见最后一节) |

## 编号是唯一身份:一个测试函数一个编号

`@case(id=...)` 不是标签,是归因与证据的唯一身份。同一个编号挂在两个**不同的测试函数**上时,
收集阶段直接报 `pytest.UsageError`(消息里给出两个 nodeid),不跑出两套同名但互相矛盾的证据。

参数化不产生新编号:一条用例按设备或按设定值展开成 N 个节点时,N 个节点共用一个 `@case` id ——
这是合法的(判重按测试函数归并,不按 nodeid),"哪一轮跑了哪个取值"由节点 id 表达。
因此 **id 里不要拼参数**(`M_00_00_01_000002_09_47.5Hz` 是错的),报告里分别是
`..._by_reading_key[inv_03]`、`...[47.5]`。

场景记录要跟着节点走:`record_scenario(..., name=request.node.name)`。
没参数化时 `request.node.name` 就等于 `originalname`;参数化时它带上参数 id
(`scenario-<用例名>[47.5].json`),4 个设定值落 4 份互不覆盖的记录。写成 `originalname`
会让它们往同一个文件名上覆盖,只剩最后一个取值 —— 归因里就只能说"这条用例出过问题",
说不出是 47.5 还是 51.5。

per-device JSONL 里的 `case.id` 仍然相同(它就是同一条用例):要分到具体哪个节点,看
`run-scope.json` 的 `collected[].nodeid`。

**唯一的例外是设置类条款**:一个函数跑 N 条条款时,条款本身就是身份,所以编号由参数给
(`@case_from_param("case_id")`):参数值就是清单 `setting_cases` 里的条款编号,
编号与 clause 都从清单取,收集期挂到每个节点上。证据里 N 条条款各有自己的编号 ——
这正是把 527 条条款收进一个函数的前提,写成 527 个函数就退化成上游那个形状了。


## 四种用例写法,分别用在什么场景

### 1. 契约测试(`@pytest.mark.contract` + 泛型 fixture)
"这一类设备的每一个实例都必须满足"。用 `python -m pytest -m contract` 可以单独只跑这一类用例。
读数一律按键取(`inverter.read("有功功率")`、`meter.read("电压")`),用哪个键由型号的点表决定。

### 2. 类级别不变量测试(直接 new 聚合根,不经过任何 fixture)
"这是业务规则本身,和具体是哪台设备无关"。不要用泛型 fixture 去遍历 N 台配置相同的模拟器
重复验证同一条规则 —— 那只是重复跑同一个逻辑,不会带来新信息。

```python
def test_inverter_rejects_read_before_connect():
    inv = Inverter(
        device_id="tmp", name="tmp", rated_power_w=5000, criteria=CriteriaProfile("tmp")
    )
    with pytest.raises(DeviceNotConnectedError):
        inv.record_reading(1000, 230, 50)
```

边界值建议用 `pytest.mark.parametrize` 批量列出,比复制粘贴同一个测试函数省事得多:

```python
@pytest.mark.parametrize("power, voltage, freq", [
    pytest.param(-100, 230, 50, id="功率为负"),
    pytest.param(3000, 230, 70, id="频率超出判据范围"),
])
def test_inverter_records_out_of_spec_readings(power, voltage, freq):
    inv = Inverter(
        device_id="tmp", name="tmp", rated_power_w=5000, criteria=_grid_50hz(5000)
    )
    inv.mark_connected()

    assert inv.record_reading(power, voltage, freq).passed is False
```

### 3. 特定实例/特定链路测试(具名 fixture)
"只对这一个具体设备或这一条接入路径有意义"。比如专门验证工控机网关这条链路本身能不能正常工作
(`inverter_02_via_ipc`)—— 这不是"逆变器"这个类型的通用规则,是"通过网关接入"这件事本身的规则。

### 4. 集成测试(`tests_integration/`,直接用 `composition_root.get(id)`)
"这几个设备放在一起跑会不会出问题":装配顺序、资源独占、初始化互相干扰、收尾。
目录里放什么、不放什么见 `tests_integration/README.md`;单台设备对不对**不**在这里测。

集成测试里**不要**让一个聚合根拿到另一个聚合根的内部引用去读写(比如不要在 `Inverter` 里塞一个
`Battery` 字段)—— 协调逻辑留在测试/应用层,两个聚合根互相之间永远不知道对方的存在。
跨设备的场景用例写进 `tests_scenario/`,那里要求显式列出参与的被测设备与周边设备,
并把场景结论落成 `scenario-<用例名>.json`(形状与不支持的东西见 ADR-0005,
现成示例见 `tests_scenario/test_grid_frequency.py`)。

### 5. 造清单:用 `tests_support/manifest_factory.py`,不手抄

用例需要一份**能装配**的清单时,不要手抄十几行:`manifest(inverters=..., meters=..., support=...)`
已把型号、点表、判据集、设备都安排好,`mutate` 只改你要验的那一处。

```python
from tests_support.manifest_factory import build, manifest

MANIFEST = manifest(inverters=2, meters=1)                   # 两种设备共用一份清单
root = build(tmp_path, manifest(inverters=1, support=True))  # 清单落盘 + 装配(用完 root.teardown())
```

**故意写坏的清单不走工厂**:那类用例测的就是校验分支,它要的是一份合法清单再掰坏一处。

### 6. 设置类条款(清单驱动,`setting_cases`)

“写 N 个参数 → 读回 → 判相等”,没有因果链的条款。上游 659 条里 527 条是这样(实测),
所以它们不应该写成测试函数:一条条款就是一个节点,代码只有一个
(`tests/test_setting_cases.py::test_setting_roundtrip`)。

```yaml
setting_cases:
  dtsd1352_modbus:                     # 型号:点表在哪,决定哪些点可写
    M_00_00_10_000008_02:
      clause: "DL/T 645 4.5.2"
      steps:
        - {write: 有功计量模式, value: "1600", expect: "1600"}
        - {write: 有功计量模式, value: "1601", expect: "1601"}
```

- 步骤词表只有 `write` / `value` / `expect`。**不支持条件、循环、引用前一步读数**:
  有分支的条款拆成两条,跨设备联动归场景层(`tests_scenario/`)。
- 相等口径就是判据的 `equals`(两边都能当数字按数值比,否则按文本比),
  所以 `expect: "1601"` 与读回的 `1601.0` 判等 —— 不为它另写一个比较器。
- 装配前校验(全在 `parse_manifest`):型号存在、条款编号全清单唯一、`write` 的键是该型号
  点表里 `writable: true` 的点、`value` 是数值(写原语只写单寄存器数值)、
  `expect` 是单个值(区间归判据集)。
- 新增一条条款 = 加几行 yaml。节点 id 就是条款编号
  (`test_setting_roundtrip[M_00_00_10_000008_02]`),`--select case:<编号>` 能只跑它。
- 这类用例不按设备参数化(设备由条款的型号决定),所以**不受 `test_matrix` 限制**,
  也不被 `--select model:` / `capability:` 筛中;按 `tag:一致性` / `tag:设置` 筛得到。
- 条款要落在哪个型号的哪几台 dut 实例上由清单决定:型号下没有 dut 实例时这条节点 skip 并说明。
- **台架前置写成 `bench:`**(ADR 0006),不进 `steps`:它是“让表能测”的前提,不是被测行为。

```yaml
    M_00_00_13_0002FF_61:
      clause: "DL/T 645 4.5.2"
      bench: {Ua: 220, Ub: 220, Uc: 220, Fre: 50}   # 量名过 configs/import/params.yaml
      steps:
        - {write: 有功计量模式, value: "1601", expect: "1601"}
```

  执行顺序:连接 → 台架前置(周边设备驱动下发)→ steps → 回读判定。前置没发下去是
  **测试无效**(`skip`);前置发下去了、读数不成立才是**设备不合格** ——
  证据里先有一条 `bench_setup` 再有 `reading`,两种现场分得开。
  被观测的台架动作(把台架调到 X → 读表 → 判 Y)不是前置,整条归场景层。

### 7. 场景里的固定时长等待(`settle`)

走字类条款的动作是"台架按设定输出 N 秒 → 读表 → 判增量",所以场景层有一个时间原语:

```python
steps = [grid_step, settle(60.0, "走字"), dut_step]   # 这 60 秒里台架保持设定,不动被测表
```

`settle(seconds, reason)` 真等满 `seconds` 秒,并落成场景记录里的一步:`detail` 带
`reason` / `target_s` / `actual_s`。`actual_s` 是 `monotonic` 前后量出来的,不是把配置值抄一遍 ——
这次被打断或被拖长,记录里对得上。这一步的 `device` 是 `null`、不进场景结论:等多久不构成
通过或不通过,它只让后面的读数有意义。**用例里不许自己写 `time.sleep`**,那样"等了多久"就进不了证据。

边界:**固定时长 ≠ 等待到条件。** `settle` 等的是条款规定的物理时长(台架输出 60 秒,
表就走 60 秒的字),机器快慢改变不了它;ADR-0005 拒绝的是轮询"直到某个条件成立",
那会让结论取决于机器速度、产出偶发红。不要给 `settle` 加条件参数、超时参数或时间容差断言 ——
那是重开 ADR-0005 那条不支持清单的事。超过 `SETTLE_LIMIT_S`(10 分钟)的时长会被拒
(防止谁写 `3600` 把 CI 卡住),真需要时传 `confirm_long=True`。

### 8. 跨设备派生量(误差):区间进清单,公式留在用例里

判据集的 `quantity` 是**单台设备**的读数键,表达不了"被测表减标准表"这种量。
这类量在清单里只声明参与方与区间:

```yaml
derived_criteria:
  有功误差:
    participants: [meter_01, reference_meter_01]   # 被测表 + 标准表
    limits: {min: -1.0, max: 1.0}                   # 单位 %,与上游 fPercent 同义
```

**公式不进 yaml。** `(被测增量 − 标准增量) / 标准增量` 是普通 Python,写在场景用例里;
组合根只拿清单里的区间判一个已经算好的值:

```python
error_pct = (delta_dut - delta_ref) / delta_ref * 100
steps.append(ScenarioStep(
    "meter_01", "有功误差在区间内",
    composition_root.judge_derived("有功误差", error_pct),
    {"derived": "有功误差", "limits": {"min": -1.0, "max": 1.0},
     "error_pct": error_pct, "delta_dut": delta_dut, "delta_ref": delta_ref},
))
```

- `detail` 要带判据名、区间与实测值:只留一个 `passed: true` 看不出判的是哪条、标准多少。
- 分子分母写反符号就翻,而区间往往不对称 —— 写法错了会表现成"设备超差"。
  用例里锁一条方向(`tests_scenario/test_derived_error.py` 的第三个用例)。
- 装配前校验:参与方必须是清单里的设备 id 且至少两台、判据名不能与参与方的读数键重名、
  `limits` 只收数字 `min` / `max`。没有 `derived_criteria` 段的清单行为不变。
- 为什么不在 yaml 里支持公式:那就是 ADR-0002 / 0005 拒绝的那门 DSL,
  真实台架迟早需要变量与重试,它会一路长成半门编程语言。

## 读数超出判据 = 不合格,不是异常

判据集的阈值写在清单里,`domain/` 里没有一个默认阈值。实测值超了照旧记录,
结论是不合格,并说清超了多少:

```python
inv = Inverter(device_id="t", name="t", rated_power_w=5000, criteria=grid_50hz(5000))
inv.mark_connected()

reading = inv.record_reading(3000, 230, 60)      # 不抛异常

assert reading.passed is False
assert reading.breaches[0].quantity == "grid_freq_hz"
assert reading.breaches[0].exceeded_by == pytest.approx(5.0)
```

`grid_50hz` 这类判据集在清单的 `criteria_profiles` 里声明一次,型号用 `criteria` 引用它;
个别设备的例外写在它自己的 `criteria_override` 上(60Hz 机型就是这样和 50Hz 机型共存于同一份清单)。
类级别用例不经过组合根,自己拼一份同形的 `CriteriaProfile` —— 下文片段里的 `grid_50hz(额定功率)`
就是这种小辅助函数,`tests/test_inverter.py` 里那份叫 `_grid_50hz`。

## 模拟器 / 真实硬件怎么选择性运行

```bash
python -m pytest                         # 按清单里每台设备自己的 mode 字段跑
python -m pytest --force-simulator       # 全部设备强制用模拟器,适合 CI
python -m pytest -m contract             # 只跑契约测试,跳过单测和集成测试
python -m pytest -m "not real_hardware"  # 跳过所有标了需要真实硬件的用例
python -m pytest tests/test_inverter.py::test_inverter_reads_physical_value_by_reading_key            # 只跑这一条(会展开成多个节点)
python -m pytest "tests/test_inverter.py::test_inverter_reads_physical_value_by_reading_key[inv_03]"  # 精确跑某一台
python -m pytest --select model:inverter_pv_series   # 只跑某型号的全部用例
python -m pytest --select capability:电压            # 只跑依赖某能力的用例
```

### 场景用例只按 `tag` / `case` 筛,`--select model:` 挑不出场景

场景用例(`tests_scenario/`)不按设备参数化:参与方是 `composition_root.get("inv_01")` 这样的
字面量,收集期拿不到"这条场景牵涉什么型号"。所以 `--select model:` / `capability:` 一条场景也
筛不到 —— 不是静默跳过,直接报错(退出码 4):

```
$ python -m pytest --select model:inverter_pv_series
ERROR: --select / 适用矩阵筛完没有任何用例可跑(条件之间是"且"):
  model:inverter_pv_series —— 清单里有这个型号的设备,但没有用例同时满足其它条件
```

想把场景单独跑一遍,用编号或标签(场景编号统一以 `S_` 开头):

```bash
python -m pytest --select case:S_                        # 全部场景
python -m pytest --select case:S_00_00_03                # 只跑编号前缀是走字误差那一组
python -m pytest --select tag:场景                        # 同上,按标签
python -m pytest tests_scenario/test_grid_frequency.py   # 按文件
```

按型号挑场景目前做不到:要做到得让场景在收集期声明参与方
(`@pytest.mark.participates("inv_01", …)`),`--select model:` 才有东西可匹配。等真有这个需求再做。

`@pytest.mark.real_hardware` 标记的用例(比如响应延迟这种模拟器下测了也没意义的指标):

- 加了 `--force-simulator` 时全局自动跳过(逻辑在 `testplatform_plugin.py`);
- 没加这个参数、但某台设备本身配的就是 `mode: simulator` 时,要在用例内部用
  `composition_root.mode_of(device_id)` 主动判断并 `pytest.skip()`
  (`test_inverter_responds_within_protocol_timeout` 是现成示例)——
  收集阶段 `composition_root` 还没被装配出来,那一步不知道每台设备的真实运行模式。

## "数据向下 / 事件向上":软信号该怎么报告

一次读数有三种结局,别混用:

| 情况 | 用什么表达 | 后果 |
|---|---|---|
| 设备不合格(频率 60Hz) | 判据集 `severity: reject` 的判据 | 读数照记,结论不合格,不抛异常 |
| 平台自身不该发生的状态(未连接就读数、SOC 越界) | `InvariantViolation` | 立即抛错 |
| 值得记录但不构成失败(功率逼近额定、SOC 偏低) | 判据集 `severity: warn` 的判据 → 领域事件 | 只记录与上报 |

聚合根只管生产事件,不知道谁会处理。软信号就是这样出来的:判据集里那条 `warn` 判据被越过时,
聚合根把这个事实记进自己的队列,由 `application/event_relay.py` 在每条用例结束后搬进 `event_sink`,
最终在 `pytest_terminal_summary` 里打印,同时由证据层写进同一份证据文件。

```python
def test_inverter_emits_high_power_warning_without_failing_the_call():
    inv = Inverter(device_id="t", name="t", rated_power_w=1000, criteria=grid_50hz(1000))
    inv.mark_connected()
    inv.record_reading(950, 230, 50)     # 越过 90% 额定的 warn 判据,不抛异常

    events = inv.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], HighPowerWarning)
```

**不要**为了"图省事"把不合格也写成异常:那会让一次只是超出阈值的读数变成 error,
丢掉"实测多少"这份证据,产线上制造大量误报。

## 一个容易踩的坑:不要在用例里绕过聚合根直接改状态

```python
# 不要这样写——绕开了聚合根自己的不变量校验
inverter.device._history.append(PowerReading(...))

# 应该这样写——通过公开方法,不变量在方法内部自动生效
inverter.read("有功功率")
```

如果发现"必须绕过聚合根才能把测试数据造出来",通常说明聚合根少了一个该有的公开方法,
应该去 `domain/` 里补这个方法,而不是在测试代码里破例。

## 新增一条测试项要动哪几处

0. 先看它是不是设置类条款(“写参数 → 读回 → 判相等”、无因果链):是就只改清单的
   `setting_cases` 段(加几行 yaml),不要新建测试函数。
1. 在对应的 `tests/test_<设备>.py` 里写函数(参考同文件里已有的契约用例)。
2. 打上 `@case(id=..., clause=..., tags=[...])`,需要能力/硬件时再加对应标记。
3. 该型号的 `test_matrix` 若按编号限制,把新编号加进去;按 `tag:` 限制时带上对应标签即可 ——
   收集阶段会检查"按设备参数化的用例是否被某个型号的矩阵覆盖",没覆盖会直接报错,不会静默少跑。
4. 用 `python -m pytest --select case:<新编号>` 确认只跑它。

## 跑之前先想清楚你这轮在验什么

四条命令,各验一件事。默认套件只跑第一条,其余三条是显式选择。

```bash
pip install -e .[dev]        # 一次装齐下面用到的 pytest 工具链

python -m pytest -q          # 1. 单次全量:这次改动有没有当场弄坏东西
python -m pytest -q --count=3                                                              # 2. 稳定性:同一批用例连跑三轮
python -m pytest -q -p randomly                                                            # 3. 顺序无关
python -m pytest -q --cov=testplatform.domain --cov=testplatform.protocol --cov-report=term-missing   # 4. 纯逻辑覆盖率门槛
```

nightly 用 `python -m pytest -q --count=3 -n 4 --dist loadgroup`:重复跑与拓扑分组一起上,
那是「总线抢占型随机失败」唯一的自动化探针。

### 1. 单次全量:默认套件

`python -m pytest -q`,行为和以前一样。两条例外:用例超时见下一节;装了 `pytest-randomly` 但默认被挡掉。

跑完想知道"这一轮到底哪些设备不合格、哪些是台架问题、断在哪一步",不要再翻 json:

```bash
python tools/attribute_round.py                 # 取 evidence/ 下最新的一轮
```

它把结论分成四类(设备不合格 / 测试无效 / 平台错误 / 没有有效结论),每一条都指回证据文件行,
并写一份 `evidence/<会话>/attribution.json` 给机器看。四类为什么不许合并、全绿一轮为什么还要报规模,
见 `README.md` 的「一轮的失败归因」。

### 2. `--count=3`:对应票 10 的「连续多次全量跑,结果稳定」

```bash
python -m pytest -q tests_platform/test_mock_meter_end_to_end.py tests/test_terminal_topology.py --count=3
# 23 passed × 3 轮 = 69 passed
```

那两条包含起子进程、真 socket、总线分组,是最容易出「跑一次绿、跑三次红」的地方。
随机失败不要重跑掩盖,要查。**不要装 `pytest-rerunfailures`**:自动重跑会把总线抢占型的随机失败削成绿,
而那类失败正是并行分组存在要暴露的东西。

### 3. `-p randomly`:顺序无关性(默认关)

`pyproject.toml` 里有一行 `addopts = "-p no:randomly"`,所以 `pytest-randomly` 装上也不会打乱默认执行顺序。
这样做是因为:它自带 autouse,装了就改写所有人的执行顺序 —— 日志与证据的时间戳、并行分组的实测,
都依赖「顺序是确定的」;历史上的顺序依赖 bug(`composition_root` 是 session 级、`history` 跳用例累积)
应该作为一个专门的检查去发现,不该变成每次跑都变化的默认行为。

```bash
python -m pytest -q -p randomly                 # 显式开:本次会话随机顺序
python -m pytest -q -p randomly --randomly-seed=12345   # 种子复现某一次的顺序
```

会话头会打印 `Using --randomly-seed=...`,拿到种子就能原样复现。命令行上的 `-p` 优先级高于 `addopts`,所以显式开是真的生效。
注意这个开关是和并行分组、证据落盘一起用时最值得看的组合 —— 默认套件里那些顺序无关性没被覆盖到的地方,
只在随机顺序下才露头。

**嵌套 pytest 不继承父进程的 ini**:用例里 spawn 子进程再跑 pytest 时(如 `tests_platform/test_attribution_*.py`),
子进程的 cwd 是 `tmp_path`,读不到本仓库 `pyproject.toml` 那行 `addopts = "-p no:randomly"`,
于是 pytest-randomly 在子进程里是开着的 —— 父进程的 `-p no:randomly` 保护不了它。
所有嵌套调用都用 `tests_support/nested_pytest.py` 的 `pytest_command(*args)` 拼命令行,由它显式带上这个开关。

### 4. `--cov`:只给 `domain/` 与 `protocol/` 设门槛

```bash
python -m pytest -q --cov=testplatform.domain --cov=testplatform.protocol --cov-report=term-missing
```

- 只量 `domain/` 与 `protocol/` —— 没有硬件、没有网络、可以要求高覆盖率的两层。
  `transport/` `reporting/` `composition/` **不设门槛**:真机分支本来就跑不到,强行拉高只会逼出假用例。
- 跑全量,不跑 `tests_unit/` 子集。单测子集只有 85.77%,不是代码没测,而是 `meter` / `terminal` /
  `grid_simulator` 的一部分行只能经组合根装配才走到 —— 拿子集当门槛等于在量一个不完整的系统。全量是 94.35%。
- 门槛 `fail_under = 93` 写在 `pyproject.toml` 的 `[tool.coverage.report]`,只在显式带 `--cov` 时生效;
  默认套件不跑覆盖率,不改耗时。

**为什么是 93 而不是 94**:94.35% 钉成 94,等于「下一个人加一个带两条难测分支的函数就红」——
那会逼人写凑数测试把数字补回来,正是这个项目一路在防的事。93 留一分头寸:真掉下来
(比如某个已测分支改成没测)会红,正常增量不会红。等 `domain/` 稳定、`dlt645` 的异常分支补齐后再上调。

### 用例超时:`timeout = 60` 已经在默认配置里

`pyproject.toml` 里 `timeout = 60` / `timeout_method = "thread"`,不需要任何命令行参数。
真机路径(`RealSerialTransport.receive`、`SshTransport.exec_command`、mock 子进程)卡死时,
现在会输出挂住那一刻的栈并退出码 1,不再让套件一直挂着等 CI 外部杀进程。

```bash
python -m pytest -q --timeout=5          # 临时收紧,压一个怀疑会挂的用例
timeout = 0                              # 或 -o timeout=0 关掉(调试断点时必须关)
```

输出长这样,带文件、行号、用例名:

```
+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++
~~~~~~~~~~~~~~~~~~~~~~~~~ Stack of MainThread (8156) ~~~~~~~~~~~~~~~~~~~~~~~~~~
...
  File "tests_unit\test_zz_hang_probe.py", line 5, in test_probe_uses_the_ini_timeout
    time.sleep(120)
+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++
```

为什么选 `thread`:`signal` 只有 Linux 有,`thread` 跨平台同一套行为,并且会打出**所有**线程的栈
(挂住往往发生在收发线程里,不是用例主线程)。代价说清楚:没有 SIGALRM 的平台(比如 Windows)
是 dump 完栈就 `os._exit(1)`,**没有 pytest 的失败行、也不会继续跑后面的用例**;能定位到哪一条用例,
但拿不到「这条失败、那几条通过」的逐条结论。要那种逐条结论就在 Linux 上用 `--timeout-method=signal`。
另外 thread 模式杀不掉卡在 C 扩展里的调用,它只能把现场打出来。

### 明确不装的几个,和理由

| 插件 | 结论 | 理由 |
|---|---|---|
| `pytest-rerunfailures` | **禁止** | 自动重跑掩盖总线抢占型随机失败 —— 那是票 8/10 要暴露的东西 |
| `pytest-check` | 不装 | 软断言要的是「一条用例报多个失败」,`Evaluation.breaches` 已经一次收齐全部越界,再包一层没新信息 |
| `pytest-subtests` | 不装 | 同上;参数化已经把「多设备各判一次」做完了 |
| `pytest-mock` | 不装 | 仓库统一用 `monkeypatch`,`mocker` 只是换个名字 |
| `pytest-regressions` | 观望 | 协议帧向量手写在 `tests_unit/test_protocol_codec.py` 里够用;等 DL/T645 命令字多到几十条再迁成 golden 文件,那时它能自动给出 diff |

`pytest-html`(`--html=报告.html --self-contained-html`)在 dev extra 里:证据层产出 JSONL 给机器,
交付/周报要的是人点得开的单文件 HTML。

## 规模清单:262 台设备的一份清单,和它量出来的五个数

契约测试对每台设备展开,所以"加设备"本身不需要写用例,需要的是**量一次**:
默认清单只有 6 台,规模这条维度从来没被碰过。规模清单是脚本生成的,不进默认套件:

```bash
python tools/gen_scale_manifest.py                      # 生成 configs/devices_scale.yaml(265 台)
python -m pytest -q --device-config configs/devices_scale.yaml   # 在这一份清单上跑整套用例
python tools/measure_scale.py                           # 五个数 + 两处瓶颈的微基准(约 5 分钟)
```

清单构成:200 台被测设备(4 个型号 × 50)+ 3 台采集终端各挂 20 块表(`via:` 拓扑,共享总线)
+ 2 台周边设备。型号与判据集从 `configs/devices.yaml` 读出来复用,只有 DL/T645 的电表与终端
型号是生成脚本里新加的。

它**不是**默认 `testpaths`,也不是默认 `--device-config`:不带参数跑 `pytest -q` 与以前完全一样。
规模测量要回答的是"按 200 台会先在哪里撑不住",不是"这份清单能不能跑绿";
`tools/measure_scale.py` 的注释里写了五个数各自的量法。

## 顺序随机化:抓"跨用例共享状态"的唯一系统性闸门

默认套件的 `addopts` 里有 `-p no:randomly`(顺序固定,便于看日志与证据时间戳)。但**顺序固定等于把"共享状态/顺序依赖"这一类缺陷的门常年关着** —— 这一路撞过三次同类:`history` 跨用例累积、共享 `grid_sim_01` 测合并语义、清单工厂复用模块常量。三次都是事后才发现,因为它们都不改用例数量、也不改单文件结果。

所以固定跑这一条(发布前或 nightly):

```bash
# 三要素缺一不可:顺序真的变了 + 能复现(seed) + 至少两轮(--count=2)
python -m pytest -p randomly --randomly-seed=12345
python -m pytest -p randomly --count=2 --randomly-seed=12345
```

**不要加 `-q`** —— 它会把 `Using --randomly-seed=…` 那行吞掉,而 seed 是这条闸门红了之后唯一能复现的东西。没有 seed,"随机顺序下红了"就是一句无法复查的报告,和"永远绿"是同一族的另一种死法(永远是"没法查")。红了之后把 seed 一起记进结论(例如"种子 12345 红了")。

**两条命令抓的不是同一件事,不能互相替代:**

| 旗标 | 抓什么 | 机制 |
|---|---|---|
| `--count=2` | 用例把状态存到用例之外(改模块常量、往 list 里累积) | 同一用例紧接着再跑一遍,第二遍看见第一遍的残留 |
| `-p randomly` | 用例之间的顺序依赖 | 每次会话重排顺序 |

所以"顺序一样"恰恰是 `--count=2` 需要的 —— 它要的就是同一条件下重复,变了反而测不出累积。

注意:开了 `--count=2` 之后节点 id 会变成 `[meter_01-2-2]` 这种形态,**`--lf` / `--last-failed` 的缓存对不上**。红了之后用 `--randomly-seed` + `--count=2` 重跑复现,别指望 `--lf`。

它抓不到功能错,**只抓这一类**:共享常量、累积状态、依赖执行先后。红了先怀疑**测试之间共享了可变状态**,而不是怀疑被测代码。

## 条款怎么写

- 条款在 `configs/clauses.yaml` 声明一次(`id → standard / number / summary`)。**`summary` 是本仓库的用法概括,不是标准原文标题** —— 原文未入库,报告与目录不得把它渲染成标准原文的样子。
- 用例里写条款 id:`@case(id="M_...", clause="DLT645-4.5.2")`。
- **裸自由文本 → 收集期报错**,消息里给出可用 id 与旧写法。
- 迁移期旧写法:`clause="free: <原文>"`。只允许出现在夹具 / 平台内部条目(现为 3 处);源文件漂移守卫会卡住新增的 `free:` 与裸字面量,要加必须改守卫 —— 那是有意的评审点。
- 按条款聚合:`testplatform.clauses.group_by_clause(cases)` → `{条款 id: (用例编号…)}`。报告(04)与目录(08)只调这一个入口。
- 校验入口:`--clause-config`(缺省用平台自带那份;覆盖时给路径)。
