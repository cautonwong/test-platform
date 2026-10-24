# 57 `13通信` 的 10 条 consistency 条款,写步骤不落在任何可写点上

Status: resolved
Type: task
Blocked by: 56(型号参数声明)先合入 —— 它把"缺型号参数"一档清掉,剩下的才是本票

**要修什么(56 实测得出的结构性结论):** `13通信` 里被判成 consistency 的 **10 条条款,没有一个写步骤落在 `dtsd1352_modbus` 唯一可写点 `有功计量模式` 上**。

`setting_cases` 只能表达"写某个可写点 → 读回判等"。所以即使把剩余四档缺口全补齐,**这 10 条也没有一条能真写出去**;硬把它们映射到 `有功计量模式` 就是编一个假映射 —— 那正是这个项目一路在防的。

## 剩余四档缺口(56 实测,开工时不必重跑)

| 档 | 处数 | 涉及的条款 | 要改哪里 |
|---|---|---|---|
| `bench_enum`:`接线方式="与表型一致"` | 10 处 / 10 条 | 全部 10 条 | `configs/import/params.yaml` 或 `wiring_modes.yaml`(上游原文带 ASCII 引号,取值表里没有带引号版本) |
| `bench_symbol`:`Ib` | 73 处 / 5 条 | 5 条 | 需工程师确认 `Ia=Ib` 是否基本电流(见 `docs/import-gaps-13comm.md`) |
| `param`:时钟 / APN参数 / 负荷曲线捕获对象 等 | 45 处 / 6 条 | 6 条 | `configs/import/params.yaml` |
| `expect`:`通信成功率:100%` 等 | 25 处 / 7 条 | 7 条 | `configs/import/expects.yaml` |

## 先纠正试点型号(leader-0 2026-10-23)

现在的试点用 `--model dtsd1352_modbus`(**Modbus**),但这批条款要写的量(`时钟`/`表地址`/`红外波特率`/`通信口速率`)**上游给的是 DL/T645 的 DI + 写入格式**(43/44 已拿到),不是 Modbus 寄存器地址。

| 型号 | 能不能现在补可写点 |
|---|---|
| `dtsd1352_modbus` | ❌ 要等厂家 Modbus 寄存器表(外部) |
| **`dlt645_meter`**(`devices_scale.yaml` 里有) | ✅ DI 与写入格式已到手,**现在就能补** |

**所以首选动作不是二选一,而是**:给 `dlt645_meter` 补可写点(时钟 `04000101`/`04000102`、表地址 `04000401`、红外波特率 `04000701/02`、通信口速率 `04000703-05`、有功计量模式),来源用 `configs/import/writes.yaml` 里已有的 DI + 类型 + 长度;再拿**同一批条款**跑试点。很可能就从"无可写点"变成"能生成"。
Modbus 型号那条明确写成"等厂家寄存器表",不要含糊过去。

## 两条路(必须先选一条,不要边做边改)

- **(a) 给电表型号补真实可写点**:补哪些点、地址与编码从哪来 —— **必须来自厂家寄存器表或实测**,不接受推测。补上之后 10 条里可能有一部分真能落下。
- **(b) 承认这批条款不能进 `setting_cases`**:按 ADR 0006 的分流规则,它们要么归场景层,要么标"平台不支持"。**"标不支持"也是合法结论**,比编一个映射强。

## 不做

- 不把写步骤硬映射到 `有功计量模式`
- 不猜地址/编码
- 不为这批条款改 `setting_cases` 的表达能力(加 bench/read 步骤是另一张票)

## 验收

- [x] 明确写下选了 (a) 还是 (b),以及依据 —— **(a)**,依据:这批条款要写的量上游给的是 DL/T 645 数据标识 + 写入格式(`configs/import/writes.yaml`),不是 Modbus 寄存器;试点型号因此换成 `dlt645_meter`
- [x] 若 (a):新可写点的地址/编码**有出处** —— 2 个点,DI/长度/类型/图例逐字来自 `writes.yaml`;`mode: real` 守卫没动(没撤)
- [x] 若 (b):不适用((a) 走了,但那 4 条只有人工步骤的条款按 (b) 的结论处理:归人工 / 场景层,不进 `setting_cases`)
- [x] `13通信` 的"完整可生成"数与试点条数真实反映结果 —— **仍 0 条**,不拿"缺口数下降"冒充
- [x] 全量与规模清单差值可解释 —— 默认 701→702 passed(新增 1 条守卫用例),规模清单不变

## 结论(2026-10-23)

**选了 (a),只走到"地址与编码都有出处的那一半"。试点仍然生成 0 条,而且就算四档缺口全补齐,
DL/T 645 现在也写不出去 —— 写路径不在代码里。下面每条都带出处,不留推测。**

### 1. 补进来的可写点(`configs/devices.yaml` 的 `dlt645_meter`,2 个)

| 读数键 | DI | 长度 | 类型 | 编码 | 出处 |
|---|---|---|---|---|---|
| `时钟日期` | `04000101` | 4 | `Date`(YYMMDDWW)| `bcd` + `byteorder: little` | `writes.yaml` → `fields.04000101`(`common/p645dict.py:4915`)+ 图例 `legend.Date`(`common/p645dict.py:5`)|
| `时钟时间` | `04000102` | 3 | `Time`(hhmmss)| `bcd` + `byteorder: little` | `writes.yaml` → `fields.04000102`(`common/p645dict.py:4916`)+ 图例 `legend.Time`(`common/p645dict.py:6`)|

`bcd` 不是猜的:图例的十进制位数 `digits` 正好 `= 2 × length`,字节对得上由
`tests_unit/test_p645_write_format.py` 原有的 Date/String 两条钉着;本次新增一条守卫
`test_可写点的地址与编码全部来自_writes_yaml`,把"每个可写点的 DI/长度/类型都要在
`writes.yaml` 查得到,且图例必须给出字节布局"变成可执行断言。

### 2. **没**补的:三类,各缺一样别的东西

| 量 | DI / 出处 | 为什么不补 |
|---|---|---|
| `表地址` | `04000401`,6 字节,`String`(`common/p645dict.py:4919`)| 上游图例只说 `String`、**没给每字节怎么编**(`legend.String.layout: null`);且写路径只支持数值编码(`protocol/values.py::ENCODERS` 没有 `string`/`ascii`)。要补先要:字节布局(手册)+ 文本写路径(代码)|
| `红外波特率` / `通信口速率` | `04000701`–`04000705`,1 字节,`String` | 同上(无布局),另外还缺**特征字取值表**(哪个数 = 哪个波特率),见 `writable_params.yaml` 的 `still_missing` |
| `有功计量模式` / `通信模式` | — | `writes.yaml` 里**没有**这两个量的 DI。不拿别的 DI 顶,不编 |

`dtsd1352_modbus` 那句照旧:**等厂家 Modbus 寄存器表**(外部输入),一个点也没补。

### 3. 试点真实输出(同一批 13通信 条款)

```
python tools/import_cases.py --sheet 13通信 --model dlt645_meter --pilot-limit 10
13通信: 条款 105 条 | 完整 0 | 部分 0 | 映射缺口 10 | 结构异常 7
试点 setting_cases 0 条(model dlt645_meter):没有一条完整条款,试点为空

pytest -q tests/test_setting_cases.py --device-config build/pilot_devices.yaml
5 passed, 1 skipped            # 全部是清单里原有的 dtsd1352_modbus 条款
--collect-only → test_setting_roundtrip 只有 5 个 dtsd1352 参数,dlt645_meter 0 个
```

顺带修掉一处会让"0 条"连跑都跑不了的东西:`tools/import_cases.py::render_pilot_manifest`
在试点 0 条时会写出 `型号: {}`,而清单装配拒收空映射 → `pytest` 直接 INTERNALERROR。
现在 0 条不建那个键,0 条这个结果才拿得到。

### 4. 还差哪几档(只数进 consistency 的这 10 条)

| 档 | 处数 / 条数 | 要改哪里 | 例 |
|---|---|---|---|
| `bench_enum`:`接线方式="与表型一致"` | **10 处 / 10 条** | `configs/import/params.yaml` 的 `接线方式.values`:上游原文带 **ASCII 双引号**,表里只有不带引号的写法 | 第 841 行步骤1 |
| `bench_model_param`:`Ua/Ub/Uc=Un`、`Fre=Hz` | **40 处 / 10 条** | `dlt645_meter` 的 `device_params` + 每台实例给铭牌值;**声明即必填**,只声明不给值整份清单装载就失败(所以本票没加) | 同上 |
| `bench_symbol`:`Ia=Ib` / `Ib=Ib` / `Ic=Ib` | **15 处 / 5 条**(整表 73 处 / 5 条)| `symbols.yaml` 加一行 `Ib` → `basic_current_a`,前提有人确认 `Ib` 是基本电流 | 4195、4235、4272、4629、4694 |
| `param`:对象参数名没落处 | **45 处 / 6 条** | `configs/import/params.yaml`;多数是报文原串与 `时间相关参数` 这类整段参数,不是单个可写点 | 1640、4213、4253 |
| `expect`:预期结果短语不可机读 | **25 处 / 7 条** | `configs/import/expects.yaml`;其中 `通信成功率：100%` / `合格：命令执行成功…` 不是等值判据,不能塞进 `equals` | 856、4299、4675 |

另外 4 条(`M_00_00_13_000101_18` / `000102_13` / `009001_01` / `009001_06`)**只有人工步骤**
(串口工具发报文、开关 COM 口、看通信成功率),没有一步是"写参数 → 读回判等" —— 它们不是
参数设置条款,补多少可写点都进不了 `setting_cases`。这 4 条按 (b) 处理:归场景层 / 人工。

### 5. 时钟为什么不进 `params.yaml`(补了点还不够的那一步)

1. 条款写 `时钟=2024-01-01 15:55:00` 是**一个名字**,点表里是**两个 DI**(日期 + 时间);
   一个 dut 条目只能指一个 key。
2. 就算挤成一个点(`0400010C` DateWeekTime 7 字节,同族),"写进去 → 读回判相等"对时钟
   **不成立**:写和读之间表自己走了几秒。上游那句话也是「读写一致为合格,**误差±5s**」——
   区间判据,不是等值。塞进 `equals` 只会得到一条真表永远绿不了 / 模拟器永远假绿的条款。

### 6. 就算四档全补齐,还是写不出去(代码侧,不在本票)

`src/testplatform/protocol/dlt645.py` 只有 `build_read_request` / `build_read_response`,
没有写帧(控制码 `0x08` + 密码 + 操作者代码);`drivers/meter_driver.py::_build_dlt645_meter`
也没注入 `write_raw`。所以 `PointTableDriver.write` 会抛"驱动没有写路径(没注入 write_raw)"。
**在那之前 `dlt645_meter` 的试点条款最多是"生成了但跑不成",不会有一条 PASSED** ——
下一张票:DL/T 645 写帧构造 + 驱动注入 `write_raw`(仍受 `mode: real` 守卫)。

## Comments

- 2026-10-23 出票依据:56 的实测 —— 参数补齐后"缺型号参数 40 处 → 0 处",但条款级映射缺口仍 10,且 10 条的写步骤都不在唯一可写点上。原挂在 56 的验收"≥1 条真跑绿"因此移到本票。
- 2026-10-23 结票(未达成"真跑绿"):试点型号改 `dlt645_meter`,补了 `时钟日期`/`时钟时间` 两个可写点(DI 与编码全部出自 `writes.yaml`);试点仍 **0 条生成**,因为 10 条里 6 条有写步骤、每条都还欠 `接线方式带引号 10 处 / Ib 15 处 / param 45 处 / expect 25 处`(型号参数那 40 处按"声明即必填"没动),另 4 条只有人工步骤。真跑绿的下一道门是 **DL/T 645 写路径**(代码,另开票),不是点表。

- 2026-10-23 完成 / 合入 `63d88b8`。**702 passed / 7 skipped · 709 collected · 规模 1806/109**(+1 用例)。
- 走 **(a)**:给 `dlt645_meter` 补 **2 个可写点**(`时钟日期 04000101` 4 字节 Date、`时钟时间 04000102` 3 字节 Time),编码 `bcd + little`,**DI/编码/长度全部出自 `writes.yaml` 与 `p645dict` 图例**,并加守卫用例把"`digits == 2×length`"变成可执行断言。
- **试点仍 0 条**,补可写点没让数字动 —— **这是结论不是失败**。`--model dlt645_meter` 前后都是 `完整 0 / 部分 0 / 缺口 10 / 结构异常 7`。
- **下一道门不是点表,是写路径**:`表地址 04000401` / `红外与通信口速率 04000701-705` 是 **String 类**,上游图例只给长度不给字节布局,而 `values.py` 的 `ENCODERS` 没有 string 写方向 → 另开 58。
- 剩余缺口分档(只数进 consistency 的 10 条):`bench_enum` 接线方式带引号 10 处/10 条、`bench_model_param`(`Un`/`Hz`)40 处/10 条、`bench_symbol` `Ib` 15 处/5 条、`param` 45 处/6 条、`expect` 25 处/7 条;另 4 条只有人工步骤。
- 顺手修掉一处让"0 条"连跑都跑不了的问题:0 条时不再写 `型号: {}`(空映射被装配拒收 → 之前是 INTERNALERROR)。
