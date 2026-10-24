# 51 证据的两个静默缺口:超出窗口的部分无声消失、时间戳无时区

Status: resolved
Type: task
Blocked by: None — 两条都是"证据可信"的同一件事

**What to build:** 两条都会让证据"看起来完整、实际不可用"。第二条(`49-D`)上轮体检就提了,一直没做,而且裸 `datetime.now()` 从 5 处变成了 7 处。

## A. 取增量时超出窗口的部分静默丢失(3 行改动)

`reporting/evidence.py:331-338`:

```python
recorded = getattr(device, "readings_recorded", 0)
fresh_count = recorded - self._recorded_readings.get(device_id, 0)
history = getattr(device, "history", ())
# 已有注释:两轮之间记了超过一个窗口时,已裁掉的补不回来,能取多少取多少
fresh = history[max(len(history) - fresh_count, 0):]
self._recorded_readings[device_id] = recorded
```

`history` 是 1000 条窗口。若两轮之间记了 >1000 条,超出部分不会报错、不会告警,直接不写证据。**注释承认了这个限制,但没有任何可见痕迹。**

期望:发现缺口时写一条

```json
{"kind": "evidence_gap", "missing": 5, "window": 1000, "note": "两轮之间有 1005 条读数,已裁掉最旧的 5 条"}
```

**并且归因必须认这个 kind**(它现在对未知 kind 是静默忽略 —— 那就等于又看不见了)。归到「证据损坏」一类,责任方是写证据这一侧,不是设备。

## B. 所有时间戳都是无时区本地时间(7 处)

```
domain/battery.py · inverter.py · meter.py · terminal.py(读数 timestamp)
domain/device.py(连接时间)
reporting/evidence.py ×2(会话目录名、记录的 at 字段)
```

两台台架各跑一轮、把证据并到一起时,`timestamp` 与 `at` 无法排序、判断不出先后。

期望:`datetime.now(timezone.utc)` 或 `.astimezone()`,序列化保留偏移。

**这是数据格式变更,不是内部实现变更**:
- 读的路径要容错:旧证据的 `at` 没有偏移,按本地时间理解
- 写的路径一律带偏移
- 在证据格式说明里写一句"旧证据无偏移"的已知限制

**不要动** `_receive_frame` 里的 `time.monotonic()`:超时用单调钟是对的,和"记录绝对时刻"是两个需求。

## 验收

- [x] 构造"两轮之间记 1005 条"的用例:证据里出现 `kind: evidence_gap` 且 `missing == 5`;归因把它列在「证据损坏」并写明少了几条
- [x] 正常情况下(少于窗口)不写 gap 记录(否则又是噪声)
- [x] 新读数与证据 `at` 带偏移(`+08:00`);旧的无偏移记录仍能读、仍能排序(以本地时间理解)
- [x] 现有断言时间戳格式的用例相应更新
- [x] `time.monotonic()` 未被改动(有 grep 或断言证明)
- [x] 基线:682 passed / 7 skipped,规模 1786/109 —— 数字变化只能是新增用例,差值要能解释

## Comments

- 2026-10-20 出票依据:第二轮 ponytail-audit 的 finding 1 与 finding 2;`history` 窗口 1000 与 `readings_recorded` 游标是 47 合入的,本票补它留下的可见性缺口。

- 2026-10-22 已完成 `2c69f15` / 合入 `cd8bd04`。**687 passed / 7 skipped · 规模 1791 passed / 109 skipped**,用例差值 +5。
- A:`evidence_gap` 写在读数之前(`{"missing":5,"window":1000}`);恰好 1000 条**不写**(反向断言在);归因归「证据损坏」并**显示缺几条**,设备不合格/平台错误均为空。
- B:时间戳带偏移;跨台架合并**必须解析而非字符串排序** —— 实测字符串排序会把 `07:23Z` 排在 `14:30+08:00` 前面,而两者实际差近 9 小时;**旧格式(无偏移)仍可读且参与排序**。
- `grep datetime.now()` 零命中;`time.monotonic()` 原样(超时判断未被动)。
- 用例纯内存造(直接打领域层 1005 次,不走传输),毫秒级,未 monkeypatch 生产常量。
