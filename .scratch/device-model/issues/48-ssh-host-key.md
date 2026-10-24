# 48 SSH 接受任意主机密钥(安全,Should fix)

Status: resolved
Type: task
Blocked by: None

**What to build:** 经工控机网关接真机时,SSH 不该无条件信任对端。

## 现状

```python
# transport/real_transports.py:346
self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
```

任何主机密钥都接受。台架网络里有人冒充网关时,平台会把别人的回帧当设备数据,**证据里完全看不出**。

## 期望

二选一(推荐前者):

- (a) `RejectPolicy` + 显式 `known_hosts` 路径(清单 `connection.known_hosts`),首次连接失败时给出"如何登记指纹"的提示
- (b) 清单里可选的 `host_key_fingerprint`,装配期校验格式,连接后比对;不匹配 → `ConnectionError` 并点明期望/实际指纹

**不要求**默认更严格到破坏现有清单:未配置时保持当前行为,但要在文档里写明"未配置 = 不校验主机密钥"。

## 验收

- [x] 配置了 known_hosts / 指纹时,不匹配 → 连接失败,消息含期望与实际
- [x] 未配置时行为不变(现有网关用例仍绿)
- [x] `docs/how-to/run-on-real-hardware.md` 补一节:怎么登记网关指纹
- [x] 有假 paramiko 注入的用例(不连真机)
- [x] 基线不回归

## Comments

- 2026-10-20 出票依据:ponytail-audit 体检项 5。没有硬编码凭据(已 grep),这条只关乎主机密钥。

- 2026-10-22 已完成 `5a2092d` / 合入后 **685 passed / 7 skipped**。校验放进 `_VerifyHostKeyPolicy.missing_host_key` 钩子(不在 `connect()` 里跟 `exec_command()` 的路径上,否则每次命令重算、超时/重连可能绕过)。
- 三态 ×(直连 / 经网关)**各有用例**;"未配置"那条断言的是**真的连上了**(`has_client() is True` 且 `exec_command` 有输出),不是"没抛异常"。不匹配时报错含期望指纹 + 实际指纹 + `ssh-keyscan` 登记命令。
- **补了一条 leader-0 指出的缺口**:断言交给 `set_missing_host_key_policy` 的**策略类型**是 `_VerifyHostKeyPolicy` —— 假 paramiko 不自己校验,若实现把 policy 设成别的、校验写在别处,三态仍会全绿。
- `gateway_known_hosts` 与直连 `known_hosts` 同一套语义、同一套装配期校验(路径存在性,不读内容)。
