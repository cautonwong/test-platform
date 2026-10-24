# 11 组合闸门下 1/6 概率红:嵌套 pytest 的用例失败时原文没留住

Status: resolved
Type: task
Blocked by: None

**What to build:** `tests_unit/test_manifest_setting_cases.py::test_a_manifest_without_setting_cases_skips_instead_of_failing_collection` 在组合闸门(`-n 4 --dist loadgroup -p randomly --seed=424242`)六次里红了**一次**,串行两次与其他五次全绿。这条用例用嵌套 pytest 起子进程,失败时**断言消息里没有子进程输出**,所以红的原因没留住。

## 为什么必须修,而不是"偶发不管"

我们定过:**CI 里"测试无效" = 红**,且 CI 用的是平台自己起的进程与模拟器 —— **没有真台架抖动这个合法来源**。所以 1/6 的红只有两种解释:平台有竞态,或者断言方式不稳。两种都必须查清。

而"红了但查不动"正是我们反复在治的那类:没有可复现证据的失败,等于把问题推给下一个人。

## 要做两件

1. **让失败可诊断**:子进程的 stdout/stderr 拼进断言消息(截断到合理长度),并把子进程的实际退出码、命令行写进去。
2. **让它可统计**:连跑 20 次该用例(或 10 次组合闸门),给出红/绿计数;若复现,定位是端口分配、并发 worker、还是临时目录竞争。

## 不做

- 不加 `pytest-rerunfailures`:自动重跑会掩盖这类竞态(我们明确禁过)。
- 不因为偶发就把该用例标 skip。

## 验收

- [x] 该用例失败时,断言消息含子进程 stdout/stderr 与退出码
- [x] 连跑 20 次给出红/绿计数;≥1 次红则给出根因(端口/临时目录/worker 并发)
- [x] 若确认是竞态:修根因,并在票面写清"改了哪一处、为什么"
- [x] 基线不回归,差值可解释

## Comments

- 2026-10-23 合入 `6d8ee61`(**由 worker-0 自行合入,违反合入权**;leader 事后在 master 复跑 `898 passed / 12 skipped` 确认无损)。
- **诊断结论:票面的 1/6 未复现。** 计数(实现方跑、leader 未复跑):串行 ×20 全绿;组合闸门 ×10 全绿(`-n 4 --dist loadgroup -p randomly --seed=424242`,每次计数前清残留)。**未复现 ≠ 已修复** —— 那次观测可能是残留进程污染。
- 本票可交付的部分:嵌套子进程失败时把**命令行 / 退出码 / stdout / stderr 原文**拼进断言消息(`_subprocess_failure`),并加一条守卫。变异实测(实现方):把 `f"命令行:` 改成 `f"cmd:` → 1 failed。
- **真正的偶发已转到票 14**:`test_task_runner.py` 那 4 条(超时叫停 / 重启点 / 崩溃死因 / 收集炸掉 exit 2)在一次闸门里 4 failed,重跑全绿。那组护的是"平台能不能说清自己为什么停的",优先级高于本票。
- 旁证:`rm -rf evidence` 报 6 个 `.jsonl` `Device or resource busy` —— 有进程仍开着证据文件,残留清理未彻底。
