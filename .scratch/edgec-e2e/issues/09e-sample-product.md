# 09e 示例产品随包

Status: resolved

## 缺口

`configs/` 随 wheel 走,`tests_support/` 不随包 → 装完跑 `acceptance_sample` 必然 exit 4,而错误消息说「**产品命令不可执行:<项目>/tests_support/fake_product.py 不存在**」—— **把"包缺文件"说成了"你项目缺文件"**,责任方指错。

## 落成

- 只 `fake_product.py` 一个文件进 `_data/tests_support/`(整个 `tests_support/` 不进,wheel 内 `site-packages/testplatform/_data/`)
- 任务里的产品路径走**同一个资源解析入口**(不 `__file__` 拼路径);`layout.sample_product` 记来源
- `product_sha256` 三态:文件型 → 真 sha256;内联命令型(命令行无文件参数)→ `"unknown"`;**永不 null**

## Comments

- 2026-10-24 合入 `c85e295`。**leader 自验(干净环境,不看它的表)**:
  - `git archive c85e295` → `python -m build --wheel` → 新 venv 装 → `_data/tests_support/fake_product.py` 在 ✓
  - 项目目录 = 用例树 + configs,**故意删掉 `tests_support/fake_product.py`** → `acceptance_sample` → **exit 0**,`3 passed`
  - 该轮 `task.json`:`layout = {pytest_ini: package, slave_script: package, registers: project, clauses: project, sample_product: package, cases: project}`,`product_sha256 = 72dc12d2…`(安装形态不再是 null)
  - **负例**:删掉包内 `_data/tests_support/fake_product.py` → **exit 4**,消息:「产品命令不可执行: **包内缺** tests_support/fake_product.py(平台包装坏了,重装平台包)」+ 包内应有路径 + 「或在项目目录放一份(项目目录优先)」
  - 内联命令型产品:读**现文** `test_task_runner.py:130-149` —— `assert "product_sha256" in env` + `== "unknown"`;单跑该用例 passed;变异(去掉 `or UNKNOWN` → 必红)由实现方实测
  - master 全量 **1005 passed / 12 skipped · 1017 collected**(基线 1000/12/1012 → +5)
- **验收条件改写过一次(leader 的错)**:原写「只有 configs/ 的目录 → exit 0」,与 09d 的不变量「用例不随包」冲突 → 不可能达成。改为「用例树 + configs、无 `tests_support/`」。教训:**两个不变量混一条验收,红了分不清谁坏。**
- 未做:产品是 PATH 上命令时能否算摘要(本票已按 `unknown` 处理,不再 null)。
- 事故:worker 在 23:58 被运行器杀(`proof-write-failed`),改动留在工作区由上层接手提交 `346dc77`。**再次印证:长任务分批、结论先落 commit。**
