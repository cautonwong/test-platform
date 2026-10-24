# 15 第一条 kind: acceptance 任务

Status: resolved
Priority: P0 —— CI 第 6 档现在必然红("没有一条验收任务在跑"),本票是解锁点

## 边界(先读这条)

EdgeC 真产品在另一个仓库(`E:/workspace/EdgeC/cmd/*`),CI 里没有。所以:

> 本票交付**能跑的验收任务骨架 + 仓库内示例产品**;报告必须 `设备级 1/1 台` + 场景 `≥1 条`,`verdict=合格`。
> **它不是 EdgeC 产品的验收。** "接真产品"是票 16(需 monorepo 路径 + 构建步骤)。

**不许**为了凑绿写 `kind: smoke` —— gate 第 6 档会立刻抓住。

## 落成物

- [x] `configs/tasks/acceptance_sample.yaml`:`kind: acceptance`;launch 用 `${PYTHON}` / `${PORT}` / `${SLAVE_PORT:meter_01}`;不带真硬件
- [x] 1 台设备级契约用例 + 1 条场景用例,**都要真读数**;用共用 tag `验收` + `select: tag:验收`(选择器已存在:`cases.py:28` `SELECTOR_KINDS`)
- [x] 只加 tag,**编号 / 条款 / 断言一字不动**
- [x] 端到端 <60 s(GitHub runner 要能过),`--evidence-dir reports/<task>`
- [x] 任务文件头 + 报告写明"示例产品,不是 EdgeC 产品"
- [x] 证据含 `port_source` / `port_range` / `port_attempts`(**由票 14 提供,本票只消费,不许自己加**)

## 并发约束(硬)

**不许改 `src/testplatform/execution/task.py`** —— 票 14 正在改它并新增上述三个字段。两个 writer 一个文件必撞。
**不许改 `configs/devices.yaml`**(共享输入)。
不许动 `reporting/**`、`.github/**`、`docs/**`、`.scratch/**`。

## 守卫

- [x] 产品起不来 → `未完成` + `reason: platform_error`(**不是合格**)
- [x] 用例换成不按设备展开的 → `未完成`(证明本任务真按设备展开)
- [x] `python tools/gate.py reports` 对它的**单独**报告 → exit 0
- [x] 在**没有 `.git`** 的目录里也能跑(`env.commit` 落 `unknown`)

## 基线

开工时以 master 最新提交为准,报差值来源(新增用例数 / 新任务)。

## Comments

- 2026-10-23 合入 `0cafae0`。**leader 自验**:任务 exit=0、`3 passed`、`devices 1/1`、`scenarios 1/1`、`gate` 单独报告 exit 0、`verdict=clean`、`env.commit` 有值;守卫 6 条(产品起不来 → `platform_error`;换成只跑场景 → `no_device_coverage` + exit 3)。
- **边界**:`product` 是仓库内 `tests_support/fake_product.py`,**不是 EdgeC 产品**;报告与任务头都写明。真产品验收挂起为票 16(产品在 Linux、当前拿不到)。
- 09e 之后示例产品随包,本任务在**安装形态**下也能跑(见票 09 的 Comments)。