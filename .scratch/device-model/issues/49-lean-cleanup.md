# 49 结构收敛:删死代码、两份从站加对照、拆两个超大文件

Status: resolved
Type: task
Blocked by: None — 三件事可分开提交,顺序如下

**What to build:** 体检里的"精简"部分。三件事互不依赖,建议按 A → B → C 三次提交,便于分开审。

## A. 删 `DeviceControlService`(约 85 行 + 其测试)

`application/device_control_service.py` 全仓 grep 只有它自己和 `tests_unit/test_device_control_service.py` 提到。组合根的重试是另写的(`root.py` 的 `connect_support` / `get_ready`)。**一条测试专测一段没人用的代码。**

删实现与测试;若将来需要编排,按那时的形状重写。

## B. 两份 Modbus 从站加对照断言

`transport/simulators.py`(390 行)与 `tools/mock_modbus_meter.py`(446 行)都实现 0x03/0x04/0x06。**已经漂移过一次**:规模清单 `simulated_registers` 少声明一个寄存器,100 条用例红,靠 `gen_scale_manifest.py:263` 的守卫才发现。

要求:一组用例对**同一请求**同时打到两份从站,断言**应答字节一致**;覆盖 0x03/0x04/0x06、异常应答(非法地址 0x02 / 非法值 0x03)、坏 CRC。
不做全量重构(两份继续存在);只把"不一致"变成红灯。

## C. 拆两个超大文件(可延后)

- `reporting/attribution.py` 1,239 行:同时做解析(`_read_device_file` 98 行、`_read_scope` 154 行)、分类(`attribute` 136 行)、渲染(`_render_no_conclusion` 74 行)。按三件事拆成 `parse / classify / render`,**判定只有一份**。
- `config/manifest.py` 1,340 行:`_validate_point_fields`(71 行)与 `_parse_devices`(71 行)是自然缝,按"校验"与"解析"分开。

拆的时候只移动代码,**不改行为**;diff 里应全是 rename/move。

**49-C 的触发条件(两个,任一成立就动手,不要提前):**

1. **出现第 4 类证据 `kind`** —— 这轮已经加过 3 类(`batch_fallback` / `manual` / `link_lost`),每加一类都要在 1,239 行里找位置
2. **「证据损坏」需要严重性刻度** —— 现在"1 条证据文件解析失败"和"丢了 5000 条读数"被归进同一个桶,读的人判断不了要不要停线;而这个分类的量级信息要动归因的顶层结构,**在 1,239 行里改最贵,拆完再改最便宜**

两条都不是"以后再说",是**具体的、会真的发生的事**;提前拆就是为想象的需求写代码。

## 验收

- [x] A:文件与测试都删掉;`grep -r DeviceControlService` 为空;全量绿
- [x] B:至少 6 条对照用例(3 个功能码 × 正常/异常);故意改坏其中一份从站的应答 → 必须红
- [x] C:`attribution.py` 与 `manifest.py` 各自按职责拆开,单文件 ≤ 500 行;判定逻辑仍只有一处;全量绿且用例数不变
- [x] 三件事分三次提交,每份 diff 都能单独审
- [x] 基线不回归(609 passed / 7 skipped);累计净减约 **170–370 行**

## Comments

- 2026-10-20 出票依据:ponytail-audit 体检项 6/7/8/9/10;
- C 是 nice-to-have,若 A、B 之后时间紧,可单独挂起,不影响其它票。
## D. 时间戳全是裸 `datetime.now()`(nice-to-have,和 A/B 一起做也行)

`domain/{battery,inverter,meter,terminal}.py` 与 `device.py` 共 **5 处**用无时区的 `datetime.now()`,证据层 `evidence.py:102` 的会话目录名同样是本地时间。

实测影响(具体场景):**两台台架各跑一轮,把证据并到一起时** —— `reading.timestamp` 与 JSONL 的 `at` 字段都是裸本地时间,没有时区偏移,合并后无法排序、也无法判断哪条更早。单机单轮时无害。

修法:`datetime.now(timezone.utc)` 或 `datetime.now().astimezone()`(带偏移的 aware datetime),序列化时用 `isoformat()` 保留偏移。会话目录名保持本地时间可读性即可,不必改。

**注意**:`_receive_frame` 里的超时判断用的是 `time.monotonic()`,那是对的,不要改成 wall clock。
两个需求不同:一个要"间隔准确"(单调钟),一个要"绝对时刻可比较"(带偏移的 wall clock)。别被"统一成 UTC"一起改掉。

**这是数据格式变更,不是内部实现变更** —— 旧证据的 `at` 是无时区本地时间,新的是带偏移。消费方(归因、人)会同时看到两种形态,所以二选一,不许含糊:

- (a) **推荐**:读的时候容错(无偏移按本地时间理解),写的时候一律带偏移 —— 新旧都能读,排序在"新证据之间"是正确的
- (b) 明确"旧证据不参与跨轮比较",并在证据格式说明里写一句

否则"合并两份证据排序错乱"会从**跨台架**问题变成**跨版本**问题 —— 同一个坑换个入口。

## 验收补一条

- [x] 读数与证据 `at` 字段带时区偏移(`...+08:00` 形式);现有用例断言时间戳的地方相应更新

- 2026-10-22 **A + B 完成**(`c0e36f6` + `38673d0` / 合入 `c90c246`);**C(拆两大文件)按计划挂起**,单开一轮:搬 1,239 + 1,340 行本身风险就高,单独一次提交、diff 全是 rename 才审得动。
- **闸门数字我的要求写错了**:A 明确要删 `tests_unit/test_device_control_service.py` 的 3 条,**"数字一条不变"在删测试的前提下不可能成立**。实际:默认 685→682、规模 1789→1786、收集 692→689,**差值正好 3**,skip 一条没动。worker 自己把算术摆出来而不是硬凑数字 —— 这是对的。
- **差分用例**:9 条 hex 字面量向量 + 坏 CRC,覆盖 0x03/0x04 读、0x06 写+回读、0x10 写+回读、0x02 非法地址、0x03 非法值、坏 CRC;**只起一个子进程**;断言逐字节。向量**没用 `build_*_request` 生成** —— 否则两边共用同一个编码器,差异永远测不出来。
- **改坏一份从站会红**(实测字节序反转:`进程内 01 03 06 00 01 e2 40 08 fc 2c 98` vs `独立 mock 01 03 06 01 00 40 e2 fc 08 d5 94`),已 revert。
