# 14d 真 paho 打本地 broker 的集成用例(14c 的覆盖缺口)

Status: ready-for-agent
Priority: low(不阻塞任何票)
来源:14c 实现方主动交代的弱点。

## 缺口

14c 的用例用**假 client 在 `connect()` 里同步回调 `on_connect`** —— 于是"等 CONNACK"与"不等"在正常路径上**看不出差别**(变异能红,只证明等待那行承重)。

**真 paho 的 `on_connect` 在网络线程里回调**,所以真实路径下 `answered.wait(timeout)` 的行为**没有被覆盖**。

## 要求

1. 起一个本地 MQTT broker(如 `amqtt` / `mosquitto` 容器 / 极简 asyncio broker),标记 `@pytest.mark.slow`,CI 默认跳过
2. 三条真路径:
   - 能连上 → `connect()` 成功,之后的读正常
   - broker 不在 → `MqttBrokerUnreachable`,**在 `connect()` 就抛**
   - 要认证但没给账号 → rc≠0 → 同一异常,消息含 rc
3. 判据:**不许用 sleep 等"大概连上了"**;断言的是 `connect()` 的返回/异常,不是时间
4. 若本地 broker 起不来(依赖缺失),用例**必须 skip 并写清原因**,不许静默通过

## 边界

不改 14c 的类型层次(`MqttBrokerUnreachable(ConnectionError)` 是责任方的表达方式,**不许改成 TimeoutError**)。
