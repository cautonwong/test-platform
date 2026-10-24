# 13b `teardown()` 在证据记录抛错时跳过清理

Status: resolved

## 复现(改前,独立脚本)

```
记录前连接已建立: True
teardown 抛出: 证据盘满:record_round 抛错
台架连接还在: True              ← 没断开
证据句柄没关(文件删不掉): PermissionError
另一台"断开也失败": 台架参数复位跑了吗: 没跑 / platform-errors.jsonl: 不存在
```

## Comments

- 2026-10-24 合入 `1722087`。**leader 自验**:`pytest -k teardown` → 3 passed;`composition/root.py:438` 注释与 `finally` 结构确认(清理在 finally 里、永不外抛);master 全量 **1077 passed / 12 skipped**。
- 修后:`台架连接还在: False`、`证据句柄已关(文件可删)`;**两条错误各记一条**,`nodeid` 精确匹配(`teardown` / `teardown:meter_a:disconnect`),不是"至少一条"。
- 变异(实现方实测):把 `finally` 挪出 → `test_both_teardown_failures_are_recorded_and_the_original_is_raised` 红。**守卫钉的是"清理必须在 finally",不是注释。**
- **越界确认(leader 批)**:修改点在 `composition/root.py::teardown`(全仓唯一 `def teardown`),不在票面的可动清单里 —— **票面写错了位置,不是实现越界**。
- **格式决定(leader 拍)**:清理失败继续用既有 `platform_error`,`device: null`、设备名写进 `nodeid`;**不新增 kind**。理由:`nodeid` 已能区分,新增 kind 要连带改报告、契约计数与文档,收益不足。
- 主次判据(写进纪律):**主错误 = 最接近用户意图的那个(记录失败);清理失败是附带,必须可见但不许顶掉主错误。**
