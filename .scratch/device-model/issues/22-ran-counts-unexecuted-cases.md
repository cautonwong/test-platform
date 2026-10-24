# 22 「这一轮跑了 N 条」在中断时会把没执行的用例算进去

Status: resolved
Type: task
Blocked by: None — 小改动,但属"断言超出证据"同一family

**要修什么:** `tools/attribute_round.py` 的规模行说「这一轮跑了 N 条」,而 N 实际是 `run-scope.json` 里的 `collected − skipped`,**不是执行过的条数**。`pytest -x` 在第一条失败后停下、或会话被 Ctrl-C 打断时,剩下被收集的用例根本没跑,却仍被算进"跑了 N 条"。

## 复现

```bash
.venv/Scripts/python.exe -m pytest -q -x tests/test_link_lost.py   # 或任何中途停下的跑法
.venv/Scripts/python.exe tools/attribute_round.py
```

规模行显示的条数 = 收集数 − 跳过数,而不是真正执行过的条数。

## 为什么值得修

同一个输出里已经有一行更诚实的口径(「证据里留下读数的: N 条」)。两行口径不一致时,读者会信更醒目的那一行 —— 而它是错的。这个仓库的一贯标准是:**报告只断言证据支持的东西**。

## 建议做法

1. `run-scope.json` 增加 `executed`(nodeid 列表),在收集期之后用 `pytest_runtest_logreport` 按 `when == "call"` 或 `setup` 结果记录;仍然**只记范围,不记判定**。
2. 规模行的"N 条"改用 `len(executed)`;另有 `collected` / `deselected` / `skipped` 三个数字保留,让读者能看出"收集了多少、真正跑了多少"。
3. `executed` 缺失(旧证据目录 / 写文件失败)时,明确标注"执行条数未知",**不要退回用 `collected − skipped` 冒充**。

## 验收

- [ ] `-x` 中途停下的一轮,规模行显示的是真实执行条数,不等于收集数
- [ ] 正常跑完的一轮,`executed` 条数 = 实际执行条数
- [ ] 旧证据目录(无 `executed`)时显示"执行条数未知",不冒充
- [ ] 现有归因用例相应更新,其余不变;全量套件全绿

## Comments

- 2026-10-21 出票依据:`issue/21` 修复时的自查报告(worker 主动指出 "跑了" 的口径其实是 `collected − skipped`,当时判为范围外未改)。我确认属实,单独出票。
- 与 `issue/21` 同族:都是"报告说了证据支持不了的话"。`21` 修的是设备维度,这条修的是条数维度。
- 2026-10-21 已实现(`6ed0765`)。`run-scope.json` 新增 `executed`:收集期之后按 pytest 报告记——进了 `call` 并跑出结论(通过 / 失败)、或 `setup` 就报错的那条;`call` 报告是 skipped 的不记(它已在 `skipped` 里,两边都算会让「收集 = 执行 + 跳过 + 没跑到」对不上)。`collected` / `deselected` / `skipped` 三个数字保留,规模行改成「执行了 N 条(收集 C 条;其中跳过 S 条;还没跑到 X 条)」。
- 口径对齐:`attribution.json` 里只有 `ran.executed` 表示「跑了多少条」(旧证据目录为 `null`),`ran.cases` 仍是「证据里留下读数的条数」;设备范围那侧的 `device_scope` / `out_of_scope_devices` / `no_readings` 不变,`scope.executed` 与规模行同一个数。`executed` 缺失时输出「执行条数未知」,不退回 `collected − skipped`。
- 全量 320 passed / 6 skipped(基线 315 / 6);实跑 `--maxfail=1` 一轮:收集 4 / 执行 1 / 跳过 0,规模行「执行了 1 条(收集 4 条;其中跳过 0 条;还没跑到 3 条)」。
- 2026-10-21 已完成 `6ed0765` + `8d126bb` + `5a790c1` / 合入后 320 passed / 6 skipped。`executed` 来自 pytest 报告,规模行用它;缺失时显示"执行条数未知",不退回减法。
