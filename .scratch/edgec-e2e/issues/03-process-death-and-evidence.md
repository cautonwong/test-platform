# 03 进程死因分类与证据完整度

Status: resolved
Type: task
Blocked by: 01 一条任务跑起来(最小闭环)

**What to build:** 产品进程死掉时要说得清是谁的责任。三种情形给三种结论,并在证据里留下足够复现的信息。

## 验收

- [x] 产品自己异常退出(非零退出码或收到信号)→ 该轮记**产品不合格**,证据有进程退出记录(码/信号)
- [x] 平台主动终止(收尾、任务超时)→ **不计失败**
- [x] 产品起不来(可执行缺失、参数错、权限)→ **平台错误**,不记成产品不合格
- [x] 崩溃后本轮剩余用例记**测试无效**;报告把「崩溃」与「未执行 N 条」分开陈述
- [x] 证据含:实际命令行、产品 stdout/stderr、runtime(启动耗时 / 首个读数耗时 / 总耗时)、env(commit、平台版本、产品二进制摘要、模拟器版本)
- [x] 显式重启动作可用(给需要冷启动的用例),重启点写进证据
- [x] 三类各有一条用例;把分类改错时对应用例必须红
- [x] 基线不回归

## Comments

- 2026-10-23 合入 `57eea17`。leader-0 六步审:
  ① `git show --stat` + 复跑:`784 passed / 12 skipped · 796 collected`(+11/+3 skip/+14,skip 全是"需要 task.json"的执行器用例)
  ② ponytail-review:`death.py` 落在 `execution/`(未平铺);`evidence.py` **一行未改**(判断:新开 `*.jsonl` 会被归因当设备证据)—— 这个判断对;`code` 为负拆成 `code=null, signal=N`,不让读的人猜负数含义
  ③ ocr delegate preview:`57cdfae..57eea17` 改动面含 `test_death_classification.py` / `test_round_probe_cases.py` / `fake_product.py` / `task_runner.py`,与声明一致
  ④ 探针:分类器的 `exit_event(process, *, ready, platform_stopped, at)` 可注入假证据(无自 new 协作者);`test_wire_labels_are_pinned_literals` 存在且钉住 `self_exit` / `platform_killed`
  ⑤ 架构六条:①0 ②0 ③0 ④0 ⑤0 ⑥顶层仍只 `__init__/cases.py/clauses.py`
  ⑥ 四问:红了 → 单测 1 s;绕过 → 断言用假证据测分支、真进程测接线(两条都有);退休 → `how` 是 04 的 wire 契约,长期保留
- **审出的缺陷(worker-0 自己发现并已修)**:原用例 `assert event["how"] == SELF_EXIT` 是**拿常量和自己比** —— 常量值改成 `"self_exit_typo"` 时 6 条全绿。补了字面量钉死用例。新增失效族:**⑫ 断言引用被测常量**。
- 白名单:07 守卫只加 1 条 `free: 平台:死因分类`,政策未动。
