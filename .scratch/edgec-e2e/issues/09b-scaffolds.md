# 09b 四个 scaffold + 消费者端到端

Status: resolved

## 落成

```
testplatform scaffold driver|protocol|case|simulator --out <dir>
→ 生成一个完整小项目:pyproject.toml / product.py / configs/{devices,clauses}.yaml
   / tasks/<kind>_sample.yaml / tests/test_<kind>.py
→ testplatform-run-task <dir>/tasks/<kind>_sample.yaml --root <dir> --evidence-dir <dir>/reports → exit 0
```

生成物全部 Python,每个文件第一行写明「`npx` 只是外壳,写的是 Python」;项目自带判据集与条款清单,不依赖仓库 `tests_support/`。

## Comments

- 2026-10-23 合入 `7f9ed10`。**leader 自验(不看它的表)**:
  - `pytest -q tests_platform/test_scaffolds.py` → **10 passed**(12.17 s)
  - 手跑:`python -m testplatform.cli scaffold driver --out /tmp/scf3` → exit 0,产物 6 项齐全
  - 手跑生成的项目:`testplatform.execution.cli tasks/driver_sample.yaml --root .` → **exit 0**,`1 passed`
  - 生成文件首行确为「这个文件是 Python …」
  - master 全量 **1000 passed / 12 skipped · 1012 collected**(基线 990/12/1002 → +10)
- 设计决定(实现方提出,leader 认可):纯函数骨架单跑时本轮无证据 → 判未完成(exit 3);故每个生成项目都配一条**真读模拟设备**的样板用例(`tag:样板`),纯函数自检 + 设备读数各一条。**"生成即可跑"因此是真绿,不是 smoke + 空跑。**
- 顺带修 07 守卫一条边界:生成器里的 `clause="{clause_id}"` 是格式化占位符,不是条款声明 → 值里含 `{`/`}` 跳过;真正的检查仍在收集期。
- **基线的记法问题**:实现方报「1000 passed,但没单独量基线」。leader 已实量:`4295719` 为 990/12/1002,合入后 +10。
- 未做:`Python < 3.10 一次报清` / `无网·venv 慢打印进度` 两条依赖 npm 壳(09f),不在本票重复实现;骨架是"最薄起点",不生成点表库/多设备项目。

## 判据(长期有效)

四份骨架是**起点**;但「**生成 → 立刻 run → exit 0**」这条断言长期有效 —— 它防的是"生成一堆跑不起来的东西",比骨架本身好看更重要。
