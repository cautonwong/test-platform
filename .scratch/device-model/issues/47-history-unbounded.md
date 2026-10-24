# 47 `history` 无界增长,而证据层依赖它不裁剪(Should fix)

Status: resolved
Type: task
Blocked by: None

**What to build:** 每台设备的历史读数要有上界;证据层的"增量"不能靠 `len(history)` 算。

## 现状

```python
# domain/inverter.py:53,74  domain/battery.py:45,66
self._history: List[PowerReading] = []
self._history.append(reading)

# reporting/evidence.py:331-333
history = getattr(device, "history", ())
fresh = history[self._recorded_readings.get(device_id, 0):]
self._recorded_readings[device_id] = len(history)
```

`_history` 只增不减。而证据层用"上次长度"当游标 —— **一旦裁剪,游标错位,证据会漏记或重记**。

实测单条读数约 **232 字节**:265 台 × 4 小时(每 5 秒一条)≈ **169 MB**,8 小时翻倍。

## 期望

1. `_history` 设上界(如最近 1000 条),超出丢弃最旧的
2. 证据层的游标改成**独立计数器**(`已写条数`),不再用 `len(history)`
3. 裁剪必须在"证据已取走"之后发生,或者干脆由证据层主动拉取(拉模式),不依赖历史长度

**顺序要求**:先改游标,再开裁剪。反过来会有一段"证据漏记"的窗口。

## 验收

- [x] 连续 2000 次读数后,`history` 长度 ≤ 上界
- [x] 同一轮里证据条数与读数次数一致(裁剪前后各一条用例)
- [x] 裁剪后 `latest_reading` 仍是最后一次(不变)
- [x] 基线不回归

## Comments

- 2026-10-20 出票依据:ponytail-audit 体检项 4;内存数字用 `sys.getsizeof` 实测单条 232 字节推算。
- 2026-10-22 已修(A 方案:保留最近 N 条,N=1000):`Device` 用 `deque(maxlen=HISTORY_WINDOW)` 存 history,`readings_recorded` 只增不减、当证据游标;`len(history)` 断言全部改成计数器,绝对下标访问零命中(仓库里只有 `history[-1]` 取最新)。全量 668 passed / 8 skipped(同环境 HEAD 基线 665 / 8,新增 3 条用例)。commit b3d69e6。

- 2026-10-22 已完成 `b3d69e6` / 合入后 **669 passed / 7 skipped**,规模清单 1746 passed / 109 skipped。
- 策略 A(保留最近 N 条),四件齐:`readings_recorded` 单调计数器、`history` 变 `deque(maxlen=1000)` 窗口(语义写进 `CONTEXT.md`)、**证据游标改用计数器差值**、`len(history)` 断言全改。
- **先改游标再裁剪的顺序被验证过**:临时还原旧游标(`len(history)`)跑 1005 条读数 → 只落 1000 条证据(`assert 1000 == 1005` 红);新游标 → 1005 条。这条证明"反过来会漏记"不是理论。
- `history[` 绝对下标只剩 `[-1]` 三处(`latest_reading` 语义,窗口下仍正确)与测试里一处 `history[-1]`,**没有 `history[0]` 类访问**。
