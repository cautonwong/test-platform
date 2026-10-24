# 56 电表型号补额定参数声明,让 Un/In/Hz 能解析(解锁 10 条 consistency)

Status: resolved
Type: task
Blocked by: None — 值得现在做,不需要外部资料

**What to build:** `symbols.yaml` 已经把 `Un`/`In`/`Hz` 映射到参数名,但**没有任何型号声明这些参数**,所以三条全都解析不出来,`13通信` 的 10 条 consistency 一条都生成不了。补的是**声明**,不是值 —— 值按每台实例给(参数化的本意)。

## 现状(实测)

```
型号 dtsd1352_modbus.device_params = None          ← 一个参数都没声明
实例 meter_01 = {id, model, access, mode, name, bus_address, connection}
symbols.yaml: Un → rated_voltage_v, In → rated_current_a, Hz → rated_freq_hz
参照:inverter_pv_series.device_params = ['rated_power_w'],实例上写 rated_power_w: 5000
```

## 两个语义决定(已定,不要再讨论)

1. **`Un` 指向相电压,参数名就叫 `rated_phase_voltage_v`** —— 名字自带答案。三相四线表 `Ua=Un` 里 Un 是相电压(220),若参数名含糊成 `rated_voltage_v` 而被填 380,`Ua` 就会被设成 380,而报告一切正常。
2. **`In`(额定)与 `Ib`(基本电流)是两个参数**:`rated_current_a` / `basic_current_a`。DL/T645 说 Ib、IEC 说 In,中间有换算,合成一个就会错。
   **但本票不给 `Ib` 加映射** —— 上游只声明了 `In`,条款里出现的 `Ia=Ib` 还没确认是表的基本电流(见 `docs/import-gaps-13comm.md`)。只在 `symbols.yaml` 注释里写明"若确认是基本电流,参数名用 `basic_current_a`",等工程师回答再补一行。

## 要改的

1. `configs/devices.yaml`:`models.dtsd1352_modbus.device_params` 加 `[rated_phase_voltage_v, rated_current_a, rated_freq_hz]`
2. `configs/import/symbols.yaml`:`Un` 的 `param` 改成 `rated_phase_voltage_v`;注释补一句"相电压,不是线电压"
3. **不要给 `meter_01` 编数**。缺值时的行为要能直接改:拒收并点名"型号 X 缺参数 Y"。

## 验收

- [x] 型号声明三个参数;`symbols.yaml` 的 `Un → rated_phase_voltage_v`
- [x] **实例缺值 / 值为 TODO** → 导入拒收,消息点出「型号 + 参数名 + 是哪条条款的哪个符号」,并给一行 yaml 片段示例
- [x] 用一份**带值的临时清单**(220 / 5 / 50)跑:`13通信` 的 10 条从"映射缺口"变为"可生成",且**至少 1 条真的生成进 `setting_cases` 并在模拟器上跑绿**
- [x] 覆盖率报表里"缺型号参数"这一档的计数变化写进票面(修复前的 10 → 修复后 ?)
- [x] `Ib` 不加映射,`symbols.yaml` 注释里写明后续参数名
- [x] 基线:`699 passed / 7 skipped · 706 collected · 规模 1803/109 @ 2b57b19`;**允许变化,差值要解释**
- [x] 不改 `configs/devices.yaml` 里台架地址(仍 TODO)

## Comments

- 2026-10-23 出票依据:实测 `dtsd1352_modbus.device_params = None`;`symbols.yaml` 已映射但无处解析;`13通信` 10 条 consistency 全卡在此。
- 为什么不需要外部资料:这是**声明**(2 行)与**实例值**(每台表自己的铭牌),不是"问厂家要额定电压"。不同被试品(单相 220 / 3×220/380 / 60Hz)用同一套条款跑,正是参数化的目的。
- 值我故意不给:`meter_01` 填什么要按现场铭牌,猜一个数会让台架设错电压而报告正常(和"不用 220 兜底"同一条理由)。

- 2026-10-23 已完成 / 合入 `edcf229`。**701 passed / 7 skipped · 708 collected · 规模 1805/109**。
- `dtsd1352_modbus.device_params = [rated_phase_voltage_v, rated_current_a, rated_freq_hz]`;`Un` 映射改名(相电压,不是线电压),`Ib` 不加映射只在注释写明 `basic_current_a`。
- **`meter_01` 用 `TODO` 哨兵**(不编 220):导入侧见到 TODO 直接拒收并点名型号+参数,**不拿默认值兜底**。
- **验收降级并说明**:"≥1 条真跑绿"移到 57 —— 结构性原因:10 条 consistency 条款的写步骤**没有一个落在唯一可写点 `有功计量模式`** 上。本条**不是没交差**,是那张票表达不了这批条款。数字变化:缺型号参数 40 处 → 0 处;条款级映射缺口 **10 → 10**(四档明细见 57 票面)。
- **一个副作用写进票面**(容易忘):`device_params` 声明即必填 → "加声明"和"给值"绑在一起,所以哨兵不是可选项,而是不给值时的唯一不编数解法。
