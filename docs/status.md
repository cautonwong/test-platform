# 项目状态

**平台侧完成。唯一阻塞是外部资料 + 真机。**

- 代码:分层(transport / protocol / 点表 / drivers / domain)、清单三段式 + 派生判据、写原语(0x06 / 0x10 + 多字节编码)、证据(报文/读数/判据/事件/场景/轮次)、归因四类、台架复位、SSH 主机密钥、拓扑并行 —— 全部有守卫用例。
- 验收:默认 682 passed / 7 skipped;265 台规模清单 1786 passed / 109 skipped;串行/并行/随机顺序/用例级重复四种跑法一致。参见 `docs/baseline.md`。
- 挡住上真机的:台架 14 量寄存器定义、写后生效性、密级字段含义、`通信模式` DI —— 见 `docs/external-inputs.md`,每项都是"带假设的确认题",可直接转发。
- 明确不需要的:DL/T645 标准原文(我们用 DI,上游字典已有)、5 个可写参数的写入格式(上游 tuple 已给)。
- 待办:**54**(解析器丢步骤标题行 E 列,235 条条款被误报;修完预期 `13通信` 结构异常 8 → 2)。
- 挂起:49-C 拆两个大文件(只搬代码,需单独一轮)。

## 已接受的欠账(有取舍、有触发条件,不是忘了)

| 欠账 | 取舍理由 | 触发条件 / 去处 |
|---|---|---|
| `_free_port()` 分配后放掉再 bind 有窗口 | 产品不支持继承监听句柄 | **票 12**(先做"bind 失败重试 + 留痕") |
| `codec` 参数是鸭子类型(`Any`) | 只有两种帧格式、都有用例 | 出现第三种帧格式时抽 Protocol |
| DL/T645 与文本模拟器不支持数据注入 | 只对 Modbus 从站实现,声明如实为空 | 出现真实用例时按"表地址 + DI"落虚拟总线 |
| 模拟器故障是**静态**的 | 装配时定死,做不了"先正常三帧再超时" | 时序类用例需要时加运行中开关(`faults_are_static: False`) |
| 49-C 拆 `attribution.py` / `manifest.py` | 只搬代码,要单独一轮 | 第 4 类证据 kind 出现前,或「证据损坏」需要量级刻度时 |
| 58 DL/T645 String 写路径 | 无字节布局就无法证伪 | 拿到布局(外部输入第 6 项)或出现必须写 String 的条款 |

## 挂起(2026-10-23 决定:暂不做)

| 项 | 为什么挂起 | 什么时候捡起来 |
|---|---|---|
| 票 16 真 EdgeC 产品验收 | 产品在 Linux、当前拿不到;`requires:`/`unavailable` 具名状态在**还没有一条 Linux-only 任务时**属于为将来写代码 | 第一条 Linux-only 任务出现,或拿到产品时。届时先做 `unavailable` 状态再加任务 |
| 票 10 的 artifact 粒度 / 矩阵化 job | 两个任务时看不出差别 | 任务数 >10 |
| 09 的 `@edgec` org | 成员列表空,建了没人能装 | 有第二个使用者 |

**当前 CI 的验收 = `acceptance_sample`(示例产品)**,报告与任务头都写明"不是 EdgeC 产品" —— 这是目前的诚实状态,不是临时糊弄。

## 当前已知红(master)

| 时间 | commit | 现象 | 归属 |
|---|---|---|---|
| 已修(`0e2f047`) | `cde2f1f` | 3 failed / 937 passed:两条 `test_port_retry.py` + `test_task_root.py`,全是 `WinError 10013`(假产品 bind 平台给它的端口) | 票 14 正在修;根因是**测试夹具** `_reserved()` 与**平台 probe 不带 `SO_REUSEADDR`** 两处 |

**判据:14 合入后 `test_task_root.py` 两条也必须一起绿**;仍红 = 只修了半边,不许合。

## 已知偶发(master,2026-10-24)

| 现象 | 出现次数 | 已知信息 |
|---|---|---|
| 全量 1 failed,均在 `tests_platform/test_installed_layout.py` 家族 | 已捕获 3 个不同用例名；出现率约 1/6,单跑与该文件同跑多绿 | 单跑 `test_installed_layout.py::test_a_project_directory_without_cases_says_so_instead_of_falling_back` 通过;与该文件 + 10d e2e 一起跑也通过 → **顺序/资源相关** |

**处置**:由票 11b 捕获(跑 N 次,`-rf --tb=short` 落盘,记用例名与次数)。**在此之前的"全绿"结论都要看跑了几次。**

## 已修(clawpatch 批次,2026-10-24)

| 票 | 结论 |
|---|---|
| 13a / 13a2 | 事件没送到 sink → `EventEmitFailed` 带丢失条数;落 `evidence_gap{event_emit_failed, missing}`;round-status verdict 转 `needs-review` |
| 13b | `teardown` 清理进 `finally`;记录失败与清理失败各一条,原异常不被顶掉 |
| 13c | 装配期连 support 的上游 dut(`get_ready`)+ `SupportUpstreamNotReady`;ADR 0009 |
| 13d | 异常码判定下沉共用层;**原线索未复现**,真缺陷是判定写了两份 |
| 13e | `receive(None)` 循环读不截断;gap/timeout 三种结局可区分 |
| 12b | NaN / ±inf 读数不再判合格(复现成立,是真缺陷) |
| 14a | 单寄存器字节序:**误报**(帧上本来就对);留下双向 hex 守卫 |

**悬而未决**:`test_installed_layout.py` 家族约 1/6 偶发 `exit 2 invalid`(11e 已加失败时证据转储,根因未定)。
