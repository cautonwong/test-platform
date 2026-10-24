# 02b MBAP 端到端:假产品走 MBAP 跑通一个任务

Status: resolved
Type: task
Blocked by: 01 一条任务跑起来(最小闭环)、02a MBAP 编解码与模拟从站帧格式参数化

**What to build:** 02a 提供了 MBAP 编解码与可切换帧格式的模拟从站;本票把**假产品**接到 MBAP 上,让 01 的同一份任务改走 MBAP 仍然跑通 —— 证明 EdgeC 产品的真实链路形态可用。

原 02 的编解码与帧参数化已拆到 02a,不在本票范围。

## 验收

- [x] 假产品改用 MBAP 后,01 的同一份任务仍然跑通
- [x] 帧格式不匹配(RTU 客户端连 MBAP 从站)报错可辨认,不静默解出值
- [x] 有对照用例:语义相同的应答在两种帧格式下字节不同,防止混淆
- [x] 基线不回归

## Comments

- 2026-10-23 合入 `57cdfae`(rebase 到 `37424ce` 后修一行 import)。leader-0 五步审:
  ① `git show --stat`:6 文件 +492/−26(`mbap.yaml` 新建、`test_task_mbap.py` 新建、`fake_product.py`、`task.py::_start_slave` 按型号规约推 `--frame`、`mock_modbus_meter.py`、条款守卫)
  ② ponytail-review:MBAP 端到端 + 02c 欠的 `read_all()` 全路径都补上了;`clause=` 既有行零改动
  ③ ocr delegate preview:改动面与声明一致
  ④ 探针:`configs/tasks/mbap.yaml` → `1 passed` / 退出码 0;`configs/tasks/minimal.yaml` → `1 passed`(**默认 RTU 未被改坏**);全量 `773 passed / 9 skipped · 782 collected`
  ⑤ 四问:红了 → 跑那两个任务文件;绕过 → 端到端是真进程真 socket;退休 → 帧格式开关默认 RTU,不开就永远用不到
- 合入时撞 01c 的模块搬迁(`testplatform.task` → `testplatform.execution.task`):裁定**改 import,不留 re-export shim**(后来者适配;shim 会永久留下而只服务瞬时需要)。
- 触到 07 的守卫白名单:裁定**按政策而非数量** —— 白名单扩到 4 条(6 处 `free:` 合并为 1 条相同文本)、注释改成目录政策、新增 `FREE_TEXT_DIRS` 结构断言。实测在 `tests/` 放一处 `free:` → 守卫红。
