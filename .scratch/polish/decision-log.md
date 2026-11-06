# test-platform 打磨:决策与进度

- 目标:把 test-platform 打磨得更专业、扩展更灵活、覆盖更广、上手门槛更低、使用更方便。
- 约束:所有改动留在本地,不 push 远端。
- 起点:`main` @ `a30ad3a` + 上一轮未提交的精度修复(1220 passed / 93.33%)。

## 已完成

### A1 统一读数结果

- `read_all()` 在电表、逆变器、电池上都有 `.readings` / `.passed` / `.breaches`。
- 实现:`RecordedReadings` 增加 `verdict` 字段与代理属性;`PointReadings` 把聚合根读数带回来。
- 终端没有判据:访问 `.passed` 抛 `NoVerdictError`(TypeError 子类,不是 `InvariantViolation`,
  避免 pytest 层把它归成"平台错误")。`TerminalReading` 不提供 `.breaches`,证据层据此继续判"不判合格"。
- 测试:`tests_unit/test_reading_surface.py`;逆变器、电池、终端三条既有用例补了属性面断言。

### B1 统一 CLI

- `testplatform run <任务> [参数]` 与 `testplatform gate <报告> [参数]` 原样转发到已有 main。
- 实现:在 argparse 之前拦截 `run` / `gate`,避免外层吃掉 `--root` / `--help`;退出码不另写一套。
- `testplatform --version`;`__version__` 放在 `src/testplatform/__init__.py`,`tests_unit/test_cli.py` 与 pyproject 对齐。
- 测试:`tests_unit/test_cli.py`(版本、帮助、run 的退出码 4、gate 的退出码 5、转发 `--help`)。

### A4 十分钟教程

- `docs/how-to/write-your-first-case.md`;`docs/how-to/README.md` 与 README 加入口。
- 教程用统一读数面、可读编号、条款一行、`testplatform run/gate`。

### 附带

- `scaffold` 的样板用例与提示命令改用 `testplatform run` 和 `reading.passed`。
- 用例行号变化后重新生成了 `catalog/catalog.json` 与 `catalog/CATALOG.md`(保持 CRLF,避免整文件 diff)。

### A2 `testplatform new-case`

- 往已有项目加一条用例,不重新生成整个项目。
- 条款必须已在项目的 `configs/clauses.yaml` 里;未知条款报错并列出可用 id。
- 生成文件使用统一读数面,打印 `python -m pytest tests -q -k <name>`。
- 测试:`tests_unit/test_new_case.py`。

### D1 协议错误分支

- `tests_unit/test_modbus_error_paths.py`:RTU 与 MBAP 的请求/应答坏帧、短帧、错从站、错功能码、
  字节数不符、回显数量不符、超长 payload。
- `tests_unit/test_protocol_dlt698*.py` 已在上一条精度批次补齐。

### E1 lint 门禁

- `pyproject.toml` 增加 `[tool.ruff]` 与 `[tool.ruff.lint]`,先只选 `E9` / `F821`(语法错误与未定义名字)。
- `dev` extra 增加 `ruff>=0.17`;CI 的 `python-310` job 增加 `ruff check .`。
- 本地实测 `ruff check .` 通过。

## 最终验证(本轮)

| 检查 | 结果 |
|---|---|
| 全量 `pytest` | 1267 passed / 17 skipped / 0 failed |
| 覆盖率(domain + protocol,门槛 93) | 95.09% |
| `ruff check .`(E9 / F821) | 通过 |
| `tests_web` 对 EdgeC | 23/23;3 个随机种子各 23 passed;`summarize.py` 23/23,exit 0 |
| EdgeC 验收(全产品) | 门禁「全部合格:4 个验收任务」+5 个 smoke |
| EdgeC WebUI 验收 | 23/23 product_ok |
| 本轮开始时的干净基线 | 7 failed / 11 errors / 1147 passed |

基线里的红项由两部分组成:环境缺可选依赖(`build` / `paho-mqtt`)与本轮修掉的代码缺陷
(`eject` 的 Python 下界、npm Node 24 输出、`user_project_e2e` 的 site 目录)。
最终全量 0 failed。

## EdgeC 验收回放（2026-11-06）

用本轮修改后的 test-platform 跑 EdgeC 的验收入口（`TESTPLATFORM=/home/edge/test-platform`、
`TESTPLATFORM_SKIP_PIN=1`、`PYTHON=/tmp/ci-venv/bin/python`）。

| 产品 | 结果 |
|---|---|
| DCU（首次） | `dcu_acceptance` 2/2；`dcu_northbound` **3 failed / 1 passed** |
| DCU（修正 EdgeC 响应控制域后） | `dcu_acceptance` 2/2；`dcu_northbound` 4/4；门禁「全部合格」 |
| iEMS | 2 个验收任务全合格；4 个 smoke 全通过；门禁「全部合格」 |
| WebUI | 23/23 product_ok；`summarize.py` 23/23，exit 0 |
| SmartBox | 5 个 smoke 全通过；门禁 6（只有 smoke，没有验收任务，按设计） |

首次 DCU 北向失败的原因：EdgeC 的 698 从站响应控制域是 `0x03`（DIR=0），而标准附录 H.3.2 的响应帧是
`0xC3`（DIR=1、PRM=1）。平台驱动现在按标准校验 DIR，所以拒绝它。这是产品侧的真实不合规，
不是平台放宽校验的理由。

处理：EdgeC `modules/edge_protocol/src/dlt698/dlt698_codec.c` 三处响应编码的控制域改成 `0xC3`
（GET-Response、GET-Response-List、ACTION-Response），重建 DCU 与 698 测试二进制；
EdgeC 自己的 698 C 测试 6/6 通过，DCU 北向验收 4/4 通过。EdgeC 改动留在本地，未提交、未 push。

### C1/C2 扩展点

- 新增 `src/testplatform/config/protocols.py`:驱动在 `@register_device` 旁边调用
  `register_protocol("<规约>", access={...})`;`config/manifest.py` 的静态 `PROTOCOL_ACCESS`
  只作独立解析回退,驱动注册优先,两边不一致时解析清单直接报错。
- `composition/registrations.py` 的 `load_plugins()` 扫描 `testplatform.drivers` 与
  `testplatform.transports` 两个 entry point 组;插件导入失败带插件名报错。
- 测试:`tests_unit/test_protocol_registry.py`、`tests_unit/test_plugin_entry_points.py`。
- 文档:README 的“新增一个规约/设备类型/接入方式”改成注册时声明,不再指向中央表。

### A3 场景门面

- `reporting/scenario.py` 增加 `ScenarioRecorder`:`device / step / measure / expect / settle / record`,
  并代理 `ScenarioCleanup`(`on_cleanup` / `run` / `ran`),旧写法不用改。
- `tests_scenario/conftest.py` 的 `scenario` fixture 改成返回门面;`record()` 自动收集参与方、
  role、读数键与用例身份;空步骤直接报错,不产出 passed。
- `tests_scenario/test_grid_frequency.py` 第一条用例改写成门面写法,场景 JSON 的 verdict 与
  participants 不变;其余用例继续用旧写法,证明兼容。
- 测试:`tests_unit/test_scenario_recorder.py`;`tests_scenario` 8 passed。

### C3 就地脚手架

- `testplatform new-driver <名字> --out <项目> [--device-type ...] [--protocol ...]`：
  写 `<module>_driver.py`、把 `import <module>_driver` 追加到项目 `conftest.py`，并把
  `[project.entry-points."testplatform.drivers"]` 与 `[tool.setuptools] py-modules` 写进项目
  `pyproject.toml`。`vendor_ascii` 生成即可跑；其它规约生成 `read_raw` 骨架。
- `testplatform new-protocol <名字> --out <项目>`：写纯函数模块，`python <文件>` 跑自检。
- `scaffold driver` 生成的项目自带 entry point，装成包后插件加载器能发现它。
- 测试：`tests_unit/test_new_in_place.py`；子进程验证生成驱动真的注册进注册表。

### tests_web 顺序隔离

- EdgeC `web/mock-server/server.mjs` 增加 `POST /__test/reset`：把用户、角色、告警、告警归档、通知渠道、
  接口状态、配置分区、控制设备与所有内存 Map 恢复成启动快照。路径不在 `/api/` 下，不进匿名
  allowlist，不改对外 API。
- `tests_web/conftest.py` 的 autouse fixture 在每条用例前 reset 用到的 mock-server；
  缺这个入口时直接报错，不静默退化成顺序相关。
- 实测 3 个随机种子各 23 passed；`summarize.py` 的缺省证据目录修正为 `evidence/webui-e2e`
  （原为 `reports/webui-e2e`，与写证据的目录不一致），并加测试守护。

### README 拆分

- `扩展指南` → `docs/reference/extension-guide.md`
- `证据` → `docs/reference/evidence.md`
- `一轮的失败归因` → `docs/reference/attribution.md`
- README 保留入口、运行、设备清单、目录结构、原则、写用例与 `tests_unit` 说明；交叉引用改成链接。

### F 大文件拆分

- `config/manifest.py` → `config/manifest/`：`constants.py`（常量与访问白名单）、`schema.py`（Point / Model / Manifest / 设置类条款）、`parsing.py`（YAML → Manifest）、`validation.py`（装配前校验）。
- `reporting/attribution.py` → `reporting/attribution/`：`core.py`（读证据与 Attribution）、`render.py`（人读渲染与 `attribution.json` 写出）。
- `execution/task.py` → `execution/task/`：`config.py`（任务文件加载与校验）、`runtime.py`（端口、产品、从站、pytest、证据）。
- 三个包的 `__init__.py` 重导出旧路径的全部公开名字，调用方一行不改。证据契约测试里冻结的发射点路径同步改成新文件。
- 行为不变，验证用全量套件与 EdgeC 验收；没有保留兼容 shim 文件。

### G 能力审计:模拟设备 / 新设备 / 新协议 / 用例与任务管理

用一个临时项目做端到端 dogfood（`/tmp/tp-dogfood`），不是只读文档：

- 同型号加第二台设备、同规约加第二个型号、各加一条用例，任务从 1 passed 走到 3 passed。
- 新协议 `demo_text`：`new-protocol` + `new-driver`，写成 ASCII 读法；新协议电表模型 + 设备 + 用例，
  任务 4 passed。
- 再用 `new-simulator --access demo_sim` 生成专用模拟器，设备 `access: demo_sim`，任务 5 passed。

审计发现并修掉的缺口：

1. **任务加载看不到项目里的协议注册。** 任务在父进程解析清单，子 pytest 还没起；项目 `conftest.py`
   里的 `register_protocol` 还没执行，新协议被判成"未知规约"。`execution/task/config.py` 现在在
   `load_task` 解析清单前导入项目根 `conftest.py`；导入失败直接 `TaskFileError`，不交半份清单。
   测试 `tests_platform/test_task_project_plugins.py`。
2. **`new-driver` 不生成 `register_protocol`。** 非 `vendor_ascii` 模板现在带
   `register_protocol("<规约>", access={...})`，新增 `--access`（逗号分隔）。
3. **新协议没有就地模拟器。** 新增 `testplatform new-simulator <名字> --access <接入方式>`：
   生成 `@register_transport(<access>, "simulator")` 的传输，带 `timeout` / `link-lost` / `slow`
   故障钩子，并追加项目 `conftest.py` 导入。缺省 access 是 `<名字>_sim`，不覆盖内置 tcp/serial。
4. **用例管理只有平台仓库的 `tools/catalog.py`。** 新增 `testplatform cases`：只扫 `@case`，
   列编号 / 条款 / 标签 / 文件行 / 设备类型，支持 `--tag` / `--clause` / `--id` / `--json`。
5. **任务管理只有运行期筛选。** 新增 `testplatform tasks`：列任务并逐个 `load_task` 校验，
   支持 `--kind` / `--tag` / `--device` / `--json`；坏任务在列表阶段就报，不用跑一遍才发现。

仍然保留的边界（不是缺陷，是下一层的事）：

- 新**设备类型**（档 4）仍要手写聚合根 + 驱动。判据与不变量是领域知识，生成一个通用聚合根只会
  得到一个"看着完整"的空壳；文档给了 1 天的步骤。
- `cases` 只列 Python `@case`；YAML 的 `setting_cases` 归清单，完整资产目录仍是 `tools/catalog.py`。
- 新接入方式（非 `tcp` / `serial` / `gateway_ssh`）的**真实**传输仍要手写；`new-simulator`
  只覆盖模拟侧。

### H 收尾审计

- 全量连跑 4 次（每次独立进程）全部 1274 passed；加上此前 `test_installed_layout.py --count=20` 的 140 条，
  仍未复现 `docs/status.md` 11b 的约 1/6 偶发。没有可修的现场，不做猜测性改动。
- 补了生成物的行为测试：`new-simulator` 的 `timeout` / `link-lost` 真的抛错；`new-driver` 生成的
  `register_protocol` 真的把 access 写进注册表。测试在 `tests_unit/test_new_in_place.py`。
- 最终全量 1276 passed / 17 skipped / 95.09% 覆盖率，`ruff check .` 通过。
- EdgeC CI 仍按 `acceptance/testplatform.sha` 固定旧平台；要把这一轮带进 CI，需要先提交/推送平台
  再更新那个 pin。当前按约束不 push。

## 下一步

- `test_installed_layout` 偶发：本轮连跑 20 次（140 条）未复现，全量也没有复现。状态仍见
  `docs/status.md` 的 11b；下一次出现时按该票记录顺序与资源组合。
- 如果还要继续缩小单文件：`manifest/parsing.py`（671 行）、`task/runtime.py`（903 行）、
  `attribution/core.py`（822 行）可以再按子职责拆一层。本轮停在包级拆分，因为再切会跨更多调用边界，
  收益是阅读顺序，风险是新 import 环。
