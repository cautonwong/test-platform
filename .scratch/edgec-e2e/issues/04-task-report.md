# 04 测试报告:四档结论与两栏通过率

Status: resolved
Type: task
Blocked by: 01 一条任务跑起来(最小闭环)、03 进程死因分类与证据完整度

**What to build:** 一个任务跑完产出两份东西:给人读的报告、给机器读的 `summary.json`。验收方第一眼就能判过没过,且能追到"哪台设备、哪条条款、超出多少"。

## 验收

- [x] 四档结论各能构造出来:**合格 / 不合格 / 测试无效 / 未完成**
- [x] 通过率**分两栏**:设备级 N/M、场景级 N/M;任务结论取两栏**最差**
- [x] 未测分两栏:申报内(灰,理由必须是枚举:平台不支持/设备缺能力/人工步骤/已知缺陷跳过)、**申报外(红)**
- [x] 报告写出**分母**(`select ∩ 适用矩阵` 命中的条款数)
- [x] 场景失败标注归属(设备 / 台架 / 平台),不合并成一句"场景失败"
- [x] 不合格项含:设备 id、条款、实测值、边界、超出多少
- [x] `summary.json` 含 runtime 与 env(同 03)
- [x] 有守卫用例:**出现申报外未测时,结论不得为"合格"**
- [x] 基线不回归

## Comments

- 2026-10-23 两轮:主体 `6664acf`、`kind` 补丁 `b86a919`。leader-0 六步审(只审补丁那一处 + 架构六条):
  - `kind` 缺省与空值都落 **acceptance**(fail-closed,不是 smoke);`kind: bogus` 加载期 `exit=2`;`acceptance` + 0/0 → 首行「未完成(reason: no_device_coverage)」;`smoke` + 0/0 → 「合格(冒烟:本轮未验设备)」且 exit 0;`minimal`/`mbap` 显式 `kind: smoke` 后仍 exit 0;三次改坏分别 2/1/1 failed
  - `clause.summary` 在人读报告里 **0 次** —— 口径落实
- **裁定记录**:`0/0` 不算合格(turn: 没验到的东西不能算通过);用任务级 `kind` 显式声明"这一轮验什么",缺省 acceptance。
- **待补(同轮已派)**:`run_task` 的退出码要**由 verdict 决定**(0/1/2/3/4),不是等于 pytest 退出码 —— 否则 `acceptance` + 0/0 会出现"报告说未完成、CI 看到 0"的假绿窗口。`pytest_exit_code` 仍写进 `summary.json` 供诊断。
- 与 06 的 rebase 冲突(条款守卫白名单)按**并集**处理,06 的政策未改。
