# 13a `EventSink.emit` 抛错 → 事件永久丢失

Status: resolved
来源:clawpatch 切片审查(provider=pi)。**先复现再修。**

## 线索

`drain_device_events` 期间若 `EventSink.emit` 抛错,已从设备读到但尚未落盘的事件会丢失 —— 而事件是归因的输入之一(设备进故障态、链路断开等)。

## 要求

1. 构造 emit 抛错(注入一个会抛的 sink)→ 贴**丢了几条、丢的是什么**
2. 修:emit 失败**不许静默吞**;要么重试一次,要么结论落到具名状态(`evidence_gaps` 或 `unexecuted` 家族),让人看得见
3. 守卫:注入抛错的 sink → 断言**报告里能看出"事件没落盘"**,不是 0 条静默
4. 判据:**丢失必须是可数的**("丢了 3 条 event"比"可能丢"有用)

## Comments

- 13a 已合入 d15e1c8:EventEmitFailed 带丢失条数与事件名;丢的倍数关系在复现里可见。
