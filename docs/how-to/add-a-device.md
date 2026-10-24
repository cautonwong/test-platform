# 加一台设备 / 一个型号 / 一个规约

分四档,**前两档一行 Python 都不用写**。先判断你在哪一档,别从最难的那档开始。

| 档 | 场景 | 改哪里 | 估时 |
|---|---|---|---|
| 1 | 已有型号,再加一台实例 | `devices:` 加一条 | 5 分钟 |
| 2 | 同规约的**新型号**(地址/倍率/编码不同) | `models:` 加一段点表 | 15 分钟 |
| 3 | 新**规约**(如现在的 vendor_ascii / dlt645 / modbus_rtu 都不适用) | 协议编解码 + 驱动 + 注册 | 半天 |
| 4 | 新**设备类型**(如充电桩、PCS) | 领域聚合根 + 驱动 + 判据集 | 1 天 |

## 档 1:加一台同型号设备

```yaml
# configs/devices.yaml
devices:
  - id: inv_04                 # 唯一,节点 id 里就是它
    model: inverter_pv_series  # 必须是 models: 里声明过的型号
    access: tcp                # 必须在该型号 protocol 允许的集合里
    mode: simulator            # 先模拟器跑通,再改 real
    name: "逆变器-产线A-04"
    rated_power_w: 5000        # 型号 device_params 声明了哪些,这里就只能写哪些
    connection:
      host: 192.168.1.53
      port: 502
      simulated_response: "4500.0 229.8 50.1"
```

设备实例的键是**闭合集合**:`id` / `model` / `role` / `access` / `mode` / `name` / `via` / `bus_address` / `connection` / `criteria_override`,加上型号 `device_params` 里声明的参数。拼错一个键(`rated_power` → `rated_powerw`)会被拒绝,不会静默取缺省。

加完之后,所有按类型展开的契约用例会自动多出 `[inv_04]` 一个节点 —— **不用改任何测试代码**。

## 档 2:同规约的型号

只加 `models:` 一段。判据集、能力、点表都在这儿声明一次:

```yaml
models:
  dtzy_new_three_phase:
    type: meter
    protocol: modbus_rtu
    criteria: meter_220v              # 引用 criteria_profiles 里的一条
    capabilities: [有功电量, 电压]      # 用例按能力跳过,不按型号分叉
    point_table:
      有功电量: {address: 0, length: 4, encoding: uint, scale: 0.01, unit: kWh}
      电压: {address: 2, length: 2, encoding: uint, scale: 0.1, unit: V}
```

点表项字段:`address`(Modbus 寄存器号 / DL-T645 数据标识 / 文本行字段序号)、`length`、`encoding`、`scale`、`byteorder`、`word_order`、`function`、`unit`。

| 字段 | 含义 | 约束 |
|---|---|---|
| `length` | 有效字节数(ascii 是**字符数**) | 正整数;`float32` 必须 4 |
| `byteorder` | **字内部**的字节顺序 | `big` / `little`;`ascii` 写了就报错 |
| `word_order` | **字之间**的顺序(32 位值跨两个寄存器时的四种组合) | `big` / `little`;`ascii` 写了报错;`length < 4` 写了报错 |
| `function` | Modbus 功能码,写语义名 | `read_holding_registers`(0x03,缺省)/ `read_input_registers`(0x04);只对 `modbus_rtu` 开放 |
| `scale` | 倍率 | 数字,缺省 1.0 |
| `unit` | 单位,只用于报告 | 字符串 |

**字节序与字序是两条独立的轴**,别混:`byteorder` 只管每个字内部的字节,`word_order` 管两个寄存器的先后。只设 `byteorder` 会读出一个数量级错的、看起来正常的假读数。

## 档 2b:协议固有信息进 `protocol_items`,点表只写型号差异

同一个 DI / OBIS 在不同型号上重复写 `length` / `encoding` / `scale`,是 200 个型号 × 200 个点 = 4 万块重复。这类**协议固有事实**写到清单第四段,点表用 `{item: <DI>}` 引用:

```yaml
protocol_items:
  dlt645:
    "00010000": {name: 正向有功总电能, length: 4, encoding: bcd, scale: 0.01, unit: kWh, byteorder: little}

models:
  dtzy_new:
    type: meter
    protocol: dlt645
    criteria: meter_220v
    capabilities: [有功电量]
    point_table:
      有功电量: {item: "00010000", scale: 0.01}   # 只写型号差异
```

两条规矩:引用必须落在目录里(不存在的 DI 装配前报错);**同一个字段不许有两处真相** —— `item` 与 `address` 并存、或把与目录逐字相同的字段再写一遍,都会被拒。

## 档 3:新规约

完整步骤(含 `read_raw` 骨架、错误类型、必备用例、五个真踩过的坑)见 [`add-a-protocol-and-driver.md`](add-a-protocol-and-driver.md)。概要:

1. `src/testplatform/protocol/<规约>.py` —— 帧的拼装、解析、校验,纯函数。长度要先读应答头才知道时,提供 `response_length(frame) -> int | None`。
2. `tests_unit/` 加字节向量用例。
3. `manifest.py` 的 `PROTOCOL_ACCESS` 加「这个规约允许哪些 access」。
4. `drivers/` 加驱动工厂并 `@register_device("<类型>", "<规约>")`,组合根加一行 import。
5. 清单里加型号 + 设备。

## 档 4:新设备类型

1. `src/testplatform/domain/<类型>.py` —— 聚合根。只保护**真正不该发生**的事(未连接就读写、SOC 越界这类),业务判据一律进判据集,不写进类里。
2. `drivers/<类型>_driver.py` —— 优先走 `PointTableDriver` 通用读取路径;只有需要时序或分支的设备才写专用驱动。
3. `@register_device("<类型>", "<规约>")` + 组合根 import。测试层的泛型 fixture 和参数化**自动出现**,不用改 `tests/conftest.py`。
4. 清单里加 `criteria_profiles` 一段 + 型号 + 设备;`test_matrix` 里给这个型号加 `tag:` 或编号。
5. `tests/test_<类型>.py` —— 照 `tests/test_inverter.py` 的结构写契约用例。

## 装配前会拦住你的(不用自己检查)

- 型号名不存在 → `KeyError` 并列出已声明的型号
- 规约 × 接入方式不配对 → 报出组合与允许集合
- 点表字段缺失/拼错、`float32` 长度不是 4、`ascii` 写了 `byteorder` → 点名型号与点表项
- 判据量写错、未知边界键(`gte:` 这种)→ 点名判据集第几条
- 设备实例多写/少写键 → 列出闭合集合
- `modbus_rtu` 缺 `bus_address`,`dlt645` 的 `bus_address` 不是 12 位 BCD
- `via:` 指向不存在的终端、`via:` 成环

## 验证

```bash
python -m pytest -q --select model:<新型号名>     # 先只跑这个型号
python -m pytest -q                              # 再全量
python tools/attribute_round.py                  # 看这一轮归因
```
