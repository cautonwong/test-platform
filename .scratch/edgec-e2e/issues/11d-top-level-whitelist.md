# 11d 顶层白名单:契约更新 + 机器断言

Status: resolved
来源:2026-10-24 leader 架构审查 ⑥ 发现顶层已有 6 个文件(`__init__` / `cases` / `clauses` / `cli` / `plugin` / `scaffold`),而**文档与我的审查口径一直写的是"只允许 3 个"** —— 我在 09/09b 合入时没有跑这一条,属于漏审。

## 处置

1. 契约更新:`docs/agents/collaboration.md` 已改为 6 个白名单 + 每个的理由(leader 已做,勿改 docs)
2. **机器断言**:一条用例读 `src/testplatform/*.py`,断言集合 == 白名单常量;**多一个文件 → 红**,消息要求"改白名单并写理由"
3. 白名单常量放**一处**(如 `tests_unit/test_architecture_contract.py` 顶部),附注释说明每个名字的职责
4. 变异:新增 `src/testplatform/misc.py`(空文件)→ 必红;删除 → 恢复绿

## 为什么不是"随便加都行"

这条规则的本意是 **包根不许变成垃圾场**:`cli`/`plugin`/`scaffold` 是投递壳,职责单一、不含业务;再有新文件必须有人判断它是不是该进子包(`execution/`、`reporting/`、`config/` …)。

## 不许

- 不许把断言写成"包含这几个即可"(那样加文件就不红了)

## Comments

- 11d 由 13 系列后的白名单断言覆盖(6 个顶层名字 + 理由);若有遗漏请以当前用例为准。
