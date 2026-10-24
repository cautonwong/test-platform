# 14 真机传输六大隐患,且零测试覆盖

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `transport/real_transports.py`(253 行、6 个传输类)在 `tests*/` 下**一条用例都没有**(`grep -rln "RealSerial\|RealTcp\|RealSsh\|real_transports" tests*/` 空)。模拟器把一切掩盖了:`SimulatedByteStreamTransport` 瞬间返回整帧,所以短读、长帧、大输出、半开连接这些**只在真实台架出现**的问题,现在测不出来,也不会红。

不用等真机。下面六条里除第 6 条外都能用假库(monkeypatch `serial` / `socket` / `paramiko`)锁住 —— 见末尾的“推荐做法”。

## 1. 字节流接收没有帧边界(头号真机杀手)

`real_transports.py:57-59`

```python
def receive(self, timeout: float = 2.0) -> bytes:
    self._ser.timeout = timeout
    return self._ser.read(256)
```

`serial.read(256)` 的语义是“读到 256 字节 **或** 超时即返回”,**不保证一次拿到一帧**。DL/T645 与 Modbus RTU 的应答长度是已知的,所以:

- 应答被拆成两段(常见于 9600 波特的长帧)→ 上层拿到半帧 → 解析失败或解出**看着正常的假读数**
- 帧超过 256 字节 → 静默截断(`read(256)` 不会告诉你还剩多少)

`real_transports.py:91-93` 的 TCP 版本(`recv(4096)`)是同一个问题。

期望:接收按规约收帧,而不是按缓冲区大小。最简的两个方向,选一个:

- (a) 按点表/规约声明的应答长度收满,再返回;短读继续读直到够长或超时
- (b) 至少区分“读到了但不完整”和“彻底没读到”:超时返回的字节数 < 期望帧长时抛明确异常,而不是把半帧交上去

## 2. `exec_command` 可能在输出较大时死锁

`real_transports.py:166-171`

```python
stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
exit_code = stdout.channel.recv_exit_status()      # ← 先等退出
return _SshResult(exit_code, stdout.read().decode(), stderr.read().decode())
```

`recv_exit_status()` 会阻塞到命令结束;命令的输出超过 SSH 通道窗口(通常 ~64KB)时,远端卡在 write、永不退出 → **互相等死**。这是 paramiko 文档明确警告的顺序问题,正确写法是先 `read()` 再 `recv_exit_status()`。

今天 `gateway_transport.py` 的输出是 base64 的小帧,碰不到;但 `SshTransport` 是公开能力,任何“跑一次 `dmesg` / 导日志 / `logcat`”的设备驱动都会挂在这里,现场表现是“命令不返回”,最难查。

期望:先读完 stdout/stderr,再取 exit code;有假通道单测模拟窗口耗尽的情况。

## 3. 非字节流传输被证据层包成坏对象(与 issue 13 同一处,这里补精确故障点)

`interfaces.py:65-75` 的 `HttpTransport` **连 `connect()` 都没有**(只有 `request`)。而 `composition/root.py:264-272` 对所有 `role: dut` 无条件 `evidence.wrap(...)`,`RecordingTransport.connect()` 直接转调 `inner.connect()`。

所以加第一台 HTTP 设备时,故障点是**装配后的第一次连接**:`AttributeError: 'RealHttpTransport' object has no attribute 'connect'`。CAN 设备则是 `send_frame` 缺失。装配期不报。

修法见 issue 13 第 2 条(推荐装配期校验形状并报错)。

## 4. `is_connected` 三套语义,都不可靠

- `RealTcpTransport.is_connected`(`:95-96`):只看 `self._sock is not None` —— 对端拔线、RST 之后仍是 `True`
- `RealCanTransport.is_connected`(`:112-113`):同样只看对象非空
- `RealSerialTransport.is_connected`:用 `is_open` —— USB 转串口被拔掉后同样可能仍为 `True`

上层拿它判断“要不要重连”会得到错答案。期望:要么把名字改成 `has_client`(诚实),要么在读数失败时以实际异常为准,并在文档里写明它不代表链路健康。

## 5. CAN 传输硬编码 `socketcan`

`real_transports.py:104-107`:`bustype="socketcan"` 写死,而清单里只能配 `channel` 和 `bitrate`。socketcan 是 Linux 专用接口 —— 当前开发环境是 Windows,接了 CAN 设备会直接起不来,且无法通过配置改用 `pcan` / `vector` / `slcan`。

期望:接口类型进配置(`bustype` 或 `interface`,缺省给出当前平台的合理值),未知类型在装配前报错并列出可用值。

## 6. 零测试覆盖

`git grep -l "RealSerial\|RealTcp\|RealCan\|RealSsh\|RealHttp\|RealMqtt" tests*/` 无输出。六个类的构造参数、可选依赖缺失时的报错、协议形状一致性,全都没锁。

## 推荐做法(我认为的最优解:假库契约测试)

不要等真机,也不要起 docker 串口服务器。用可控假对象把真机行为钉死,半天工作量:

- `tests_unit/test_real_transports.py`,按类各一组:
  - 假串口:分两段返回应答 → 断言 `receive` 拿到完整帧(锁第 1 条)
  - 假串口:返回 300 字节的长帧 → 断言不会被 256 截断
  - 假 paramiko 通道:输出 100KB 再退出 → 断言不阻塞、`exit_code` 正确(锁第 2 条)
  - 假 socket:对端关闭后 `recv` 抛 `ConnectionResetError` → 断言不在 `is_connected` 撒谎(锁第 4 条)
  - 六个类:可选依赖没装时抛出的错要带“请安装 pyserial / paramiko …”,不是裸 `ModuleNotFoundError`(当前实现是裸的)
- 可选依赖全部在方法内 import(`real_transports.py:17-19` 的设计),所以**这些用例在没装任何可选依赖的 CI 上也能跑**,只要假对象注入到 `sys.modules`。

## 验收

- [ ] 短读的应答能被收成一帧;长于缓冲区上限的帧不会被静默截断(或明确报错)
- [ ] `exec_command` 在输出超过通道窗口时不阻塞,`exit_code` / `stdout` 都正确
- [ ] 六类真机传输各有至少一条假库用例,在未安装任何可选依赖的环境下通过
- [ ] `is_connected` 的语义在文档里说清;撒谎的那两个要么改名,要么改实现
- [ ] CAN 接口类型可配置,未知值在装配前报错
- [ ] 默认套件仍是 `181 passed, 6 skipped` 加新增用例,其余一条不变

## Comments

- 2026-10-20 出票依据:只读审 `a82af5d` 的 `transport/real_transports.py`。六条都是读代码 + 库语义确认,不需要真机;第 3 条与 `13-evidence-gaps.md` 第 2 条同源,不重复计工作量。
- 相对优先级:第 1、2 条是会到真实台架才暴露的“假通过→现场炸”;第 4、5 条是当场就起不来的显性问题;第 6 条是让前五条不被发现的根因。

## Comments

- 2026-10-21 已完成:`issue/14a`(合入 `3be78de`)字节流按规约收帧 + SSH 先读输出再取退出码;`issue/14b`(合入 `e21bb97`)`is_connected` 改名 `has_client`、CAN 接口类型进清单(`canopen` 规约)、六类假库契约、`serial`/`tcp`/`gateway_ssh` 的 connection 键闭合。终态 265 passed / 6 skipped。
- 遗留另出票:`16-http-mqtt-ssh-unreachable.md`(HTTP / MQTT / SSH 三个传输在清单里不可达,和 CAN 修复前同一形态)。`is_connected` 改名连带影响 `tests/test_evidence.py` 两行方法名,证据语义未变。
