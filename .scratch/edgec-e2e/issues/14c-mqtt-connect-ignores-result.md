# 14c `RealMqttTransport.connect` 忽略 paho 连接结果 → 连不上也报"可用"

Status: ready-for-agent
来源:clawpatch low 项。**归因后果严重:连不上会被判成别的故障。**

## 线索

`connect()` 发起 paho 连接后不检查结果/不等待 `on_connect` → 返回"成功",后续读写才失败;于是**归因把"根本没连上"读成"读超时/设备坏"**。

## 要求

1. **先复现**:对一个不存在的 broker 调 `connect()` → 贴返回值与后续第一条读的错误
2. 修:`connect()` 必须等到 `on_connect`(带超时),失败则抛**具名异常**;消息含 broker 地址与 rc
3. 守卫:不可达 broker → `connect()` 直接失败(**不是**等到读才失败);可达 → 成功
4. 与 13c 同族:连不上必须说"没连上",不许让下游把它读成"设备坏"

## 边界

不改证据格式;真实 broker 不可用 —— 用本地临时 MQTT broker 或 mock 的 `on_connect` 回调,贴清用了哪种。
