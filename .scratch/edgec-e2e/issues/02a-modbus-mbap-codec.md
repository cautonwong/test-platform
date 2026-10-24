# 02a MBAP 编解码与模拟从站帧格式参数化

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** EdgeC 产品走 **Modbus TCP(MBAP,7 字节头)**,不是 RTU。本票只做**纯函数与注册**,不涉及任务执行:MBAP 编解码、字节向量、同一份模拟从站按帧格式参数化(RTU / MBAP 共用寄存器表与故障注入)、协议能被清单接受。

端到端(假产品走 MBAP 跑通一个任务)拆到 02b。

## 验收

- [x] MBAP 编解码是纯函数:读请求、读应答、写单寄存器、异常应答各有字节向量
- [x] 长度字段/事务号算错必须红
- [x] 模拟从站可按帧格式切换,两种格式共用寄存器表与故障注入(不复制实现)
- [x] 帧格式不匹配(RTU 帧喂给 MBAP 解析)报错可辨认,不静默解出值
- [x] 同一寄存器表在两种格式下行为一致(有对照用例)
- [x] 清单能写 `protocol: modbus_tcp` + `access: tcp`;`access: serial` 装配期拒绝
- [x] 现有 RTU 用例一条不改、全绿
- [x] 基线不回归(数字差值可解释)

## Comments

- 2026-10-23 合入 `ee52c9b`(交付 `a8806b5`)。leader-0 复跑:`738 passed / 7 skipped · 745 collected`;`tests_unit/test_modbus_tcp_codec.py` + `test_modbus_tcp_frames.py` + `tests/test_meter.py` 共 76 passed。
- 五条重点核过:①`configs/devices.yaml` 与 `tests/` 未被改(`git diff --name-only` 零命中);②寄存器读写各只有一处(`_read` / `_write_single_register` / `_write_multiple_registers`);③`modbus_tcp + serial` 装配期拒(`该规约允许的接入方式: ['tcp']`);④RTU 帧喂给 MBAP 解析报错点名"这是 RTU 帧,没有 MBAP 头";⑤驱动仍是 `PointTableDriver`,块规划/降级/解码只有一份 —— 引入的 `codec` 参数只换帧层,读表层没分叉。
- **审查发现一个 Must fix,另开 `02c`**:MBAP 事务号只发不验(`parse_read_response` 把事务号丢弃),迟到应答会被当成当前应答。
