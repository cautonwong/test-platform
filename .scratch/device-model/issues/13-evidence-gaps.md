# 13 证据层三个洞:失败不落盘、只认字节流、不记模拟器/真机

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 票 6 的验收项是“失败用例必定留下证据”和“证据里能看到原始报文与解码读数,能据此复现结论”。按现在的实现,这两条在最需要它的三种场景下都不成立。三个洞都能在 `reporting/evidence.py` 一处收掉。

## 1. 失败的收发不落盘(最严重)

`evidence.py:158-171` 的 `RecordingTransport`:

```python
def send(self, data): self._session.record(...); self._inner.send(data)   # 先写再发
def receive(self, timeout): data = self._inner.receive(timeout); self._session.record(...)  # 先收再写
```

`receive` 先调内层、成功了才写。所以**设备超时、链路断开、回帧被拒**这三种最需要证据的失败,证据文件里只留下一条 `send`,看不出“根本没收到应答”。

对“设备不响应”的现场排障来说,这正是关键信息:是没回、还是回了但解析失败?现在两者在证据里长得一样(都只有一个 send)。

期望:`receive` 抛异常时也写一条记录,带上失败种类(超时 / 断开 / 其它)与已等待时长,形如:

```json
{"kind": "frame", "direction": "receive", "error": "timeout", "timeout": 2.0}
```

异常本身照常向上抛,行为不变,只是多写一行。

**注意:这条改法会与现有用例冲突,不能当纯增量做。**`tests/test_mock_meter_end_to_end.py:118-147` 的 `test_mock_timeout_fails_loudly_instead_of_returning_a_stale_value` 把现状锁成了期望:

```python
assert [r["direction"] for r in records if r["kind"] == "frame"] == ["send"]
```

改完之后这里应该是 `["send", "receive"]` 且后一条带 `error: timeout`。那条用例的其余断言(抛 `TimeoutError`、没落盘读数、耗时 < 5s)不变。

## 2. 证据层只认字节流,非字节流设备会被静默包成坏对象

`RecordingTransport` 只实现了 `ByteStreamTransport` 的五个方法(`connect` / `disconnect` / `is_connected` / `send` / `receive`),没有 `__getattr__` 转发。而 `composition/root.py:260-272` 的 `_driver_transport` 对**所有** `role: dut` 的传输无条件 `evidence.wrap(...)`。

`transport/interfaces.py` 里还有四种传输形状:`CanTransport`(`send_frame` / `receive_frame`)、`HttpTransport`(`request`)、`SshTransport`(`exec_command`)、`MqttTransport`(`publish`)。

今天没炸,只因为 `manifest.PROTOCOL_ACCESS` 里的三个规约都落在 `tcp` / `serial` / `gateway_ssh` 上,永远走字节流。一旦加 CAN/HTTP 设备(可选依赖里已经有 `python-can` / `requests`),故障点是**装配后的第一次连接**:`RecordingTransport.connect()` 直接转调 `inner.connect()`,而 `interfaces.py:65-75` 的 `HttpTransport` **连 `connect()` 都没有** —— 报 `AttributeError: 'RealHttpTransport' object has no attribute 'connect'`;CAN 设备则是 `send_frame` 缺失。装配期不报,而且现场表现是“驱动写对了却连不上”,最难查的那种。详见 `14-real-hardware-transports.md` 第 3 条。

期望:二选一,都在装配期解决。

- (a) 最小:开记录时校验传输形状,不是字节流就报错,消息说明“证据层目前只支持字节流规约”——诚实,不假支持。
- (b) 更好:`RecordingTransport` 未知属性一律转发给内层(`__getattr__`),同时对每种形状各实现一层记录包装。工作量大得多,等真的有 CAN/HTTP 设备再做。

**推荐 (a)**。现在的规格里没有任何非字节流设备,(a) 一句话加两个断言就够。

## 3. 证据不记这一轮是模拟器还是真机

`evidence.py` 里没有任何 `mode` 字段。`composition/root.py` 明明有 `actual_modes`,但 `record_round` 没往下传。

后果:一份证据文件无法自证“这条结论是在模拟器上跑出来的”。真实台架的验收记录和 CI 里 `--force-simulator` 跑出来的记录长得一模一样。别人拿这份证据去交给第三方(或半年后的自己)时,分不出结论的分量。

期望:`reading` 记录里加 `"mode": "real" | "simulator"`,来源就是组合根已经算好的 `actual_modes[device_id]`,不新增任何判断逻辑。

## 验收

- [ ] `receive` 抛异常(超时 / 断开)时,证据里多一条带 `error` 的帧记录;异常照常抛出,用例行为不变
- [ ] 有单测:一个 `receive` 必超时的假传输 → 证据文件里 `send` 之后有一条 `error: timeout`
- [ ] 开记录时包非字节流传输(占位 `CanTransport` 即可)在装配期报错,消息点明只支持字节流规约;有单测
- [ ] 每条 `reading` 记录带 `mode`,真机与模拟器两种取值都有用例覆盖
- [ ] 默认套件除新增用例外仍是 `181 passed, 6 skipped`;关闭记录时行为仍不变(`test_turning_recording_off_leaves_assembly_and_readings_unchanged` 保持绿)

## Comments

- 2026-10-20 出票依据:只读审 `a82af5d` 的 `reporting/evidence.py`(222 行)。三条都是读代码确认的,不需要真实硬件;第 1 条的对照可直接看现有用例 `tests/test_evidence.py:395`(崩在收尾之前仍留证据)——那条覆盖的是“崩在写证据之前”,不覆盖“收不到应答”。
- 与 `12-closeout-quality.md` 无关,可以并行做;三处都只动 `reporting/evidence.py` 与 `composition/root.py` 的传参。
- 13 已完成:b15fc59 / 合入 be9673f。超时/断开的收发也落盘(带 error 与已等待时长);非字节流传输在装配期被拒;reading 记录带 mode。191 passed / 7 skipped。
