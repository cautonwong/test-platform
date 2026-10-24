# 18 对端关闭被记成超时:断链和“设备不回”在证据里长得一样

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `6d3b95f` 的收帧改动做得好,但把两种完全不同的现场情况合并成了一种:TCP 对端关闭(拔网线、设备断电、串口服务器重启)现在和“设备收下请求但不回答”一样,都以 `IncompleteFrameError` 收场,证据里都写 `error: "timeout"`。

这两件事在现场的处置完全不同:超时 → 可能是设备问题(该判不合格);断链 → 是台架问题(该判“测试无效”)。现在证据分不开。

## 现状(读码 + 可复现)

`real_transports.py` 的 `_receive_frame`:

```python
chunk = read_once(want, remaining)
if not chunk:
    break                      # ← socket 关闭时 recv() 返回 b"",走到这里
...
raise IncompleteFrameError(f"{what}: {timeout} 秒内没收满一帧(收到 {len(buffer)} 字节...)")
```

`RealTcpTransport.read_once` 就是 `self._sock.recv(size)`。对端正常关闭时 `recv` **不抛异常、返回 `b""`** —— 于是:

- 抛的是 `IncompleteFrameError`(继承 `TimeoutError`),调用方按“超时”处理
- 证据层按既有规则记成 `{"direction": "receive", "error": "timeout", "waited": ...}`
- `waited` 会是接近 0 的值 —— 只有懂这个细节的人才能从数字上看出是断链

串口那边确实无法区分(串口没有 EOF 语义,`read` 超时就返回 `b""`),所以这条只针对 TCP / HTTP 这类有明确关闭语义的传输。

## 期望

按传输各自的能力区分,**不要为了统一而在共享的 `_receive_frame` 里猜**:

- `RealTcpTransport.read_once`:`recv` 返回 `b""` 时抛 `ConnectionError("TCP 对端已关闭")`(或用 `socket.MSG_PEEK` 确认后抛),把这个信号交上去
- `_receive_frame`:`read_once` 抛出的异常原样上抛,不再包成 `IncompleteFrameError`
- 证据层:`ConnectionError` 记成 `error: "closed"`,`TimeoutError` 仍记 `error: "timeout"`
- 串口保持现状(超时归超时),并在注释里写明“串口无法区分断链与超时,这是硬件语义,不是实现偷懒”

## 为什么值得修

- 这是“结论归属”问题:断链必须走“测试无效”而不是“设备不合格”。`4-周边设备与装配顺序` 已经确立了这条原则,但底层把信号合并了,上层就无从区分。
- `IncompleteFrameError` 的注释自己写了“让彻底没收到和只收到半帧分得开”—— 同样的道理适用于“收到一半时链路没了”和“设备根本不说”。
- 真机排障时,`waited ≈ 0` 这种隐晦线索不该是唯一线索。

## 验收

- [x] 假 socket 返回 `b""` → 抛的是 `ConnectionError`(或专门类型),不是 `TimeoutError`
- [x] 假 socket 什么都不返回直到超时 → 仍抛 `IncompleteFrameError`,证据记 `error: timeout`
- [x] 证据里断链记 `error: closed`,超时记 `error: timeout`;两条都有用例
- [x] 串口超时路径行为不变(`test_serial_receive_raises_instead_of_handing_up_half_a_frame` 保持绿)
- [x] 默认套件仍是全绿,skip 数不变

## Comments

- 2026-10-20 出票依据:`6d3b95f` 合入后对收帧逻辑做只读审核。`recv` 返回 `b""` 表示对端正常关闭是 socket 的既定语义,不是实现细节;`_receive_frame` 里 `if not chunk: break` 把这条路径和超时合并了。
- 同一批审核确认已修好:`13` §1(超时落 `error: timeout` + `waited`)、`13` §2(非字节流包装在 `evidence.py:81` 装配期报错)、`14` §1/§2(收帧 + SSH 死锁)、`15`(头牌用例默认跑,skip 7 → 6)。仍开着:`13` §3(证据不记 mode)、`14` §4(`is_connected`)、`14` §5(CAN `bustype` 写死)、`14` §6(CAN/HTTP/MQTT 三类仍无假库用例,只有 serial/TCP/SSH 三类有)。
- 2026-10-21 完成(`643f8f0`)。`RealTcpTransport.read_once` 收 `b""` 抛 `ConnectionError`;`_receive_frame` 不再把它包成超时;证据层 `ConnectionError` → `error: closed`,`TimeoutError` 仍 → `error: timeout`(其余 `OSError` 仍 `disconnect`);串口行为不变,注释写明"没有 EOF 语义是硬件语义"。真 socket 复核:对端关闭 → `ConnectionError`(0.03 s),静默到超时 → `IncompleteFrameError`。
- 票外一小步(已与出票人确认):底层读超时(真实 socket 静默时抛 `socket.timeout`)现在也统一收成 `IncompleteFrameError`,报错带已收字节数与 hex —— `interfaces.py` 里早写明这条契约,TCP 之前是例外。证据语义不变(仍 `timeout`)。
- 结论归属做到哪一步:证据里两类失败已按设备分得开(同一轮里周边设备 `closed`、被测设备 `timeout`,已加用例)。**差最后一步**:`connect_support()` 只覆盖"连接阶段"的周边故障,收帧阶段的 `closed` 目前没有任何地方把它转成"测试无效"。补上需要动 `composition/root.py` 或测试层(按角色把中断的链路映射成 `SupportEquipmentFault`),那是本票的编辑范围之外,也涉及产品决定(周边设备断链是立即判整轮无效,还是只在该轮确实用到它时才算)。

- 2026-10-21 已完成 `643f8f0` / 合入后 272 passed / 6 skipped。真 socket 独立复核:对端关闭 → `ConnectionError`(不是 TimeoutError 子类)、静默 → `IncompleteFrameError`(是)。证据分别记 `closed` / `timeout`。串口保持现状(无 EOF 语义)。票外一小步:`socket.timeout` 统一收成 `IncompleteFrameError`(带已收字节数 + hex),让 `interfaces.py` 的契约对 TCP 也成立;假 socket 保真度一并修正(静默抛 `socket.timeout`,新增 `_ClosingSocket` 表示 FIN)。
- 残余已另出票:`20-closed-link-does-not-invalidate-the-round.md`。
