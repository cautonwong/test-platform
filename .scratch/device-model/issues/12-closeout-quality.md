# 12 收尾四件事:分组无守卫、ADR 偏薄、场景层空壳、票号撞车

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `tickets.md` 的票已全部勾完(a82af5d),但收尾质量有三处低于票面宣告的水准,外加一处编号冲突会让后面接手的人找不到东西。四件事都很小,合一张票做。

## 0. 并行分组只在 `--dist loadgroup` 下生效,降级无声(最严重)

分组靠给用例打 `xdist_group` 标记,而标记只有在 `--dist loadgroup` 下才被 pytest-xdist 采纳。写 `-n 4` 而漏掉 `--dist loadgroup` 时:**退出码 0、`181 passed`、零警告**,分组静默失效。

实测:

```
.venv/Scripts/python.exe -m pytest -q -n 4                     # 181 passed, 6 skipped,无任何提示
.venv/Scripts/python.exe -m pytest -q -n 4 --dist loadgroup    # 同样 181 passed,这次分组真的生效
```

模拟器下两条路都绿,所以现在看不出差别;一旦接真实台架,同一终端下的两块表会被派到两个 worker、往同一条 485 总线上交叉收发,结果是随机读写失败——**而这正是票 8 要防的那个 bug**。

期望:收集期就报错。有 item 带 `xdist_group` 标记、且 `numprocesses > 0`、且 `dist != "loadgroup"` 时直接 `pytest.UsageError` 退出,消息里给出正确命令。不要打 warning 了事——这仓库自己的规矩是“拼错就报,不静默”。裸跑(不带 `-n`)不受影响。

## 1. 拓扑分组逻辑仍然零自动守卫(继承自 issue 11,唯一未解决的一条)

`tests/conftest.py:159-181` 的 `_mark_topology_groups` / `_topology_group` 没有单测。默认套件里没有任何断言说“同一 `via:` 下的两条用例拿到同一个 `xdist_group` 标记,不同终端拿到不同标记”。`tests/parallel_topology_check.py` 是端到端证明,但它 spawn 子进程、文件名不带 `test_` 前缀、默认不收集。

后果:`_topology_group` 的推导改错(比如沿 `via:` 链没跟到根、或链中间层自成一组),默认套件照样 `181 passed`。

期望(按 issue 11 的原方案,不 spawn 子进程):用假 `Manifest` + 假 item 直接调用,秒级跑完:

- 多层 `via:` 链(终端 → 抄表器 → 表)全跟到根
- 无 `via:` 的设备自成一组
- 不同终端拿到不同组名

## 2. 四份 ADR 合计 27 行,够不上 ADR

`docs/adr/0001` 5 行、`0002` 4 行、`0003` 9 行、`0004` 9 行。只有结论,没有 Context、没有备选方案、没有 Status。ADR 的价值在于“三个月后有人想推翻它时,能看懂当初排除了什么”,现在做不到。

期望:每份补到能回答三件事 —— 当时的约束是什么、还考虑过哪些方案以及为什么否掉、这条决定在什么条件下该被重新审视。不用凑长度,`0002` 加两段就够。

## 3. `tests_scenario/` 是空壳,但仍在 `testpaths` 里

目录里只有 `README.md`,`pyproject.toml:22` 的 `testpaths` 仍然包含它。票 9 的验收项字面上就是“有目录与约定说明,没有多余抽象”,所以这不是返工 —— 但现状是收 0 条用例的一个空档,跑 `pytest tests_scenario` 会直接退出码 5(“no tests ran”),在 CI 里会被当成失败。

期望:二选一并在 README 里写明选了哪个 —— (a) 保持空目录,但从 `testpaths` 移除,等第一个场景用例再加回来;(b) 留一条 `@pytest.mark.skip` 的占位用例说明形状。**推荐 (a)**,空目录不产生噪声。

## 4. 票号撞车:issue `08` 和 票 `8` 是两件事

- `tickets.md` 的 **票 8** = 按总线拓扑并行(`c09f9bd`)
- `.scratch/device-model/issues/08-encoding-decode-contract.md` = 编码解码契约(已由 `2f3e1bd` 修完)

当时 issue 是插号进去的,后来票目又把“并行”编成了 8。现在:

- 我说“票 8 修完了”指哪个,没人分得清
- `issue 11` 里引用的“票 10”在 `tickets.md` 里已经不存在(并行票后来改成 8 了)
- `tests/parallel_topology_check.py:1` 自称 “ticket 8”—— 按现在的票目它反而是对的,但和 issue 08 撞号

期望:定一条规则并落到文件里 —— 建议 **issue 编号与票号共用一套,不再各自编号**;`tickets.md` 里为插号票保留字母后缀(如 `8a` 编码契约、`8b` 并行),或把两张 issue 改名成 `08a` / `08b`。同时在 `docs/agents/issue-tracker.md` 补一句“插号票怎么编号”。

## 验收

- [x] `-n 4` 不带 `--dist loadgroup` 时收集期直接报错退出(有单测或子进程断言锁住);裸跑与 `--dist loadgroup` 不受影响
- [x] 分组键与分组标记各有单测(假 Manifest / 假 item,不 spawn 子进程),故意改坏 `_topology_group` 会让默认套件变红
- [x] 四份 ADR 各自补上 Context / 备选方案 / 重新审视条件;`0002` 至少补两段
- [x] `tests_scenario` 的二选一已落地并在其 README 里写明;`pytest tests_scenario` 不再以退出码 5 收场
- [x] 票号规则落到 `docs/agents/issue-tracker.md`,`issue 08` 与 `票 8` 的指代不再冲突;`issue 11` 里失效的“票 10”引用被更正
- [x] 默认套件除新增用例外仍是 `181 passed, 6 skipped`

## Comments

- 2026-10-20 出票依据:对 `a82af5d` 做只读核查。第 1 条是 issue 11 里未被上游处理的部分(票面已同步、脚本编号已自洽,只剩守卫);第 2/3/4 条是这一轮新发现,`wc -l docs/adr/*.md` = 27 行、`ls tests_scenario/` = 仅 README 均为实测。
- issue 11 的两条已被 `a82af5d` 解决(票 8 四条已勾;脚本自称 ticket 8 在重编号后变成正确),残余部分已并入本票。
- 第 0 条是出票当天追加的:复验 `-n 4` 时发现漏掉 `--dist loadgroup` 完全无提示,两条命令的 passe 数量一模一样。这条比余下三条都重要——它是唯一能在真实台架上造成随机失败的。
- 12 已完成:1f2386e + e8670bd。漏写 `--dist loadgroup` 现在开跑前报错(exit 4),不再静默降级;四份 ADR 补到能自立;票号 8→8b 与 issue 08 解冲突。195 passed / 7 skipped。
- 2026-10-21 记账补齐(按 issue 16 复核)。六个验收框逐条对上实际状态,下面每条都是本轮实跑:
  - **框1 · 做了。** `python -m pytest -q -n 4` → exit 4,中文 `UsageError` 提示改用 `--dist loadgroup`(判定在 `pytest_configure`);裸跑 `266 passed, 6 skipped`;`-n 4 --dist loadgroup` → `266 passed, 6 skipped`,exit 0。
  - **框2 · 做了。** `tests/test_topology_grouping.py` 8 passed。把 `_topology_group` 末尾改成返回常量后 3 条立即红(`test_group_key_follows_the_via_chain_to_its_root`、`test_device_without_via_is_its_own_group`、`test_items_on_one_bus_share_a_group_and_another_terminal_gets_another`),改回即绿。
  - **框3 · 做了。** `wc -l docs/adr/*.md` = 31 / 32 / 34 / 34(合计 131,原 27);四份都含 `Considered Options` / `Consequences` / `重新审视的条件`,`0002` 的备选方案写了三段(自定义 DSL、配置化阈值、模板代写)。
  - **框4 · 做了,按 §3 的方案 (a)** —— `tests_scenario` 从 `pyproject.toml` 的 `testpaths` 移除,该目录 README 写明"只有约定、不是用例目录,第一个真实场景需求再加回来",并新增守卫 `tests_unit/test_scenario_layer.py`(目录里出现 `test_*.py` 而 `testpaths` 没有它 → 报错)。**需要注意的偏差**:显式跑 `pytest tests_scenario` 仍然是 exit 5(`no tests ran`)——这是"该目录确实没有用例"的正确信号,不是静默;默认 `pytest` 已不再包含它,票面担心的 CI 失败点消失。要让显式调用也 exit 0,只能改选方案 (b) 留一条 skip 占位,那等于把"没测"重新包装成一行绿色的 `s`,与本票判据相反,故没选。
  - **框5 · 做了(本轮补的)。** `docs/agents/issue-tracker.md` 新增 `Numbering and closing`:编号只增不改、插号用字母后缀、issue 与 tickets 是**两套序列**、`Status: resolved` 的定义;编号本身已在 `e8670bd` 落成 `issue 08` → `08a`、票 `8` → `8b`;`11-parallel-group-guard.md` 正文里 6 处失效的"票 10"→"票 8b"。
  - **框6 · 做了。** 默认套件 265 → 266 passed / 6 skipped(唯一新增是场景层守卫 1 条),skipped 数量未变、无回归。框面的 `181` 是出票时的基线,此后票 13–19 又加了用例,所以按"除新增用例外"的写法核对:没有任何既有用例变化。
- 本票 `Status: resolved` 是倒着补的(先改了状态、后补的框)。补上的关票规则写在 `docs/agents/issue-tracker.md`:**关票 = 每个验收框有勾或写明去处,不允许只改 Status**。
