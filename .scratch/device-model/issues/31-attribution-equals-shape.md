# 31 归因的 attribution.json 对等值违约仍写 limit: null / exceeded_by: null

Status: resolved
Type: task
Blocked by: None

**要修什么:** 证据层已经改好了(`f510f5a`):等值判据违约写 `{"kind": "equals", "expected": …, "value": …}`,`limit` / `exceeded_by` **不出现**,不再让人误读成"没越界"。但**同一份输出里的 `attribution.json` 还留着旧形态**:

```json
{"limit": null, "exceeded_by": null}
```

于是同一个结论在证据里是清楚的、在归因里是含糊的 —— 又变成两份口径。

## 为什么值得修

归因是给人做决策用的入口(哪些不合格、差多少)。等值违约在归因里读成"limit null / exceeded_by null",读者只会得到"这条判据没有边界也没越界,但结论是失败"—— 自相矛盾。

这和 `issue 21`(范围维度)、`issue 22`(条数口径)是同一族:**同一个事实在机器可读的几处说法不一致。**

## 期望

1. `attribution.json` 里的违约条目与证据层同构:等值 → `kind: equals` + `expected` + `value`;区间 → 现有字段一字不变。
2. 终端渲染(`render`)里等值违约那一行要写清期望值与实测值(现在靠 `text`,可以保留,但要确认 `limit: null` 不再出现在任何展示里)。
3. 有一条守卫用例:**同一轮里同时出现等值违约与区间违约**,两种记录形态各自正确,且不互相污染。

## 验收

- [x] 等值违约:归因的机器可读记录里不出现 `limit` / `exceeded_by`(贴真实 JSON)
- [x] 区间违约:记录与现在逐字节一致
- [x] 同一轮两种违约并存的用例
- [x] 全量套件全绿

## Comments

- 2026-10-22 出票依据:`f510f5a` 的交付报告自己标出的残留(当时 `reporting/attribution.py` 不在允许改动清单内)。

- 2026-10-22 已完成 `01bc478` / 合入后 429 passed / 6 skipped。等值违约在归因里改为 `kind: equals` + `expected`,`limit`/`exceeded_by` 不再出现;区间违约记录逐字节未变(用例钉住整条 `json.dumps`)。判定逻辑未动:`_verdict` / `_read_scope` / `_read_scenarios` 零改动,`_read_device_file` 只把 append 换成 `_criterion_failure(...)`。
