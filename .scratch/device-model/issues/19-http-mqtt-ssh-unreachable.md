# 19 HTTP / MQTT / SSH 三个传输在清单里不可达

Status: resolved
Type: task
Blocked by: None — 但优先级低于真实性缺陷:今天没有这类设备

**要做什么:**`transport/real_transports.py` 已经实现了 `RealHttpTransport` / `RealMqttTransport` / `RealSshTransport`,并且在 `transport/registry.py` 里注册为 `http` / `mqtt` / `ssh`,但 `config/manifest.py` 的 `PROTOCOL_ACCESS` 里**没有任何规约放行这三种 access**。结果:这三个类从清单里根本走不到,只能被单测直接构造。

和 CAN 在 `issue/14b` 之前的状态一模一样 —— 校验/能力都写好了,入口没开。

## 现状(实测)

```python
PROTOCOL_ACCESS = {
    "vendor_ascii": {"tcp", "serial", "gateway_ssh"},
    "dlt645":       {"serial", "gateway_ssh"},
    "modbus_rtu":   {"serial", "gateway_ssh", "tcp"},
    "canopen":      {"can"},
}
```

清单里写 `access: http`(或 `mqtt` / `ssh`)会在"规约 × 接入方式"配对校验处被拒,报的是「未知/不允许的组合」,而真正的原因是**没有规约声明支持它**。

## 建议做法

1. 先确认方向:HTTP / MQTT / SSH 分别对应哪一类**规约**?
   - HTTP:设备自带 REST 接口(部分 EMS / 网关)
   - MQTT:设备走发布订阅上报(部分 HEMS / 云侧)
   - SSH:对主机执行命令(工控机、终端自检)
   这里 `protocol` 与 `access` 不是一一对应,可能一个规约允许多种 access,也可能需要三个新规约。**这一步是设计决策,不要凭猜。**
2. 放行后不要造驱动:装配层以「未找到设备工厂」收场是正确行为(和 `canopen` 现在的状态一致)。
3. 三个族的 `connection` 也要有闭合键集合(参照 `serial` / `tcp` / `gateway_ssh` 在 `issue/14b` 里的做法),拼错不许静默取缺省。

## 验收

- [x] 清单里能配出一台 HTTP / MQTT / SSH 设备,越过清单校验,在装配层以「未找到设备工厂」收场
- [x] 三族各自的 `connection` 键闭合,拼错在 `parse_manifest` 报错并列出可用键
- [x] 现有套件语义不变

## Comments

- 2026-10-21 出票依据:`issue/14b` 收尾时的人工排查(worker 自查报告的第 3 条,我复核确认)。优先级低:今天没有 HTTP/MQTT/SSH 设备,这三个类目前只被单测构造。
- 2026-10-22 收口方向(人工定):**不造规约**。今天没有 HTTP/MQTT/SSH 设备,凭空放行只会得到"能配但装配必失败"的假象。改为把边界显式化:`parse_manifest` 遇到 `access: http/mqtt/ssh` 报"传输已实现并注册,但还没有任何规约声明支持它 + 先定规约再在 `PROTOCOL_ACCESS` 放行";三处类与三个注册工厂各加注释指向该边界;README 记一句"有意保留";`tests_unit/test_real_transports.py` 加守卫用例。因此上面"验收"里的第 1、2 条(清单里能配出 HTTP/MQTT/SSH 设备)本次**有意不做**,将来放行时连同守卫用例一起改。

- 2026-10-22 已完成 `ae3e8ee` / 合入 `79a67e8`。按"不造规约、把边界写明"的口径落地:写 `access: http/mqtt/ssh` 时,报错说清"传输已实现并注册、但没有任何规约声明支持",并给出下一步(先定该设备用什么规约上这种接入,再改 `PROTOCOL_ACCESS` 并补 connection 闭合键)。`PROTOCOL_ACCESS` 一字未改;三处传输类与工厂加注释指向这条边界;守卫用例断言消息可操作且 `(access, "real") in registered_keys()`,不断言"永远不能放行"。
- **票里验收 1、2 有意不做**(放行 + 三族 connection 闭合键):没放行就没有可校验的配置;放行那天和守卫用例一起做。**遗留陷阱**:`_validate_stream_connection` 对这三种 access 仍直接早退,放行时必须同时补键集合,否则拼错的键会静默取缺省。
