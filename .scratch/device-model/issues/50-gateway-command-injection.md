# 50 网关命令注入:`local_bus` 直接拼进 SSH 命令行(安全)

Status: resolved
Type: task
Blocked by: None — 小改动

**What to build:** `gateway_ssh` 把清单里的 `connection.local_bus` 原样拼进远端 shell 命令。装配期只校验它是字符串,**任何内容都放过**。

## 实测

```python
local_bus = 'can0; id > /tmp/pwned #'
parse_manifest(...)      # 装配期接受
```

到了 `transport/gateway_transport.py`:

```python
79:  cmd = f"bus-agent send --bus {self._local_bus} --data {payload}"
86:  cmd = f"bus-agent recv --bus {self._local_bus} --timeout {timeout}"
```

再经 `_ssh.exec_command(cmd)` 在网关主机上执行。**分号、反引号、`$()` 都会生效。**

## 为什么算安全项而不是吹毛求疵

清单是数据,而且是**会被复制传播的数据** —— 供应商回一份 yaml、同事从别处粘一段 connection,都可能带着奇怪的字符。平台在网关主机上以 `gateway_user` 身份执行,通常是工程账号甚至 root。

这条和第 48 条(主机密钥)是同一层:**允许连到别人,就等于执行别人的命令**。

## 期望

1. **装配期白名单校验 `local_bus`**:`^[A-Za-z0-9_.:-]{1,32}$`(总线名就这几种字符),不合规直接报错并说明允许的字符集
2. **命令拼装改为不可注入的形式**:`shlex.quote()` 至少;更好是 `exec_command` 支持参数数组(若 paramiko 封装不方便,就用 `shlex.quote`)
3. `timeout` 是数字,拼进命令前按 float 格式化(现在靠 f-string,若上游传了奇怪类型就漏)

## 不做

- 不引第三方 shell 解析库
- 不改 `bus-agent` 的协议(那在网关侧)

## 验收

- [x] `local_bus` 含 `;` / `|` / `$(` / 反引号 / 空格 / 换行 → 装配期报错,消息写清允许字符
- [x] 合法值(`can0`、`vcan0`、`/dev/ttyS1`)仍通过
- [x] 命令拼装对合法值产生**与现在逐字节相同**的命令(有对照断言,避免顺手改了调用约定)
- [x] 有假 SshTransport 的用例:捕获实际执行的命令串,断言不含未转义的分号
- [x] 基线不回归(`docs/baseline.md`:609 passed / 7 skipped)

## Comments

- 2026-10-20 出票依据:ponytail-audit 补审项(`gateway_transport.py` 上一轮只粗读);上面 `parse_manifest` 接受注入串是实跑的。
- 与 48 同族:48 管"连的是不是真网关",本条管"连上之后执行的命令"。两条都在真机接入前置里。
- 2026-10-22 实现:白名单落地为 `^(?!-)[A-Za-z0-9_.:/-]{1,64}$`(装配期,`config/manifest.py::_validate_gateway_local_bus`),
  拼命令再加 `shlex.quote`(纵深)。相对票面字符集有两处调整,都是票面自相矛盾的补齐:
  - 加 `/`:票面自己列 `/dev/ttyS1` 当合法值,而 `/` 不在票面字符集里。`/` 既不是 shell 元字符,也在
    `shlex` 安全集内,两道防线都没被削弱。
  - 长度 32 → 64:`/dev/serial/by-id/...`(tty 的推荐写法,避开 ttyUSB 编号漂移)有 60+ 字符,
    32 会挡住一个合法路径 —— 那种表现是"配置写对了却装配失败"。
  - 前导 `-` 禁用:参数注入。`shlex.quote` 只防 shell 解释,不防 `bus-agent` 把 `-x` / `--data` 吃成选项。
    禁前导 `-` 挡掉这一类;`-x` / `--data` / `-` / `--bus=can0` 现在都在装配期被拒。
- **未做,待真机确认**:改成 `--bus=<值>` 单参数形式。它比"禁前导 `-`"更彻底,但今天无法断言真网关上的
  `bus-agent` 认不认这种写法;若它只认空格分隔,改了就是"配置对了但网关不认",那种失败要到真机才出现。
  等真机确认后再改这一行(v1 单参数 + 白名单可以一起留)。
- 合法值的命令串与修复前逐字节相同(`shlex.quote` 对白名单字符集是恒等):`can0` / `vcan0` / `/dev/ttyUSB0` /
  `/dev/ttyS1` / by-id 路径都验过;payload 是 base64(只会出现 `A-Za-z0-9+/=`),quote 同样恒等。
  对照断言在 `tests_unit/test_gateway_command_injection.py`。

- 2026-10-22 已完成 `c643abd` / 合入后 **666 passed / 7 skipped**(新增 47 条用例)。字符集定为 `^(?!-)[A-Za-z0-9_.:/-]{1,64}$` —— 比票面多两处:**加 `/`**(票面自己列的 `/dev/ttyUSB0` 不在原字符集里,以"合法值能过"为准)、**32 → 64**(`/dev/serial/by-id/...` 是推荐写法,挡住它的失败形态是"配置写对了却装配失败")。
- **参数注入那一条是复核时补的**:`shlex.quote` 不防 `-x` / `--data` 被目标程序当选项,所以加了"禁前导 `-`"这一道。`--bus={value}` 单参数形式**未实现**,记在此处待真机确认(真网关 `bus-agent` 认不认 `--bus=can0` 无法断言,改错会变成"配置对了但网关不认")。
- 合法值修复前后命令**逐字节相同**(`can0` / `vcan0` / `/dev/ttyUSB0` / `/dev/ttyS1` / 62 字符 by-id 路径);payload 是 base64,`shlex.quote` 为空操作。
