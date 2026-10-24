# 26 缺写原语:82% 的上游条款要"往被测设备里写",平台只能读

Status: resolved
Type: task
Blocked by: None

**What to build:** 驱动目前只有 `read_all()` / `read(key)`,整个平台对被测设备是**只读**的。而实测上游 659 条条款里 **542 条(82%)** 含 `universalSet` —— 进工厂模式、改密码、写计量模式、下发阈值。没有写原语,这 542 条一条都迁不过来。

## 现状(实测)

```
$ cd E:/workspace/cmake/power/autotest-10544
$ grep -l "universalSet" tests/test_M_*.py | wc -l
542          # 共 659 条
```

本项目的读写能力对照:

| 层 | 现在有什么 | 缺什么 |
|---|---|---|
| 点表 | `address/length/encoding/scale/byteorder/word_order/function/unit` | 没有 `writable` 标记 |
| protocol | `build_read_request` / `parse_read_response` / `response_length` | 没有 `build_write_request` / `parse_write_response` |
| driver | `PointTableDriver.read_all()` / `read(key)` | 没有 `write(key, value)` |
| 周边设备 | `GridSimulatorDriver.apply_grid()`(专用方法,写死的命令) | 不是通用路径 |
| 证据 | 记 `send` / `receive` 原始帧 | 写成功/失败没有专门记录 |

## 期望

**最小闭环,四步都通才算完:**

1. **点表项加 `writable: true`**(可选,缺省 false):只有显式声明可写的点才允许写。没声明就写 → 装配/调用期报错,不猜。
2. **protocol 加写帧**:`build_write_request(...)` + 校验应答(写应答通常回显地址+功能码+寄存器地址,不是数据)。Modbus 功能码 0x06 / 0x10 二选一按点表声明的 `function` 走,和读同理:**一个来源,不会写 0x06 读 0x03 对不上**。
3. **driver 加 `write(key, value)`**:编码(点表 `scale` 反算)→ 发帧 → 校验应答 → **读回一次确认**。写后回读是刻意的一步:设备"接受写入"和"值真的生效"是两件事。
4. **证据记录写动作**:`{"kind": "write", "key": ..., "value": ..., "verified": true/false}`。归因里要能分开"写失败"和"读到的值不对"。

## 不做

- 不做通用"任意寄存器写"接口 —— 只认点表里声明过的键,否则等于给用例发了一把能写任何地址的枪
- 不把写失败算成"设备不合格":写超时 → `TimeoutError`;写被拒(异常应答)→ 单独一类;**写成功但读回不一致 → 设备不合格**(这才是设备的问题)

## 验收

- [x] 点表不写 `writable` 的点调用 `write()` → 报错并列出可写键
- [x] 写一个点 → 帧内容正确(有字节向量用例)、应答校验通过、回读值与写入值一致
- [x] 设备回显异常应答 / 不应答 → 分别抛不同异常,归因分到不同栏
- [x] 证据里能看到 write 记录与回读的 reading
- [x] 用一台模拟器跑通"改参数 → 读回"闭环,并在 `configs/devices.yaml` 里至少有一个 `writable` 的点
- [x] 全量套件仍全绿

## Comments

- 2026-10-20 出票依据:上游 `grep -l universalSet` = 542/659;本项目 `point_table_driver.py` 只有 `read_all`/`read`,点表字段表里没有 `writable`;`protocol/modbus_rtu.py` 只有读帧。
- 为什么排第一:它是 82% 条款的前置条件。`27-cross-device-derived-quantities.md`(误差)与 `28-scenario-settle-and-wait.md`(走字时长)都只挡 4% 左右。
## 最小可验证切片(先做这个,半天)

整张票要动点表 schema / protocol / 驱动 / 证据四层,一上来全做会拖很久。切片的目标是**半天内拿到第一个"写"的真帧证据**,只做单寄存器写,不做批量写:

1. 点表项加 `writable: true`(缺省 false)。只为让它**能被写**;不做任何行为改动,先让清单能表达这件事。
2. `protocol/modbus_rtu.py` 加 `build_write_single_request(slave, address, value, function=0x06)` + `parse_write_response(frame, slave, address, function)`(写应答是回显地址 + 值)。
   配两条纯函数向量:`build_write_single_request(1, 0x0010, 0x1601)` 的 8 字节逐字节比对;回显应答解析通过 / 回显地址不符抛错。
3. `PointTableDriver.write(key, value)`:
   - 键不在点表 → `KeyError` 列出可用键
   - 点表项没 `writable` → `ValueError` 说明"这个点没声明可写"
   - 编码:`value / scale` 反算成寄存器整数 → 发帧 → 校验回显 → **立刻读回一次**并返回读到的值
4. 证据:`{"kind": "write", "key": ..., "value": ..., "verified": true/false, "read_back": ...}`。
5. 一条端到端(模拟从站):`write("有功计量模式", 1601)` → 回读 1601 且证据里有 write 记录。

**切片明确不做**:0x10 多寄存器写、批量写多个点、写失败的重试策略、`equals` 判据(那在 `27-derived-criteria-and-equals.md`)、场景层的"写完再判"编排。

**切片验收(5 条,半天内可全绿)**:
- [x] 未声明 `writable` 的点写 → 报错并列出可写键
- [x] 写帧逐字节正确(纯函数向量)
- [x] 回读值与写入值一致,返回值就是回读值
- [x] 证据里有 `kind: write` 且带 `verified`
- [x] 全量套件仍全绿(切片不改任何读路径)

切片合入后再开第二批:0x10 批量写 + 写后复位/收尾(上游靠"退出工厂模式"这类动作保证,场景层目前还没有 try/finally 收尾概念)。

- 2026-10-22 已完成 `eb56dc6` / 合入后 397 passed / 6 skipped。按"最小可验证切片"落地:点表 `writable`(非布尔值装配前拒绝)、`0x06` 单寄存器写(帧 `01 06 00 10 16 01 47 af`,CRC 已独立复核)、写后**立刻回读**并返回回读值、证据 `{"kind":"write","verified":…,"read_back":…}`。
- 三类失败分开:`TimeoutError`(不回话)/ `SlaveExceptionResponse`(设备拒写)/ 回读不一致(不抛,`verified: false`,由判据层判)。
- 未做且明确留给第二批:`0x10` 多寄存器写、批量写、写失败重试、写后复位/收尾。模拟从站也只实现 0x06。
- **已知措辞缺口**:模拟从站收到 `0x10` 时报的是"不认识的请求帧…收到 9 字节",读起来像长度问题而不是"0x10 暂未实现"。第二批实现 0x10 时会一并替换这条消息。
