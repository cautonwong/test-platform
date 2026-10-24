# 30 用例失败后台架不复位:一旦有了写原语,一次失败会污染后面所有用例

Status: resolved
Type: task
Blocked by: None — 但必须在 `26-write-primitive.md` 合入前做

**What to build:** `CompositionRoot.teardown()` 只做两件事:补一次证据、逐台 `disconnect()`。**没有任何"把台架/设备恢复到初始状态"的动作**。现在只读,所以无害;一旦 `26-write-primitive.md` 上线(写参数、进工厂模式),一次用例失败就会把设备留在非初始状态,**后面所有用例的读数都是错的,而且报告看不出原因**。

## 现状(实测)

```python
# composition/root.py teardown()
if self.evidence is not None:
    self.record_evidence(...)          # 补证据
for driver in self.drivers.values():
    driver.disconnect()                # 只断开
```

`grep -rn "reset|restore|还原|复位" src/` 只有串口缓冲区的 `reset_input_buffer`,没有任何"回到初始状态"的语义。

场景用例也没有 `try/finally`:台架设在 230V/50Hz,用例中途抛异常,台架**继续按 230V/50Hz 输出**到整轮结束。

## 为什么现在就要修

上游靠人工在每条用例里写收尾动作(`meter.universalSet(..., 进入/退出工厂模式, "F001")`,出场/退场成对出现)。我们马上要迁 527 条设置类条款,其中大量是"进工厂模式改参数 → 退出"。若"退出"这一步没跑:

1. 设备停在工厂模式 → 生产/计量相关读数全错
2. 下一个用例读到错值 → **判成"设备不合格"**,而真正原因是前一条用例没退出来
3. 归因四类里没有"被上一条用例污染"这一类,查不出来

这是"结论归属"问题,和 `4-周边设备`(周边故障 = 测试无效)同一层。

## 期望

**按 role 分层,不要一个笼统的 `reset()`:**

| 角色 | 收尾要做什么 | 失败时结论 |
|---|---|---|
| 被测设备 `dut` | 只做**用例自己声明的**收尾动作(如"退出工厂模式"),平台不替它猜 | 收尾失败也算这条用例失败,并在证据里标 `cleanup_failed` |
| 周边设备 `support` | 恢复到清单声明的初始设定(如 `initial_setpoint: {voltage_v: 230, freq_hz: 50}`) | 恢复失败 → 测试无效,不是设备不合格 |
| 平台自身 | 补证据 + 断开(现状保留) | — |

清单形状(周边设备):

```yaml
- id: grid_sim_01
  model: grid_simulator_3ph
  role: support
  initial_setpoint: {voltage_v: 230, freq_hz: 50}   # teardown 恢复到这里
```

用例侧给一个显式的收尾注册口(不是隐藏的魔法):

```python
scenario.on_cleanup(lambda: meter.write("进入/退出工厂模式", "F001"))
```

它必须在**正常路径与异常路径都执行**,执行结果落进 `scenario-*.json` 的 `cleanup` 字段。

## 不做

- 不做"平台自动猜该恢复什么" —— 被测设备的收尾只有条款知道
- 不在 `teardown()` 里对被测设备做任何写操作:整轮结束时设备被人动过,比留在工厂模式更难查
- 不把收尾失败混进"设备不合格":它是流程问题

## 验收

- [x] 周边设备按 `initial_setpoint` 恢复;恢复失败 → 该轮结论为测试无效
- [x] 用例注册的 cleanup 在**正常与异常两条路径**都执行,结果写进 `scenario-*.json`
- [x] cleanup 自身抛异常时:不掩盖原来的异常,两者都进证据
- [x] 一条端到端:用例把台架设到 0V 后抛异常 → 结束时台架回到 230V/50Hz,下一个用例的读数不受影响
- [x] 全量套件仍全绿;现有场景用例行为不变

## Comments

- 2026-10-20 出票依据:`composition/root.py` 的 `teardown()` 全文;`grep 复位|还原|restore` 在 `src/` 只命中串口缓冲;`tests_scenario/test_grid_frequency.py` 无 `try/finally`。
- 与 `26-write-primitive.md` 的关系:**26 让平台能写,30 保证写过的世界能回到原样**。26 可以先合入(单寄存器写 + 立刻读回,失败的写不会改变状态),但 527 条设置类条款的迁移必须等 30 落地。

- 2026-10-22 已完成 `e258618` / 合入后 461 passed / 6 skipped。按 role 分层:support 按 `initial_setpoint` 恢复(驱动须实现 `apply_initial_setpoint`,否则**装配期**报错);dut 只做用例 `scenario.on_cleanup(...)` 声明的收尾,平台在 teardown 期间**不向 dut 写任何帧**(用证据里的 send 序列断言)。cleanup 在正常与异常两条路径都执行;收尾自身抛异常不顶掉原异常,两者都进 `scenario-*.json` 的 `cleanup` / `cleanup_failed`。
- **一处约束是我改错的**:我原来要求"会话级恢复失败必须抛 `SupportEquipmentFault`(不静默)"。实测发现 pytest 里后一个 finalizer 抛异常会**盖掉前一个的 Skipped**,于是最该被看见的"测试无效"被 ERROR 吞掉、退出码从 0 变 1。改为记 `link_lost(operation=apply_initial_setpoint)` → `round-status` 落 `needs-review`。**结论归属被覆盖,比"不够严格"更糟。**
- 文案按 operation 区分:复位失败不写成"断链"(排障方向相反)。三种情况各有用例;**"两者同一轮"这一支是我在合入后自己补的守卫**(`tests_unit/test_attribution.py::test_round_note_names_both_*`)。
