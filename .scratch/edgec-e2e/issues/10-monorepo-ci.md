# 10 monorepo CI 落地:一任务一 job + 独立门禁

Status: resolved
Type: task
Blocked by: 05 门禁判定:报告 → 退出码、09 打包分发:平台与公共数据各成 npm 包

**What to build:** 在 monorepo 里把任务真正跑起来:PR 跑指定的任务、nightly 跑全量;一个任务一个 job(互不阻塞),产品二进制由独立构建 job 产出并以产物传递;另一个独立 job 汇总所有报告决定红绿。

## 验收

- [x] PR 触发跑指定任务;定时触发跑全量;两者共用同一套任务文件
- [x] 一个任务一个 job(`fail-fast: false`),一个任务失败不影响其他任务出结论
- [x] 构建 job 产出的产品二进制被任务 job 消费,不重复构建
- [x] 门禁 job 汇总所有任务报告并给出唯一红绿
- [x] 报告进 CI 产物(默认90 天);**失败证据默认上传**,全量证据需显式开启
- [x] CI 里"测试无效"= 红,报告首行声明"平台问题,不是产品",并给排查提示(模拟器未起 / 端口未分配 / 产品未就绪)
- [x] 有一个刻意的失败任务作为门禁自检:它能红,且红的原因指向正确的一档

## Comments

- 2026-10-23 合入 `7e84ed9`(一任务一 job + gate `needs` 全覆盖 + artifact 上传 + 六条机械断言),09c 又补了 `install` job 与 `python-version: '3.10'` job。
- **leader 自验**:`tests_unit/test_workflow.py` 通过(12 → 23 条);`.github/workflows/tasks.yml` 里 `--root ${{ github.workspace }}`、`--evidence-dir`、`needs: [task-minimal, task-mbap]`、`tools/gate.py` 均在。
- **已知边界(不许当作已覆盖)**:workflow 的**结果侧**天然只在 GitHub runner 上成立(安装、3.10、artifact 传递)。本地能验的是结构断言。**这条差距已单列为纪律三档之一**(可本地化但漏了 / 不可本地化 / 结构断言 + 下游真跑)。
- 遗留(未做,已在 `docs/status.md` 挂起):artifact 粒度(现在一任务传整份目录)、矩阵化 job(任务数 >10 时再改)。