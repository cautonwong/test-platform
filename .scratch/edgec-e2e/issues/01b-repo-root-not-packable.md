# 01b 执行器把仓库根写死:装成 npm 包之后跑不起来

Status: resolved
Type: task
Blocked by: None — 但必须在票 09(打包分发)之前修完

**What to build:** 任务执行器用「源码树相对位置」推出仓库根,并据此找 `tools/`、`configs/`、`pyproject.toml`、`src/`,还把相对路径(`devices`、`launch` 里的脚本)一并按它解析。这在仓库里跑是对的;一旦平台按已定决策「以 npm 包分发、monorepo 锁版本」装到 site-packages,**这四条路径全部不存在**,执行器直接不可用。

## 现状

```
REPO_ROOT = Path(__file__).resolve().parents[2]     # 依赖 src/ 布局
SLAVE_SCRIPT  = REPO_ROOT / "tools" / "mock_modbus_meter.py"
DEFAULT_REGISTERS = REPO_ROOT / "configs" / "mock_registers.yaml"
子 pytest: cwd=REPO_ROOT、-c REPO_ROOT/"pyproject.toml"、PYTHONPATH=REPO_ROOT/"src"
```

安装形态下 `parents[2]` 指向 site-packages 的上级,四处路径都不存在。

## 期望

1. **仓库根可显式指定**:环境变量或命令行参数(优先级:显式参数 > 环境变量 > 自动探测)
2. **自动探测失败时要报清楚**:缺哪个文件、期望在哪、怎么用参数指定 —— 而不是 `FileNotFoundError` 或子 pytest 的 `exit 4`
3. **`tools/mock_modbus_meter.py` 与 `configs/mock_registers.yaml` 要能随包分发**(或允许任务指定替代路径);它们是执行器的一部分,不是仓库里的示例
4. 子 pytest 的调用不要在缺 `pyproject.toml` 时静默降级成"从 cwd 猜"
5. 相对路径解析仍按"仓库根"这条既有规则,不引入第二套

## 不做

- 不改变"任务文件里 `devices`/`launch` 脚本路径相对仓库根"这条已定规则
- 不引入容器/虚拟环境管理

## 验收

- [x] 用一个**不在源码树里**的目录当仓库根(比如临时目录,只放 `pyproject.toml` 与 `src`),任务仍能跑通
- [x] 显式指定根与自动探测各有一条用例;两者冲突时显式赢
- [x] 自动探测失败 → 一处报错,列出缺失文件与设置方法
- [x] 模拟"安装形态"(把包目录挪出源码树)时,报错可读,不是 `exit 4` 或 ImportError
- [x] 基线不回归,差值可解释

## 追加要求(2026-10-23,来自 06 的 rebase 实测)

`env.commit` 的实现是"跑 `git rev-parse HEAD`(cwd=仓库根)" —— **在没有 `.git` 的目录里静默变成 `None`**,而 03 自己的用例又断言它是 7–40 位 sha:

```
从 git archive 抽出的目录里跑规模套件 → 1 failed
  assert re.fullmatch(r"[0-9a-f]{7,40}", env["commit"] or "")   # commit=None
```

**这是本票同一个病**:证据字段的来源不该由"目录里有没有 `.git`"决定。平台一旦按 09 以 npm 包分发,安装目录通常没有 `.git`,**`env.commit` 与 04 生成的报告会一起静默缺字段**。

所以本票再加一条:

- [x] `env.commit` 的来源必须**显式**:构建/发布时写入(或读 `PI_COMMIT` 这类环境变量),取不到时**报错或明确标 `unknown`**,不允许静默 `None`
- [x] 有一条用例证明:在没有 `.git` 的目录里跑,`env.commit` 要么有值要么明确 `unknown`,不会静默缺

## Comments

- 2026-10-23 合入 `e2eab20`。leader-0 自验:`grep -rn "parents\[" src/` = **0**;显式根真跑任务(临时目录当根,不在源码树内);`env.commit == "unknown"`(无 `.git` 时不是 None/空串);架构六条 ①0 ②0 ③0 ④0 ⑤0,⑥ 顶层仍只 `__init__.py / cases.py / clauses.py`。
- 复现:`pytest tests_unit/test_platform_paths.py tests_platform/test_task_root.py -q`;真任务 `python tools/run_task.py <task.yaml> --root <目录>`。
- **合入时发现语义冲突(leader 漏看)**:该分支基线 `b86a919` 早于退出码连线 `97a4ae9`,4 条用例断言 `exit_code == 0` 在 master 上变成 3。处置:这 4 条验的是平台行为 → 任务 YAML 加 `kind: smoke`(`1db4a33` 修好)。**教训:合入前要看分支基线是否落后于改了契约的提交。**
- 留给 09:`testplatform_plugin.py` 在仓库根、不在包内;子 pytest 的 `PYTHONPATH` 兜底已标 `shortcut:`;分发形态必须随包安装或声明 entry point。

### 2026-10-23 补记(票 09d 对判据的替换)

本票的三条负向用例编码的判据是「**显式根 / 环境变量给的根必须四样齐全**」。票 09d(逐样回落)把判据换成
「**逐样:项目有就用项目的,包内有就用包内的,两边都没有才报错**」,因此这三条的**旧断言必须随之改判**:

| 用例 | 09d 后 |
|---|---|
| `test_an_explicit_root_that_is_incomplete_lists_every_missing_piece` | 改成负例:包内 `_data` 也缺 → 仍报错,消息含**包内与项目两侧路径** |
| `test_an_installed_looking_tree_is_not_silently_accepted` | 改成正例:显式根只含包目录 → **加载成功**,`sources()` 记 `package` |
| `test_a_root_that_is_only_the_package_reports_what_the_executor_needs` | 删除,由 `tests_platform/test_installed_layout.py` 同形状用例取代(commit 里点名) |

判定:**不改判 = 与 09d 直接冲突;skip = 把旧契约挂在墙上当装饰**。两条正向用例(`explicit_root_outside_the_source_tree`、`root_environment_variable_also_works`)一字不动。
