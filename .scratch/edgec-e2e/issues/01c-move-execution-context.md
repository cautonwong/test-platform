# 01c 预重构:任务执行上下文从根目录挪进 `execution/`

Status: resolved
Type: task
Blocked by: None — 预重构,必须在 03(死因分类)动 task.py 之前完成

**What to build:** `task.py` 平铺在 `src/testplatform/` 根上,而它是**新的有界上下文**(测试执行:任务/占位符/就绪/退出码)。根目录已经有 `cases.py`,再往上叠 `task`/`report`/`catalog` 会让根变成杂物箱。本票只做**搬家**,不改行为。

## 为什么要先做

03(进程死因)、04(报告)、08(目录)都会往这条线上加代码。先定归位,后面每次只往一个既定的包里加,不用每票都想"这个该放哪"。

## 范围

- 把任务执行相关模块移入 `execution/`(包内保留原有公共名字,调用方改成新路径)
- `tools/run_task.py`、任务相关测试的 import 跟着改
- **只搬不改**:不重命名函数、不改逻辑、不加新行为

## 依赖方向(搬完要成立)

- `execution/` 可以依赖 `config/`、`cases.py`;装配仍发生在子进程里(组合根不在 `execution/` 里被调用)
- `domain/` **不得**依赖 `execution/`、`catalog/`、`reporting/`
- 除 `composition/root.py` 外,**任何模块不得 import 具体 driver/transport**

## 验收

- [x] 任务执行模块位于 `execution/`;`src/testplatform/` 根上不再有任务执行模块
- [x] `tools/run_task.py ... minimal.yaml --evidence-dir <空目录>` 仍 `1 passed`、退出码 0
- [x] 全量与收集数不变(只可能因 import 路径变化的用例措辞而变,数值不变):`768 passed / 8 skipped · 776 collected @ 07 合入后`
- [x] diff 里是移动为主;被移动文件的内容变化只限 import 行
- [x] `grep` 确认:`domain/` 不 import `execution/`;除组合根外无人 import 具体 driver/transport

## Comments

- 2026-10-23 合入 `37424ce`(交付 `2adb3cf`,rebase 到 `ffc0b04`)。leader-0 五步审:
  ① `git show --stat -M`:`rename 96%`,5 文件 14+/4−,被移动文件只动 import 行 + `parents[3]` + `__init__` 说明
  ② ponytail-review:`__init__.py` 只放上下文说明、不放代码 —— 对,避免它变成第二个杂物箱
  ③ ocr delegate preview:`4c48c4a..c1dc160→2adb3cf` 改动面与声明一致
  ④ 探针:全量 `768 passed / 8 skipped · 776 collected` 与合并前**逐字一致**(搬家不改行为的最好证据);`tools/run_task.py configs/tasks/minimal.yaml` → `1 passed` / 退出码 0
  ⑤ 四问:红了 → 跑 minimal 任务即复现(REPO_ROOT 指错会立刻红);绕过 → 搬家无可 mock 的东西,数字不动就是证据;退休 → 只有 `parents[3]` 会被 01b 替换
- 依赖方向:`grep execution src/testplatform/domain/` = 0;`src/` 内除组合根外无具体 driver/transport import。
- 口径纠正(来自本票审):架构第 2/3 条的检查范围是 `src/`,测试文件 import 具体实现是既有设计。
