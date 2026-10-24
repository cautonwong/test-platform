# 协作与审查协议(leader ↔ workers)

这份文件是**唯一的协作口径**。技能(`test-platform-verify-worker-output`)与各票面都指向这里,不要另立第二份。

## 1. 角色

| | leader-0 | worker-0 / worker-1 |
|---|---|---|
| 做 | 派活、审、拍板、改票面与基线、判合入 | 实现、commit、报交付 |
| 不做 | spawn / 建 worktree / 写业务代码 | 改共享文档 / 改基线 / 动别人的 worktree |

**共享文档只有 leader 改**:`docs/baseline.md`、`docs/**`、`.scratch/**`、`configs/devices.yaml`(出厂清单是唯一共享输入,示例清单另建 `devices_*.yaml`)。

## 2. 并发:两个 worker

1. **一人一个 worktree**(`E:/workspace/worktrees/<slug>`),一个目录只有一个写者。
2. **开工前 send 一行声明**:票号 + worktree + 分支 + 基于哪个 commit + 预计触碰路径 + 明确不碰什么。leader 据此判是否同文件重叠。
3. **同文件重叠就不并行**:让其中一个先做、另一个押后。已经撞过的三次:插件收集期钩子、`execution/task.py`、`configs/devices.yaml`。
4. **不删别人的 worktree**。删的条件三条全绿:代码已合入 master + 该 worker 真的停了(以 run 状态为准,不是它最后一次 send)+ 目录里没有别的 run。
5. **不在别人正在写的目录跑测试**;怀疑工作树状态被污染时用 `git archive <sha> | tar -x` 到临时目录复核。
6. **交付必须 commit**;合入由 leader 做。

### 语义冲突(不只文件冲突)

同一段代码的合法性可能取决于"哪张票先合入"。分两种处置:

- **新建**的代码依赖一张还没合入的票 → 找**两种时序下都成立**的形式(实例:新用例写 `free: <原文>`,条款清单票前后都合法)。
- **移动/改名**了已合入的路径,后来者适配 → **让后来的分支改**,不留 re-export shim(它是永久的东西,只服务瞬时需要)。

判据:这东西是"以后也一直在"还是"只是过渡"。

## 3. 派活模板(缺一项就打回)

票号 · worktree + 分支 + 基线 commit · 逐条验收 · **不许做什么** · 开工前当场量的基线(带 commit)· 交付格式

**范围要写成"要交代的单点",不是"允许区间"** —— 后者等于没定,worker 判断边界的默认答案是"再多改一点也没关系"。

## 4. 交付格式

```
commit sha
→ 逐条验收 + 实测输出(命令 + 尾巴)
→ 数字与基线比 + 差值解释(带量纲)
→ 三问自答:红了怎么复现 / 测试绕不绕得过去 / 以后退休会不会挡路
→ 已知没做的(不许藏进"已完成")
```

## 5. 审查六步(严格执行,第 2、3、5 步都不许省)

1. **确认合入**:`git log` / `git show --stat`。**测试数字不能证明 commit 身份** —— 同一个数字在别的提交上也成立。
2. **ponytail-review**:读 diff 与它触及的代码(调用方、被调函数、测试),按 正确/安全/规模/测试/性能/精简 出 Must / Should / Nice。
3. **ocr**:
   ```bash
   export PATH="/e/workspace/cmake/power/tools/Zephyr_Offline_Pack/ZephyrStudio/Git/cmd:$PATH"
   d:/env/ocr.exe delegate preview --from <base> --to <sha>
   d:/env/ocr.exe delegate rule <改动文件...>     # 11 组规则逐组复查
   ```
   LLM 版 `ocr review` 需 `OCR_LLM_URL/TOKEN/MODEL`,未配置时只用 delegate。
4. **逐条探针实测**(不读代码推断)。探针本身要先对契约:清单校验在 `CompositionRoot.build` 而非 `parse_manifest`;任务文件 `select` 是字符串;管道会吞退出码。
5. **架构合规**(六条,逐条跑命令,不看感觉):

   ```bash
   # ① 聚合根:domain/ 不认识任何上层
   grep -rn "^from testplatform\.\(execution\|catalog\|reporting\|drivers\|transport\|composition\|application\|cases\|clauses\)" src/testplatform/domain/ ; # 必须为 0

   # ② 组合根是唯一认识具体实现的地方(范围:src/)
   grep -rnE '^\s*(from|import)\s+testplatform\.(drivers\.(inverter|battery|meter|terminal|grid_simulator)_driver|transport\.(real_transports|simulators))' --include=*.py src/ | grep -v 'composition/' ; # 必须为空

   # ③ 不绕过组合根:装配只发生在组合根(排除 def 定义本身)
   #    窄例外:`transport/registry.py` 允许为"这一层自省"构造模拟器实例(问它能造哪些故障),
   #    那不是给设备接线。例外只允许在这一处,别的地方出现就是违规。
   grep -rn 'build_device(\|build_transport(' --include=*.py src/ | grep -v composition/ | grep -v 'def ' | grep -v 'transport/registry.py' ; # 必须为空

   # ④ 消费者定义接口:传输形状是 Protocol,不在 src/ 里 isinstance 具体实现
   grep -rnE 'isinstance\([^)]*(RealSerialTransport|RealTcpTransport|RealSshTransport|Simulated)' --include=*.py src/ ; # 必须为空

   # ⑤ 依赖由使用者分配(user alloc):__init__ 里不自己 new 基础设施协作者
   grep -rn -A6 'def __init__' --include=*.py src/ | grep -E 'self\._[a-z_]+ = (Evidence|RecordingTransport|RealSerial|RealTcp|RealSsh|Simulated)[A-Za-z]*\(' ; # 必须为空

   # ⑥ 有界上下文:顶层平铺文件必须在此白名单内
   ls src/testplatform/*.py | xargs -n1 basename   # 只允许白名单里的名字(见下)

**⑥ 白名单(2026-10-24 修订)**:`__init__.py` / `cases.py` / `clauses.py` / `cli.py` / `plugin.py` / `scaffold.py`

- `cli.py` / `scaffold.py`:`npx` 与 `testplatform` 的**投递壳**(09b);它们只拼参数、调平台入口,不含业务
- `plugin.py`:pytest 插件入口(09),`pytest11` entry point 指向它
- **新增顶层文件必须改这份白名单并写理由** —— 这条规则的本意是"包根不许变成垃圾场",不是"只能是这三个名字"
   ```

   判据:①②③④⑤ 全空,⑥ 不越白名单。越界就问一句:**这是一段该归到既有上下文里的代码,还是真的开了一个新上下文?** 后者要同时把它写进本文件第 7 节。

6. **四问**:① 红了怎么复现 ② 测试自己绕不绕得过去 ③ 退休时会不会挡路 ④ 数字/证据的来源能否唯一指认被测对象。

**收尾三步(缺一步等于没审)**:① 勾框 ② **写 `## Comments`(含实测输出)** ③ 刷基线。

> 判据:`Status: resolved` 的票 **必须**有 `## Comments`。审了不记录 = 没审 —— 下一次谁也说不清这张票验过什么。曾漏过四张(01c/02b/02c/02d),事后补录。

过 → 按上面三步收尾;不过 → findings 当"活"发回 worker。**leader 自己的失误也照发。**

## 6. 闸门口径

- 基线数字**开工前当场量**,每个数字带 commit。
- **允许变化,差值必须解释**;不要求"一条不变"。
- 判据:两个套件**同增** = 新用例不按设备展开;默认涨得少、规模涨得多 = 按设备展开的契约用例。
- 守卫必须**能失败**:故意改坏一处,对应用例必须红。

## 7. 架构约束(六个词,每条都可机械检查)

| 约束 | 含义 | 检查 |
|---|---|---|
| **聚合 / 聚合根** | 领域规则与不变量在聚合根内部,外部只能通过公开方法改状态 | `grep '^from testplatform\.\(execution\|catalog\|reporting\|drivers\|transport\|composition\|application\)' src/testplatform/domain/*.py` → **必须为 0** |
| **组合根** | 唯一认识具体实现的地方 | `grep 'drivers\.[a-z_]*_driver\|transport\.real_transports\|transport\.simulators' --include=*.py src/ \| grep -v composition/root.py` → **必须为空** |
| **消费者定义接口** | 消费者声明自己要的形状(`Protocol` / 传入的 callable),实现去满足它 | 传输层 `interfaces.py`、驱动的 `ReadRaw`/`WriteRaw`、`codec` 参数 |
| **user alloc** | 依赖由**使用者分配并注入**,被调用方不自己 new、不全局找 | 构造函数显式收依赖:`PointTableDriver(device, model, transport, read_raw)` |
| **有界上下文** | 一个上下文一个位置;上下文之间的依赖单向 | `src/testplatform/` 顶层只放跨上下文的公共物;任务执行在 `execution/`,条款在 `clauses.py`,报告在 `reporting/` |
| **不绕过组合根** | 装配只发生在组合根(或它所在进程) | `execution/` 不 import 任何 driver/transport |

**范围说明**:第二、三条的检查范围是 `src/`;**测试文件直接 import 具体实现是既有设计**(它们就是实现的单测),按全仓 grep 会把合法依赖判成违规。

## 8. 边界与例外

- 任务文件等同代码(能在跑测机器上执行任意命令):只跑评审过的任务,任务文件留在仓库内。
- `free: <原文>` 形式的条款只允许出现在 `tests_platform/`、`tests_unit/`、`tests_scenario/`(都是平台自己的用例),且必须登记在守卫白名单里(精确集合相等,任何新增都要改守卫 = 一次评审)。**业务一致性用例(`tests/`)一律不许**。
  为什么不给平台用例开一个条款 id:条款清单登记的是**标准条款**,塞进「平台:X」会让它变成杂物箱(和 `devices.yaml` 塞示例同一个病)。
- `shortcut:` 注释标明的限制是**决策不是缺陷**,除非负载已经越过它。

## 9. 当前票盘

见 `.scratch/edgec-e2e/tickets.md`(依赖表 + 派活状态)。基线见 `docs/baseline.md`。

**② 的旧正则有盲点(worker-1 2026-10-23 指出,已修):** 旧写法 `drivers\.[a-z_]*_driver` 只看得见 `from testplatform.drivers.inverter_driver import X`,看不见全仓实际在用的 `from testplatform.drivers import inverter_driver`。收紧后仍**必须排除 `interfaces` / `registry`**(那两个是允许的读:接口与注册表),否则会多出 10 条误报。当前在 master 上精确计数 = **0**。

## 7. 长跑验证:设超时 + 清残留

**规则(2026-10-23,一次 30 分钟 worker 超时事故后加):**

1. 任何跑全量的命令都加超时(`timeout 600 ...` / 工具的 timeout 参数),不许裸跑。
2. 每轮验证前先清残留进程与临时目录:`pkill -f pytest` / `pkill -f mock_modbus`,再看端口(`netstat -ano | grep 65000`)。
3. **"红"先怀疑残留,不是先怀疑代码** —— 上一轮没退干净的进程会占端口,让新一轮变成假红(实测:同两条用例,清残留前 2/6 红,清后 6/6 绿)。
4. 证据:`port_retry` / `platform_error` 里若出现端口撞号,先查是不是自己的上一轮。

## 8. 合入权

**只有 leader 合入 master。** worker 交付分支 + commit sha,不 merge(2026-10-23 worker-0 自行合入 11 后加的)。

理由:合入的人必须同时做六步审。worker 先合,leader 就只能在"已合入"的前提上补审 —— 顺序反了,万一红,红在 master 上。

## 9. 长任务分批

跑计数/长验证一律**每批 5 轮**,每批打印结果再开下一批。原因:worker 两次被 30 分钟超时杀掉,杀掉时未落盘的数据全丢。分批 = 每批都有可交付产物。

清残留顺序:`pkill` → 等 3 秒 → `rm`;`rm` 报 `Device or resource busy` 就**停下报告**,不许硬跑(有进程还开着证据文件,计数会是脏数字)。

### 「可配置项把保护关掉」的已知实例(2026-10-24,四次同型)

| 配置项 | 配成什么会让保护失效 | 现在怎么守 |
|---|---|---|
| `kind` | 缺省当成 `smoke` | 缺省 = `acceptance`,fail-closed |
| `expected_transaction` | 不填就跳过校验 | 必填(仅关键字) |
| `TESTPLATFORM_PORT_RANGE` | 配成与系统临时段重叠 | 加载期报错 |
| `read_gap_ms` | 配成 > `timeout`(gap 永不生效) | 加载期报错 |
| npm 打包 | 仓库 `.gitignore` 忽略 `npm/dist/`,npm 无 `.npmignore` 时**回退到 `.gitignore`** → 包里没有 wheel | `npm/.npmignore` + 断言「包里必须有 dist/*.whl」 |

**共同判据:有一个"合法输入"能让保护静默失效,就是缺陷。**

## 10. 改"分配/获取"逻辑时,搜索依赖它失败的代码

**事故形状(2026-10-23,票 14):** 端口从 `bind(0)` 改成"平台段顺序扫" —— 修好了"端口被抢",却放出一个新前提"端口总是拿得到"。而重试判据一直假设"截到第二个端口也可能被抢",靠这个**副作用**间接压住"产品崩了还重试"。新实现让 bind 永远成功,判据就漏了:产品崩 → 照样换端口重试。

**规则:** 改动任何"分配 / 获取 / 抢占"类逻辑(端口、临时目录、文件锁、连接池、名额)时,**grep 一遍"有没有人靠它失败过来判分支"**。靠副作用成立的分支,在新实现下会静默失效。

**同族:** 可配置项把保护关掉(§9);正则有盲点让绿来自没覆盖(§7)。

## 11. 一次验证只回答一个问题

**规则:** 修复/搬运/重构**不要**和"基线是否干净"的验证混在同一轮里。

- rebase 后先跑全量 → 绿 → **再**动新东西(插件搬迁、路径改造)
- 同一 commit 里不要放"必然红"的中间态(例如搬了模块却没改调用点)→ 对验证是"分不清是谁红",对 `git bisect` 是"必红版本 = 噪音"

**两个不同理由,同一个约束:**
1. 对验证:混两件事,红了不知道是解冲突错还是新功能错
2. 对历史:中间那版必然红,以后 bisect 到它就是浪费一轮

## 12. 有名字的未知:不许缺席,不许 null,不许默认值冒充

三种坏形态,各自的本质不同,修法也不同:

| 症状 | 本质 | 后果 |
|---|---|---|
| 字段缺席 / `null` | **记录不全** | 复盘时读成"平台没记录" |
| 自由文本 "不知道" | **无法机械判断** | 没人能对它写断言 |
| 默认值兜底(`project` / `acceptance` / `package`) | **把"没做"伪装成"做了"** | 假绿 |

**要的是第四种:有名字的未知。**

```json
{"product_sha256": "unknown"}     // 平台知道"自己不知道"
```

规则:
- 任何"来源可选"的证据字段,**禁 null**;能算给值,不能算给 `unknown`,**字段本身永不缺席**
- `unknown` 必须是**闭集里的一个值**,不是自由文本
- `unknown` 要能说清"什么时候会出现";说不出 → 它是装饰值,按分层契约处理(原始视图允许、可用视图禁止,如 `paths.sources()` 与 `layout()`)
- 读的一侧(`reporting`)对每个枚举值都要走通 —— **最容易崩的不是写,是把 `"unknown"` 丢进 `hexdigest` 比较或拼进 `sha256:` 字符串**

## 13. 共享 `.venv` 不许 `pip install -e`

**事故(2026-10-24):** 有人往共享的 `E:/workspace/test_platform/.venv` 里 `pip install -e` 了某个 worktree 的可编辑安装,留下 `__editable__.testplatform-0.1.0.pth` 指向另一个 worktree。后果:所有嵌套 pytest 同时看到"已安装的插件"与显式 `pytest_plugins`,报 `Plugin already registered under a different name` —— **11 failed / 12 errors,而根因不在任何一票的代码里**。

**规则:**
1. 共享 `.venv` 只装**平台依赖**(pytest / pyyaml / xdist),**永不 `pip install -e`** 平台自己
2. 需要"安装形态"的验证 → **一次性 venv**(`python -m venv /tmp/vXXX`),验证完丢弃
3. 另:`python -m build` 会留下 `build/` 与 `src/*.egg-info`,让平台变成"看起来已安装" → **打完必须清**,并有一条断言钉住(`npm/cli/prepack.js` 已这么处理)

**症状识别**:出现 `Plugin already registered` 且你并没有同时传 `-p` 和 entry point → 先查共享 venv 里有没有 `__editable__*` 指向别的 worktree。

## 14. 报错三件套:事实 + 判据 + 下一步动作

一条好的错误消息必须能回答三个问题:

```
事实:发生了什么(已读 17 字节 / 连试 3 个端口都不可用 / 缺 tests_support/fake_product.py)
判据:凭什么这么判(read_gap_ms=50 内无新数据 / 端口段 20000-31999 全被占 / 包内 _data 里没有)
动作:我该做什么(调大 read_gap_ms / 重装平台包 / 在项目目录放一份同名文件)
```

**只有事实 = 描述;有了动作才是可执行的报错。**

已落地的例子:`包内缺 tests_support/fake_product.py(平台包装坏了,重装平台包)`、
`连试 3 个端口都不可用(61879,61886,61895)… 重跑一次或换一台机器`。

**反例(要避免)**:`IncompleteFrameError`、`TaskRunError`、`FileNotFoundError` 裸抛 ——
它们把责任指向"对端/环境",而真正的问题常常在**参数或包装**。

## 15. 写入类改动:必须断言**发出去的字节**,不许只断言"读回来对"

**由来(2026-10-24,票 14a)**:一个"写路径忽略 byteorder"的线索,复现时发现**帧上本来就是对的** —— 因为 `driver` 的 `int.from_bytes(regs,"big")` 与 `protocol` 的 `to_bytes(2,"big")` 互为逆运算,读错写成错、读对写成对,**恒等往返**。

于是"写 `0x1234` 再读回 `0x1234`" 这类断言**永远绿**,哪怕编码真的写错了。

**规则:**

```
写入路径(寄存器 / 命令 / 报文)的改动,断言实际发出的 hex:
  little → 01 06 00 03 34 12 ef 07
  big    → 01 06 00 03 12 34 74 bd
两种字节序各一条,双向钉住
```

**并且必须有变异证据**:把编码写死 `big` → little 那条红、big 那条绿。
缺变异证据的守卫,和注释等价。

**推论**:任何"输入 → 编码 → 解码 → 输出"的闭环断言,都可能因为两端同错而永远绿。
**闭环不能证明中间正确,只有中间那层的字节能。**
