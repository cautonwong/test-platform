# 01 一条任务跑起来(最小闭环)

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 测试工程师写一份任务文件,一条命令跑完:平台按任务起「产品进程」、分配端口填进命令、等它就绪、拉起模拟周边设备、跑选中的用例、落证据、给出退出码。**这是整套方案的第一个可验证闭环。**

任务文件的形状(决策):`id`、`product`(仅身份与追踪)、`launch`(含 `${PORT}` 占位符的命令行)、`devices`(设备清单)、`select`(用例筛选)。

产品由**一个假产品脚本**扮演(监听端口、按 Modbus 读写、可配置慢启动/不启动/崩溃),不依赖 EdgeC 编译。

## 验收

- [x] 写一份任务文件后,一条命令跑完,退出码 0
- [x] 产品从平台分配的端口起来;实际命令行与端口出现在证据里
- [x] 就绪等待生效:产品延迟 2 秒才开始监听时用例不失败;等待时长进证据
- [x] 产品经 Modbus 读到模拟从站给的值,用例断言成立
- [x] 证据目录含每设备 JSONL + 轮次范围 + 轮次状态
- [x] 任务引用不存在的清单/不可执行的产品路径 → 加载期报错,指出是哪一条
- [x] 基线不回归(`docs/baseline.md`),数字差值可解释

## Comments

- 2026-10-23 合入前审(交付 commit `fbc9fb1`,父 `7dc766c`,分支 p1/task-runner)。leader-0 独立复核:
  - 全量 `708 passed / 8 skipped / 716 collected`(基线 702/7/709,+6 通过 +1 跳过,差值可解释)
  - 任务真跑:`tools/run_task.py configs/tasks/minimal.yaml --evidence-dir <空目录>` → `1 passed`、`EXIT=0`
  - `task.json`:产品端口 `65035`、从站端口 `meter_01:65038`(都不是清单里写死的 15020)、`readiness {"seconds": 2.36, "timeout": 15.0}`
  - 四个加载期探针全部 `EXIT=2` 且消息点名:清单不存在 / 未知占位符(`${METER_PORT}` 会列出可用值)/ 缺 `${PORT}` / `${SLAVE_PORT:meter_99}` 未知设备 id(列出清单里的设备)
  - 证据目录六件齐:`meter_01.jsonl`、`run-scope.json`、`round-status.json`、`task.json`、`product.out`、`product.err`
  - 进程无残留(跑完后 `fake_product.py` 残留 0)
  - `tests/conftest.py` 的 `+13`(清单没有该类型设备 → skip)未削弱硬错误:`tests_unit` 里 marker/capability 5 条仍绿
- **审查发现两条,另开 `01b`(Must,阻塞票 09)与一条文档要求**
