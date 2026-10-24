# 17 pytest 生态落地:装哪几个、怎么配、哪几个明确不装

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 我把一圈 pytest 插件装进 `.venv` 逐个实测过。下面四个值得进仓库(dev extra + 配置),四个明确不装并写清理由。全部数字都是在本仓库上跑出来的,不是看文档推的。

## 一、进仓库(dev extra)

### 1. `pytest-timeout` — 唯一有“事故预防”价值的

**为什么**:真机路径会挂死。`RealSerialTransport.receive` / `SshTransport.exec_command` / mock 子进程,任何一处卡住,现在没有任何兜底 —— 套件会一直挂着,CI 只能靠外部超时杀掉,还不告诉你卡在哪条用例。

**实测**:`pytest -q --timeout=20 --timeout-method=thread` → `199 passed, 7 skipped in 9.72s`,零影响。

**配法**:`pyproject.toml`
```toml
[tool.pytest.ini_options]
timeout = 60
timeout_method = "thread"
```
真机上按最长一次读数放大(SSH 网关单次 `exec_command` 默认 10s,`--fail slow` 用例会等 0.3s,60s 够)。注意 thread 模式杀不掉卡在 C 扩展里的调用,但至少能报出是哪条用例。

### 2. `pytest-repeat` — 把票 10 那条验收变成可跑的

**为什么**:票 10 的验收项是「连续多次全量跑,结果稳定」,现在靠人手动跑几次看。装上它就是一条命令。

**实测**:`pytest -q tests/test_mock_meter_end_to_end.py tests/test_terminal_topology.py --count=3` → `63 passed, 3 skipped in 18.43s`(三轮都过,含起子进程与真 socket 的那 6 条)。

**配法**:nightly 跑 `--count=3 -n 4 --dist loadgroup`,默认套件不变。这是“总线抢占型随机失败”唯一的自动化探针。

### 3. `pytest-randomly` — 但必须**默认不启用**

**为什么**:历史上真出过顺序依赖 bug(`composition_root` 是 session 级、`history` 跨用例累积,`len(history)==1` 才红)。那次是靠人发现的。

**实测**:三个随机种子各跑一次全量,均 `199 passed, 7 skipped`(9.68s / 9.96s / 9.59s),说明当前顺序无关性成立。

**关键警告**:这个插件**装上就默认打乱所有人的执行顺序**(它自带 autouse)。这会直接改变并行分组的实测行为,也让日志/证据的时间戳与顺序对不上。所以:

- 进 `dev` extra
- 需要时显式开:`pytest -p randomly`(或 `-p no:randomly` 关掉)
- 不要进 `all`

### 4. `pytest-cov` — 只对纯逻辑层设门槛

**实测**:`pytest -q --cov=testplatform --cov-report=term` → `TOTAL 1654 stmts / 215 missed / 87%`。

**建议门槛**:`domain/` 与 `protocol/` 是可以要求 95%+ 的纯逻辑(没有硬件、没有网络);`transport/`、`reporting/`、`composition/` 不要设门槛 —— 真机分支本来就跑不到,强行拉高只会逼出假用例。

**配法**:`--cov=testplatform.domain --cov=testplatform.protocol --cov-fail-under=95`,跑在单测子集(`tests_unit/`)上,不跟全量混。

### 5.(可选)`pytest-html` — 给人看的报告

**实测**:`pytest -q --html=$TEMP/report.html --self-contained-html` 生成成功。

**理由**:证据层产出的是 JSONL,给机器和排障用;交付/周报要的是人点得开的单文件 HTML。成本几乎为零。

## 二、明确不装

| 插件 | 结论 | 理由 |
|---|---|---|
| `pytest-rerunfailures` | **不要装** | 自动重跑正好掩盖“总线抢占导致的随机失败”——那是票 8/10 存在的理由。写进 README 当反面禁例 |
| `pytest-check` | 不装 | 软断言的价值是“一条用例报多个失败”。领域层的 `Evaluation.breaches` 已经一次收集全部越界,再包一层没有新信息 |
| `pytest-subtests` | 不装 | 同上;参数化已经把“多设备各判一次”做完了 |
| `pytest-mock` | 不装 | 仓库统一用 `monkeypatch`,换成 `mocker` 只是换个名字 |
| `pytest-regressions` | 观望 | 协议帧向量现在手写在 `tests_unit/test_protocol_codec.py` 里够用;等 DL/T645 命令字多到几十条再迁成 golden 数据文件,那时它能自动给出 diff |

## 三、落地动作

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-xdist>=3.8",
    "pytest-timeout>=2.4",
    "pytest-repeat>=0.9",
    "pytest-randomly>=5.0",
    "pytest-cov>=7.0",
    "pytest-html>=4.2",
]
```

`pyproject.toml` 的 `[tool.pytest.ini_options]` 只加 `timeout` / `timeout_method` 两项。`randomly` 不写进 `addopts`,要靠 `-p randomly` 显式开。

加一条 `docs/WRITING_TESTS.md` 的小节:「跑之前先想清楚你这轮在验什么」——单次全量 / `--count=3` 稳定性 / `-p randomly` 顺序无关 / `--cov` 纯逻辑门槛,四条命令各验一件事。

## 验收

- [x] `dev` extra 落地,`pip install -e .[dev]` 后 `pytest` 默认行为不变(顺序不打乱、不跑覆盖率)
- [x] `timeout = 60` 生效:人为把某条用例 sleep 到 61s,报错信息指出是哪条、不是整轮被杀
- [x] `--count=3` 的跑法写进 README / `docs/WRITING_TESTS.md`,并说明它对应票 10 的哪条验收
- [x] `-p randomly` 的跑法写进文档,并注明“不要默认启用”的原因
- [x] 覆盖率门槛只对 `domain/` + `protocol/`,跑在 `tests_unit/` 上
- [x] `pytest-rerunfailures` 以“禁止”形式写进文档(比不写更有用)

## Comments

- 2026-10-20 出票依据:把 `pytest-timeout` / `pytest-randomly` / `pytest-repeat` / `pytest-html` / `pytest-cov` / `pytest-check` / `pytest-mock` / `pytest-regressions` / `pytest-subtests` 装进 `.venv` 逐个实跑。上面的 passed/skipped 数量、耗时、覆盖率 87%、`--count=3` 三轮结果都是真跑出来的。**没有改 `pyproject.toml`**,所以这只是建议,不是已完成状态。
- 顺带说明:`pytest-cov` 那个 87% 里,`transport/real_transports.py` 的六个类是 0 —— 这正好印证 `14-real-hardware-transports.md` 第 6 条。
- 2026-10-21 落地 commit `fbb8645`:dev extra(timeout/repeat/randomly/cov/html/xdist)、
  `timeout = 60` + `timeout_method = "thread"`、`addopts = "-p no:randomly"`、
  `[tool.coverage.report] fail_under = 93`、README + `docs/WRITING_TESTS.md` 的四条命令与不装清单。
  默认套件 `pytest -q` = 273 passed / 6 skipped(数量、skip 数、耗时都不变);`-p randomly` 全量同样 273/6;
  nightly 形式 `--count=3 -n 4 --dist loadgroup` = 819 passed / 18 skipped。挂死兜底实测:60 秒后打栈并退出码 1。
- **与票的偏差(已确认)**:覆盖率门槛不是 `--cov-fail-under=95` 跑 `tests_unit/`。实测单测子集 85.77%、
  全量 94.35%,95 在两个条件下都到不了(domain 里 meter/terminal/grid_simulator 的一部分行只能经组合根装配才走到)。
  改为全量 + `fail_under = 93`:93 低于当前值留一分头寸,真掉下来会红,正常增量不会逼人写凑数测试。
- 票里的 `199 passed, 7 skipped` 是出票时的数量,本分支基线已是 273 passed / 6 skipped,上面数字按新基线。

- 2026-10-21 已完成 `fbb8645` / 合入后 273 passed / 6 skipped(默认跑不带覆盖率,耗时未增)。复核:覆盖率门槛确实会拦(临时抬到 99 → exit 1);`--timeout=3` 确实能报出卡住的用例并打栈;randomly 默认关、`-p randomly` 可显式开。
- 口径修正:票里写"domain/protocol 95% 跑在 tests_unit 上",实测单测子集 85.77%、全量 94.35% —— 子集低估(部分行只有经组合根装配才走到)。最终按全量 + `fail_under = 93`(不钉在当前值上,留一分头寸),且只在显式 `--cov` 时生效。
