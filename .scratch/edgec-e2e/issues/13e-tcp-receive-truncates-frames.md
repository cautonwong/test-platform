# 13e `RealTcpTransport.receive(expected=None)` 只 recv 一次,帧被截断

Status: resolved
来源:clawpatch 切片审查。**先复现再修。**

## 线索

`expected=None`(未知长度帧)时只做一次 `recv`,TCP 是流协议:**一次 recv 拿到的可能只是半个帧**。上层于是解出截断数据或报 incomplete。

## 要求

1. 复现:对着一个**故意分两次发送**的模拟从站跑 → 贴收到的字节与解析结果
2. 修:`expected is None` 时循环读到"帧完整"或超时;有 `expected` 时读满 N 字节
3. 守卫:分片发送 → 断言拼出完整帧;**超时 → 报超时而不是返回半帧**
4. 与 11 号的"EOF 记成 timeout"不同:这条是**正常分片**,不是断链

## Comments

- 13e 已合入(7a421ab→合并):receive(None) 循环读不截断,gap/timeout 三结局可区分。
