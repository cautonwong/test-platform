# 02d `expected_transaction` 默认 None 让事务号校验可被静默跳过

Status: resolved
Type: task
Blocked by: None — 4 处调用点的小改动

**What to build:** 02c 给 MBAP 应答解析加了事务号比对,但参数默认 `None` = **不校验**。于是"新写一个 codec 忘了传"这条路会**静默放行**,而且不报错。

## 现状(实测)

```python
parse_read_response(应答事务号 999, unit=1)                       # 不传 expected → 0001e240 静默通过
parse_read_response(应答事务号 999, unit=1, expected_transaction=7)  # → TransactionMismatch ✓
```

默认值当初是为了"保住既有 4 处调用"。但同样的理由会让**第五个调用点**也静默通过。

## 决定(leader 裁定,不要改成"报警")

**参数改成必填(keyword-only)**,不是"不传时 warning"。

理由:
1. 报警在一次跑几百条用例的输出里等于没有;规矩是让"省略即失效"**不可能发生**。
2. 必填的代价只有 4 处调用点,一次性改完,而且改动会把"这里有没有期望值"写进代码。
3. 从站侧回显那种"确实没有期望值"的场景,显式写 `expected_transaction=None` 比默认值更诚实:**读代码的人一眼知道这里故意不校验,因为我是从站**。

## 验收

- [x] `expected_transaction` 是必填 keyword-only 参数;不传 → 调用即报错(TypeError 或显式 ValueError,消息说明"要么给期望事务号,要么显式写 None")
- [x] 客户端路径传真实事务号;从站回显路径显式传 `None`,并在代码里注释为什么
- [x] 有守卫用例:**新写一个不传该参数的调用必须红**(防止以后有人加回默认值)
- [x] 事务号不匹配仍抛 `TransactionMismatch`(02c 的行为不变)
- [x] 基线不回归(`0489ffe · 751 passed / 8 skipped · 759 collected`),差值可解释

## Comments

- 2026-10-23 合入 `4c48c4a`。leader-0 五步审:
  ① `git show --stat`:4 文件 75+/26−
  ② ponytail-review:参数改 keyword-only 必填,客户端 3 处显式传 `self._pending_transaction`,从站回显路径显式传 `None`
  ③ ocr delegate preview:改动面一致
  ④ 探针:不传 → `TypeError: missing 1 required keyword-only argument`;`grep 'expected_transaction *: *int *| *None *= *None' src/` 零命中;`modbus_rtu.py` 零改动;全量 `754 passed / 8 skipped · 762 collected`
  ⑤ 四问:红了 → 新写一个不传参数的调用即红(守卫用例参数化成 3 条);绕过 → 必填让"忘传"不可编译/不可调用;退休 → 参数会随 MBAP 协议一直在
- 裁定记要:leader 选**必填**而非"不传时报警" —— 报警在几百条输出里等于没有;规矩是让"省略即失效"不可能发生。
