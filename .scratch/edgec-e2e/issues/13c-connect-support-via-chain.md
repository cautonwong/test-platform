# 13c `connect_support()` 忽略 `via:` 链

Status: resolved
来源:clawpatch 切片审查。**先复现再修。**

## 线索

支撑设备声明了 `via:`(经某台上游设备访问,如 DUT 的采集终端),但 `connect_support()` 不看这个字段 → 上游还没连上时,支撑设备直接失败;失败还会被归成"测试无效"而不是"顺序错"。

## 要求

1. 复现:两台设备 A(via: DUT)与 DUT → 贴**连接顺序与错误原文**
2. 修:按 `via:` 拓扑排序连接;**环检测**(A via B、B via A)→ 加载期报错
3. 守卫:via 链两台 → 顺序正确;**via 成环 → 加载期报错**,不许跑到一半
4. 归因:连不上的支撑设备判「测试无效」,但消息要指出**是上游没就绪**,不是设备坏

## Comments

- 13c 已合入 49c9942:装配期连 support 上游 dut(get_ready)+ SupportUpstreamNotReady;ADR 0009。
