# 55 `symbols.yaml` 的 3 条出处没进机械断言:唯一手工核对的提取产物

Status: resolved
Type: task
Blocked by: None — 小改动

**要修什么:** 44 立了"每个提取值都可被机器复核"这条口径 —— 产物每条带 `source: 路径:行号`,`tools/check_import_sources.py` 打开那一行断言关键字符串出现,改错一位必须红。53 新增的 `configs/import/symbols.yaml` **没走这条**:

- 53 的范围锁不允许动 `tools/check_import_sources.py`,所以这 3 条只做了**人工核对**
- 结果:同一个仓库里,`writes.yaml` / `bench_quantities.yaml` 的出处是机械校验的,`symbols.yaml` 的是人工的 —— **两类产物的可信度不一样,但文档里看不出来**

## 现状

```yaml
Un: {param: rated_voltage_v, unit: V, source: "tests/param.py:13", expect: "Un = 230"}
In: {param: rated_current_a, unit: A, source: "tests/param.py:15", expect: "In = 5"}
Hz: {param: rated_freq_hz,   unit: Hz, source: "tests/param.py:19", expect: "Hz = 50"}
```

`check_import_sources.py` 目前覆盖 79 条逐行断言 + 25 条反向断言,不含这 3 条。

## 期望

1. `check_import_sources.py` 覆盖 `symbols.yaml`:逐条打开 `source` 那行,断言 `expect` 里的字符串确实出现
2. **改错一位必须红**(行号 +1)、**改错符号名必须红**(`In` → `Ib` 这类上游没有的符号) —— 各贴一次真实失败
3. 上游目录缺失时仍然 skip 并说明(既有行为不变)
4. 报告里给出覆盖率数字:现在总共多少条机械断言(原 79 + 新增 N)

## 不做

- 不改 `symbols.yaml` 的内容(出处已人工核过,这票只补校验)
- 不动 `writes.yaml` / `bench_quantities.yaml` 的既有断言

## 验收

- [x] `symbols.yaml` 3 条纳入机械校验;未设 `UPSTREAM_DIR` 时 skip 不变
- [x] 行号改错一位 → 红(贴);符号名改成上游没有的 → 红(贴)
- [x] 断言总数从 79+N 可核对(贴)
- [x] 全量与规模清单不回归:**698 passed / 7 skipped · 规模 1802 passed / 109 skipped**,收集 705

## Comments

- 2026-10-22 出票依据:53 的交付报告自己标的遗留("`tools/check_import_sources.py` 不在允许改动清单里,所以 3 条出处只做了人工核对")。
- 与 44 的关系:44 那条验收写的是"每个提取值可被机器复核",本票是把它补全到最后一个产物上。
- 2026-10-23 完成 @ `c995ac2`:`tools/check_import_sources.py` 的 `collect()` 增加一段 —— `symbols.yaml` 顶层键即符号名,每条自己的 `source` + `expect` 进逐行断言。**逐行断言 111 → 114**(票里写的"原 79"是 43 出票时的数,44 加进 `writes.yaml` 的断言后已是 111;新增 3 条 = `Un` / `In` / `Hz`,反向断言仍 25)。
  - 两条变异都真红(改完已用 `git checkout` 还原,`git status` 干净):
    - 行号 `tests/param.py:19` → `:20`:exit 1,`- symbols.yaml Hz: tests/param.py:20 这一行不含 'Hz = 50'` / `实际是: global C`
    - 符号名 `In` → `Ib`(expect 同步成 `Ib = 5`):exit 1,`- symbols.yaml Ib: tests/param.py:15 这一行不含 'Ib = 5'` / `实际是: In = 5`
  - 未设 `UPSTREAM_DIR` 仍是 skip(退出码 0,打印"跳过:没给上游目录"那三行说明),`--upstream` 分支未动。
  - 不回归:默认 **698 passed / 7 skipped**、收集 **705**、规模清单(265 台)**1802 passed / 109 skipped** —— 与原基线逐字一致。差值为 0 的原因:只改了工具,没加用例(加用例会改集合数 705,与本票验收冲突;校验本身由上面两条变异当自检)。

- 2026-10-23 已完成 `c995ac2`(4 行)+ 我方补的守卫用例。断言 **111 → 114**(逐行)+ 25(反向)。
- 两处变异真红:行号 `19 → 20` → `tests/param.py:20 这一行不含 'Hz = 50'`;符号名 `In → Ib` → `tests/param.py:15 这一行不含 'Ib = 5'`。
- **worker 少做了一件,我补上了**:它为了不改动收集数(验收写的是 705)而**没加用例**,只做手工变异。那是被我的验收措辞逼出来的错误取舍 —— **数字变化可解释就允许**,少一条自动守卫不可解释。补了 `tests_unit/test_check_import_sources.py`:断言 `collect()` 里 `symbols.yaml` 的 Un/In/Hz 三条被收进来(纯函数,不读上游)。
- 补测试时踩到:按 `source` 前缀过滤会连带 `bench_quantities` / `wiring_modes` 引用同一上游文件的条目(20 条 ≠ 3),**必须按 label 前缀 `symbols.yaml ` 取**。
- 终态:**699 passed / 7 skipped · 规模 1802 passed / 109 skipped · 收集 706**(相比 698/705:+1 用例)。
