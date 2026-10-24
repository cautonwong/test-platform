# 28 纯等待已交付(`settle`);缺的是「台架跑 N 秒」——期间要能证明台架一直在跑

Status: resolved
Type: task
Blocked by: None

**已交付部分(2026-10-20)**:`6b83e8d` 加了 `settle(seconds, reason, *, confirm_long)`,
`ScenarioStep.device` 放宽为 `str | None`,`verdict_of` 不再对无设备的步骤查 participants ——
纯等待已经可用,包括长时长的显式确认。

**本票剩余部分:** 上游走字条款的动作是"台架按设定输出 X7 秒 → 读表 → 判增量"。我们场景层现在只有"动作 → 立刻读"，没有任何时间原语：既不能等待固定时长，也不能表达"这段时间里台架一直在输出"。ADR 0005 明确拒绝了"等待/轮询到条件"，但**固定时长的 `settle` 与它拒绝的东西不是一回事** —— 要按 ADR 的原意补，而不是开轮询的口子。

## 现状(交付后)

```python
# reporting/scenario.py
def settle(seconds: float, reason: str = "", *, confirm_long: bool = False) -> ScenarioStep
```

只解决"等待 N 秒"。**"台架跑 N 秒"还没有**:`settle` 期间不读任何设备状态,所以无法证明台架在这段时间里一直在输出。走字类条款(29 条)判的是增量,而增量除以时间才是功率 —— 时间段的**有效性**没有落盘,结论就不可复算:台架中途跳闸,报告里和"跑了满 60 秒"长得一样。

## 期望

在 `settle` 之外补一个带设备的版本(名字随意,建议 `hold`):

```python
step = scenario.hold(60.0, device=grid, reason="走字")
```

- 开始时读一次该设备状态(如 `setpoint`),结束时再读一次,**两次都在设定上才成立**
- 可选的周期性采样(`samples` / `max_gap_s`)证明期间没跳闸;采不到就只做 start/end 两次
- 落盘:`target_s` / `actual_s` / `start` / `end` / (可选)`samples` / `max_gap_s`
- 异常路径也要读 end 一次并落盘,否则最需要证据的那次失败什么都没有
- `hold` 期间**不读被 hold 的设备**(概念上"台架在跑"与"读表"是两件事)

## 不做(仍然是 ADR 0005 的立场)

| 不做 | 理由 |
|---|---|
| `wait_until(条件, timeout)` 轮询 | 结论依赖机器速度 |
| 时序容差断言("60±2 秒内必须完成") | 同上,先攒时序证据 |
| 并发 + 时序先后 | 需要时钟同步才有意义 |
| 在 hold 里跑驱动动作 | 分不清是等错了还是读错了 |

## 验收

- [ ] `hold(seconds, device, reason)` 可用,场景 json 里带 `start` / `end` / `target_s` / `actual_s`
- [ ] 期间人为让台架掉线 → 这一步 `passed=False`,且细节里能看出是 end 状态不符
- [ ] 异常路径下 end 状态仍落盘
- [ ] 不引入任何轮询或时序容差语义
- [ ] 一条走字端到端:读数 ×2 → hold → 读数 ×2 → 增量 > 0
- [ ] 全量套件仍全绿,默认套件不因此变慢(新用例用秒级)

## Comments

- 2026-10-20 出票:上游 `runWord(..., seconds=X7)`;含 `runWord` 的条款 29/659。
- 2026-10-20 更新:`settle` 已合入(`6b83e8d`),`ScenarioStep.device` 与 `verdict_of` 的空设备处理也一并做了 —— 那正是本票原稿里点出的 KeyError 陷阱。本票收窄为"带设备状态的 hold"。
