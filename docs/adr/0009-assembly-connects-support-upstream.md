# ADR 0009:装配期连接 support 及其上游 dut

状态:接受(2026-10-24)
来源:票 13c。原约定是「装配期只连 support,被测设备的连接留在用例里」。

## 背景

支撑设备可以声明 `via: <dut>`(经某台被测设备访问,例如电表经采集终端接入)。清单层**已经**做 via 拓扑排序与环检测(`config/manifest.py`),但装配期 `connect_support()` 只迭代 support 角色,`via` 指向的 dut 从未被连接 —— 于是每条用例都报「周边设备连接失败: terminal_01 状态 DISCONNECTED」,**读起来像设备坏,实际是上游没通**。

## 决定

**装配期连接 support 及其 `via` 上游链**(`get_ready(device_id).connect()`),并且:

1. **只连"被 support 依赖的 dut"** —— 不因为装配就连接其它被测设备
2. 上游未就绪 → 抛 **`SupportUpstreamNotReady`**(`SupportEquipmentFault` 子类),消息必须写明
   「上游没就绪(接入通路没通,不是设备本身故障)」
3. `via` 只能指向 `role: dut` —— 加载期拒绝 support→support 的 via(票面字面形状不可表达)

## 后果

- **语义变更**:装配期不再"只连 support",而是"连 support 及它依赖的 dut"。既有断言仍绿(那台 dut 没有下游 support),但这条契约必须以 ADR 记录,**否则下一个人会当 bug 改回去**。
- 归因不变:支撑设备连不上仍判「测试无效」,但消息区分"上游没通"与"设备坏"。
- 环检测保持在清单层,装配期不重复实现。

## 替代方案与否决理由

| 方案 | 否 |
|---|---|
| 保持"装配期只连 support",由用例自己连上游 | 每条用用例都要重复这段样板,且忘记写就被归因成"设备坏" |
| 在 `connect_support` 里再实现一套拓扑排序 | 清单层已有,两份实现必然漂(13d 就是"判定写了两份"的教训) |
