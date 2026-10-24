# 13a2 事件丢失要落到 `evidence_gaps`(报告里可见,不只是 teardown 抛 ERROR)

Status: resolved
Depends: 13a 已合入(d15e1c8,`EventEmitFailed` 带丢失条数与事件名)
来源:leader 在 13a 交付时拍的"补一半"——teardown 抛 ERROR 对开发可见,对**报告**不可见。

## 落点(上一位实现方已确认,不必重查)

- 报告读 `evidence_gaps`:`reporting/report.py` 的 `_untested(...)` 里那条 `gaps` 列表(约 line 398)
- `round-status.json` 是现成的机器可读通道(已有 `verdict` / `link_lost` 先例),**不需要新文件**
- ⚠️ 坑:`report.py` **没有 import** `ROUND_STATUS_FILE`(它只从 attribution 拿),插入前先
  `grep -n ROUND_STATUS_FILE src/testplatform/reporting/report.py` 确认,要么补 import,要么用字符串常量

## 建议形态(上一位验证过可行,未提交)

```
EvidenceSession.record_event_emit_failure(device, *, missing:int, reason)
    → 存 self.event_emit_failed;write_round_status() 写进 round-status;
      并把 verdict 从 clean 改 needs-review
report._event_emit_gaps(directory)
    → 读 round-status,产出 {kind:"event_emit_failed", device, missing:int, reason,
                            ref:"round-status.json"}
tests/conftest.py::_drain_device_events_after_test
    → except EventEmitFailed:有证据会话才写(单测注入的假 sink 不碰真会话),然后 raise
```

## 三条硬要求

1. **只记真实失败**:单测注入的假 sink 不许写进真证据会话(否则测试污染别人的报告)
2. **干净跑必须 0 条**:一条断言证明正常轮次不会冒出这个 gap
3. 与既有 `evidence_gaps` 家族同口径:`missing` 是**条数**,不是布尔;`kind` 用闭集值

## 交接说明

上一位实现方上下文耗尽,已 `git revert` 到 `d15e1c8`、工作树干净、**无半截分支**。
接手者从 `d15e1c8` 新开 worktree 即可,不要去找旧分支。

## Comments

- 13a2 已合入 b4c8a3b:round-status verdict 转 needs-review,gap 带 missing 条数;干净跑 0 条。
