# 16 issue 12 标了 resolved,但第 3 条没做、5 个验收框一个没勾

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 记账问题,不是功能问题。`11`/`12` 的两张票被上游标成 `Status: resolved` 并提交(`0dc2d45`),但其中至少一条没落地,验收框全空,Comments 只列了做过的、没列没做的。审计链断在这里,后面谁的票“关没关”就说不清了。

## 实测

`12-closeout-quality.md` 第 3 条要求二选一:保持空目录但从 `testpaths` 移除,或留一条占位用例。

```
$ .venv/Scripts/python.exe -m pytest -q tests_scenario
no tests ran in 0.03s
$ ls tests_scenario/
README.md
$ grep testpaths pyproject.toml
testpaths = ["tests", "tests_unit", "tests_integration", "tests_scenario"]
$ git log --oneline -1 -- pyproject.toml
c09f9bd ...        # 自那之后没动过
```

也就是说:目录仍是空壳、`testpaths` 仍包含它、跑它收 0 条。第 3 条没做。

同时:

- 5 个验收框(`- [x]`)一个都没勾
- `Status:` 已改成 `resolved`
- 新增的 Comments 只写「12 已完成:1f2386e + e8670bd」并列出 3 件事(§0 报错、§2 ADR、§4 票号),**§3 既没做也没写延期理由**

已完成的 §0/§1/§2/§4 我复验过,确实做了:`-n 4` 现在开跑前报错(exit 4);`tests/test_topology_grouping.py` 8 passed;四份 ADR 27 行 → 131 行;票 `8` → `8b`。

## 期望

两条路,选一条,并把 Status 与框对齐:

- (a) 做掉 §3:`tests_scenario` 从 `testpaths` 移除(推荐,空目录不产生噪声),在其 README 写明“等第一个真实场景需求再加回来”
- (b) 明确延期:Comments 写「§3 延到 issue NN,原因:等第一个场景需求」,把该框留空并说明

其余四条的框按实际勾上,并在 Comments 里写清 §3 的处理。**别把“没做”和“做完”显示成同一种状态** —— 这个仓库对静默最敏感的地方就是这种差异。

## 验收

- [x] `pytest tests_scenario` 要么收得到用例,要么该目录已从 `testpaths` 移除
- [x] `12-closeout-quality.md` 的 5 个验收框与代码实际状态一一对应;没做的留下空框并写明去处
- [x] `Status:` 只在所有框都有交代之后才写 `resolved`
- [x] 在 `docs/agents/issue-tracker.md` 补一句关票规则:关票 = 每个验收框有勾或写明去处,不允许只改 Status

## Comments

- 2026-10-20 出票依据:对 `0dc2d45` 做只读审核。上面的 `pytest tests_scenario`、`ls`、`grep testpaths`、`git log -- pyproject.toml` 都是实跑;`tests/test_topology_grouping.py` 8 passed 与我核对的 `-n 4` 报错也是实跑,所以不是“什么都没做”,是“做了 4 条、第 5 条没做却一起关了”。

- 2026-10-21 已完成 `9e01c76`:issue/12 的 6 个验收框逐条核对(票面写 5 个,实际 6 个);`tests_scenario` 移出 `testpaths`(空目录收 0 条报绿 = "以为测过了"本身),并补一条双向守卫用例 —— 我实测:目录里出现 `test_*.py` 而 testpaths 没有 → 红;把 tests_scenario 加回 testpaths → 也红;还原 → 绿。`docs/agents/issue-tracker.md` 补了编号与关票规则(两套序列 / 只增不改 / resolved 的确切含义)。issue 11 里 6 处"票 10"改为 8b。
