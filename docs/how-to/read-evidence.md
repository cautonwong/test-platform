# 跑完之后判定是谁的问题

先跑这一条,再决定要不要逐条翻证据:

```bash
python tools/attribute_round.py              # 省略参数 = evidence/ 下最新一轮
python tools/attribute_round.py evidence/20261021T152703
```

输出分四类 + 一档「没有有效结论」,外带这一轮的规模。另外会写一份机器可读的 `attribution.json`。

| 结论 | 含义 | 该找谁 |
|---|---|---|
| **设备不合格** | 读数越过判据集 | 设备/厂家 |
| **测试无效** | 台架问题:断链、周边设备没起来、总线被抢 | 台架、接线、周边设备 |
| **平台错误** | 聚合根不变量被违反(未连接就读、SOC 越界)—— 说明平台自身有 bug | 平台 |
| **证据损坏** | 证据文件写坏了、或写证据时丢了读数(`evidence_gap`:缺几条照报),责任在写证据这一侧,不算设备不合格 | 平台 |
| **没有有效结论** | 跳过 / 被筛掉 / 没跑到 / 证据判断不了 —— **都不算通过** | 看具体原因 |

## 证据目录长什么样

```text
evidence/<会话时间戳>[-gw0]/           # 并行时目录名带 worker
├── <设备id>.jsonl                    # 每台被测设备一份,按时间追加
├── run-scope.json                    # 这一轮的范围:收集/筛掉/跳过了哪些用例、落在哪些设备上
├── round-status.json                 # 这一轮有没有断链
├── scenario-<用例名>.json            # 每个场景一份:参与方、读数键、每步结果、场景结论
└── attribution.json                  # 归因结果(跑过 attribute_round.py 才有)
```

`<设备id>.jsonl` 每行一条记录,`kind` 区分:

| kind | 内容 |
|---|---|
| `frame` | 一次收发的原始报文 hex;失败时带 `error`(`timeout` / `closed` / `bad_crc`) |
| `reading` | 解码后的物理量、实际生效的判据集与边界、求值结论、`mode`(real / simulator)、领域事件、用例身份 |
| `event` | 这一轮只有事件没有读数(如设备进故障态) |
| `link_lost` | 收帧期间链路断了,以及归属判定 |
| `evidence_gap` | 写证据时丢了读数(两轮之间的增量超出 history 窗口):`missing` 是缺的条数 |

每条记录都带 `at`(UTC,带偏移)。旧证据的 `at` 没有偏移,排序时按本地时间理解
(`reporting.evidence.parse_at()`)。

## `task.json`:一轮任务的"出生证明"

每个任务目录(`reports/<任务>/` 或 `evidence/<时间戳>/`)有一份 `task.json`。**报告里的结论不够用时,先看它。**

| 字段 | 读法 |
|---|---|
| `kind` | `acceptance` / `smoke`。**`smoke` 不参与验收门禁** —— 它只证明"通路能走",不证明设备合格 |
| `launch` / `slave_ports` / `slave_addresses` | 这一轮真实用的命令行与端口。控件被替换过就看这里 |
| `port_source` / `port_range` / `port_attempts` | 产品端口从哪来(`platform_range` = 平台自选段,`injected` = 外部注入)、段值、**试了几次**(含成功那次)。`attempts=3` = 前两次没成 |
| `env` | **复现四件套**:`commit`(平台)、`platform_version`、`product_sha256`、`simulator_version`。没有它,同一份证据换了产品也看不出来 |
| `layout` | 六样资源各从 `package` 还是 `project` 来:**「装完能用」与「根里四样齐」全靠这一栏分**。`unknown` 只会出现在原始视图,`layout()` 不会交出它 |
| `process_events` | 进程侧发生了什么,按 `kind` 区分(见下) |
| `pytest_exit_code` | 子 pytest 的原始退出码,**仅供诊断**;结论以 `verdict` 为准 |

### `process_events` 的四种记录

```json
{"kind":"product_exit","how":"self_exit|platform_killed","code":0,"signal":null,"at":"…"}
{"kind":"port_retry","reason":"端口他人不可用|产品 stderr 自报 bind 失败","attempt":2,"port":30505,"at":"…"}
{"kind":"product_restart","…"}
{"kind":"unexecuted","count":47,"reason":"product_crashed|task_timeout|task_cancelled"}
```

- **`product_exit.how` 是关键**:`self_exit` = 产品自己退的(它自己的问题);`platform_killed` = 平台叫停的(超时/取消)。`unverified` 表示**平台没拿到死因** —— 那是平台侧缺证据,**不是**"产品没崩"
- **`port_retry.reason` 是闭集**,只有上面两个值。看到"因为端口能 bind 所以重试"这种反因果,就是 bug
- **`unexecuted` 必须与 `evidence_gaps` 分开读**:`unexecuted` 是"根本没跑",`evidence_gaps` 是"跑了但没留读数"

### `env.product_sha256` 的三态

| 值 | 含义 |
|---|---|
| 64 位十六进制 | 产品是文件/脚本,真算了摘要 |
| `"unknown"` | 产品是 PATH 上的命令(命令行里没有指向文件的参数)—— **平台不知道是哪一份** |
| `null` | **不该出现**:字段缺席与字段未知是两件事,`null` 会让复盘读成"平台没记录" |

## ## 常见判读陷阱

1. **`mode: simulator` 的证据不能当验收记录**。每条 `reading` 都写了 `mode`,先看这个再决定这份证据能不能对外。
2. **空的 `<设备id>.jsonl` 说明这一轮没碰过它**,不代表它合格。归因会把它归到「没有有效结论」。
3. **断链不等于设备不合格**。被测设备断链判不合格;周边设备断链判测试无效 —— 看 `link_lost` 记录里的归属。
4. **`-x` 或 Ctrl-C 中断的一轮,规模行要看清**。「跑了 N 条」在中断的跑法下可能不等于真执行条数(见 `issues/22`)。
5. **`--no-evidence` 下什么都没有**。场景用例在没开证据时会直接 skip,因为结论无处落盘。

## 一条命令定位一次失败

```bash
grep '"kind": "reading"' evidence/<会话>/<设备id>.jsonl | tail -1   # 最后一次读数和判据结论
grep '"error"' evidence/<会话>/<设备id>.jsonl                        # 收发失败的那几帧
grep -o '"error": "[a-z_]*"' evidence/<会话>/*.jsonl | sort | uniq -c # 失败种类分布
```

看完还判不了归属,就带着 `attribution.json` + 对应设备的 JSONL 去提 issue —— 别只贴一句"跑失败了"。
