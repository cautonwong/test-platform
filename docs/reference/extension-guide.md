# 扩展指南

## 新增一个型号(同大类、同规约)

只改 `configs/devices.yaml`:在 `models` 里加一段,再在 `devices` 里加至少一条引用它。
点表、能力、判据集写一次,同型号的多台实例不重复。零 Python 文件;
已有的一致性测试会自动对新型号的每台设备展开一次(`test_xxx[新设备id]`)。

## 新增一个规约(同一个大类,比如给电表再加一种规约)

1. `protocol/` 下加一个模块:拼帧、校验、按点表项解码,纯函数。
2. 在对应驱动里加一个 `read_raw`(一个点表项 → 一次收发 → 原始字节),用 `@register_device("meter", "新规约")` 注册;
   同时调用 `register_protocol("新规约", access={"serial", "tcp", ...})` 声明它允许的接入方式。
   若该规约的地址是连续的(能一次读一段),再给一个可选的 `read_raw_many` 做块读;
   给不出就不要给 —— 通用读取路径会逐点读,语义一样。
3. 清单里型号的 `protocol:` 换成新规约。

内置规约仍在 `config/manifest/constants.py` 的 `PROTOCOL_ACCESS` 留一份静态表,供"只解析清单、不 import 驱动"的
独立调用回退;新增规约不需要改它 —— 驱动注册的 access 优先。

外部项目可以用 entry point 把自己的驱动/传输装进来,不用改平台源码:

```toml
[project.entry-points."testplatform.drivers"]
my_driver = "my_pkg.drivers.my_driver"
[project.entry-points."testplatform.transports"]
my_bus = "my_pkg.transports.my_bus"
```

`pip install -e .` 后 `registrations.load_plugins()` 会导入它们;导入失败带插件名直接报错。

已有项目里加一个驱动或编解码,不用重新生成项目:

```bash
testplatform new-driver my_meter --device-type my_meter --protocol vendor_ascii
testplatform new-protocol my_codec
```

`new-driver` 写驱动文件、项目 `conftest.py` 的 import 和 `pyproject.toml` 的 entry point;`vendor_ascii` 生成即可跑,其它规约生成 `read_raw` 骨架。已注册的规约沿用平台声明的 access,不再重复注册。`new-protocol` 写纯函数模块,`python <文件>` 跑自检。这些命令都不改平台源码。

通用读取路径 `drivers/point_table_driver.py` 不改 —— DL/T645 接入时验证过这一点。

## 新增一个设备类型(新大类,比如充电桩)

1. `domain/` 下新建聚合根(参考 `battery.py`):只写物理量与不变量,不碰协议。
2. `drivers/` 下新建驱动(参考 `battery_driver.py`):用 `@register_device("新大类", "规约")` 注册工厂,
   并在 `composition/registrations.py` 的 import 清单加一行,让装饰器真正执行。
3. 清单里加型号 + 设备。

测试层不用改:按类型展开的 fixture 由注册表生成(`tests/conftest.py` 末尾),
`@pytest.mark.requires_capability` 按型号声明的能力跳过。

## 新增一种周边设备类型

同"新增一个设备类型",清单里那条设备写 `role: support`。周边设备不判合格、不参与一致性测试展开;
它连不上时结论是"测试无效"(整轮 skip),不是被测设备不合格。

## 台架型号 `grid_simulator_3ph`:量名、占位地址、三相设定

上游条款的台架前置是一整块三相设定,量名按上游写法声明,**三相电压 / 电流是独立的量**(条款里
`Ua` 和 `Ub` 是两件事,合成一个「电压」就把信息丢在清单里了):

```yaml
  grid_simulator_3ph:
    type: grid_simulator
    protocol: vendor_ascii
    capabilities: [Ua, Ub, Uc, Ia, Ib, Ic, θa, θb, θc, QUaUb, QUaUc, Fre, 接线方式, MeterConst]
    point_table:
      Ua: {address: TODO, length: 7, encoding: ascii, unit: V, writable: true}
      ...
      接线方式: {address: TODO, length: 2, encoding: uint, writable: true}   # 枚举,不是模拟量
      MeterConst: {address: TODO, length: 2, encoding: uint, writable: true}  # 整数参数
```

- **地址是占位(`TODO`)。** 真实台架的寄存器地址 / 编码 / 倍率还没拿到。`mode: real` 时装配期
  直接报错并列出是哪几个量;`mode: simulator` 照常跑。编一个"看着像"的地址写进去、回读还一致,
  是最坏的一种假绿 —— 宁可报缺。
- **量名是清单契约。** 旧清单里的 `电压` / `频率` 不再认,报错直接给改法;
  不做别名(同一个量两个名字,迟早有一处改动没跟上)。
- **`接线方式` 的枚举名 → 整数在 `configs/import/params.yaml`**,不散在驱动里;驱动只接收整数
  (设成 `3.5` 是平台自身的不变量违反)。
- **三相设定是 `SET_GRID3 量名=值 …` 一帧**,应答按同一份量名回。模拟台架
  (`connection: {simulated_grid: true}`)按设定回读,用来验驱动的帧与量名;
  单相的 `apply_grid(电压, 频率)` 与 `initial_setpoint` 照旧。

## 一条用例失败之后台架怎么复位

写原语上线之后,"用例把参数改掉之后失败"会污染后面所有用例:下一个用例读到的值会被判成
**设备不合格**,而归因里没有"被上一条用例污染"这一类。所以收尾按 `role` 分层:

| 角色 | 收尾做什么 | 失败时 |
|---|---|---|
| 周边设备 `support` | 回到清单声明的 `initial_setpoint`(如 `{voltage_v: 230, freq_hz: 50}`) | **测试无效**,不是设备不合格 |
| 被测设备 `dut` | 只做**用例自己声明的**收尾(`scenario.on_cleanup(...)`) | 这条用例失败,证据标 `cleanup_failed` |
| 平台自身 | 补证据 + 断开 | — |

- 复位在**每条用例结束后**与整轮结束前各做一次(`tests/conftest.py` 与
  `tests_scenario/conftest.py` 的 autouse fixture 调 `CompositionRoot.restore_support()`),
  所以下一条用例拿到的台架总是在初始设定上。恢复失败 → `skip("测试无效: …")`,退出码不变。
- **平台从不在收尾里往被测设备写东西**:整轮结束时设备被人动过,比留在工厂模式更难查。
  `initial_setpoint` 只允许写在 `role: support` 的设备上(清单解析时拒绝),
  复位也只遍历这张表;`tests/test_bench_restore.py` 用"复位与 teardown 期间被测设备零发送帧"来钉这一条。
- 声明了 `initial_setpoint` 而驱动没有 `apply_initial_setpoint`(消费者定义的
  `SetpointRestorableDriver` 接口)时**装配期就报错**:否则复位只是"静默没发生",
  后面所有读数都被污染,报告里看不出来。

## 新增一种接入方式(比如新的串口服务器、WebSocket 通道)

1. `transport/` 下实现 `interfaces.py` 里某个 Protocol(总线类设备一般用 `ByteStreamTransport`)。
2. `@register_transport("名字", "real")` 注册真实实现,`simulators.py` 里注册 `"simulator"` 模式。
3. 清单里设 `access: 名字`;还要在对应协议/驱动的 `register_protocol(..., access={...})` 里放行这个 access。

`gateway_transport.py` 是真实示例:作为新的字节流实现加入,逆变器和电池的驱动、聚合根一行未改。

**`http` / `mqtt` / `ssh` 三条传输已实现并注册,但清单里暂时不可达**:今天没有任何驱动
`register_protocol` 放行这三种 access(没有设备用 REST / MQTT 上报 / 主机命令访问),
所以在 `devices.yaml` 里写 `access: http` 会被 `parse_manifest` 拒绝,并说明原因与下一步。
这不是坏代码、也不是漏实现:凭空造一个 `http` 规约只会得到「能配但装配必失败」的假象。
要接这类设备,先定它用什么规约上这种接入(如 EMS 走 REST、HEMS 走 MQTT 上报、工控机走 SSH 命令),
再在对应驱动的 `register_protocol` 里放行。三个类与三个注册工厂上的注释指向同一条边界(`TRANSPORT_ONLY_ACCESS`)。

## CAN 接入:接口类型不是 socketcan 专用

CAN 设备的型号规约写 `canopen`(CANopen 是 CAN 上的应用层规约,CiA 301;电池 / 储能另有
CiA 418 / 419 行规),它在静态表 `PROTOCOL_ACCESS` 里只放行 `access: can`(目前没有 canopen 驱动)。
所以 CAN 设备的清单长这样:

```yaml
models:
  bms_can:
    type: battery
    protocol: canopen
devices:
  - id: bat_can
    model: bms_can
    access: can
    connection: {channel: can0, interface: pcan, bitrate: 500000}
```

接口类型在清单里配,不写 `interface` 时按平台给缺省(Linux `socketcan`,
Windows `pcan`,其余平台 `slcan`)。可用值(白名单在 `config/manifest/constants.py` 的
`CAN_INTERFACES`):`socketcan`(Linux 内核自带)、`pcan`(PEAK PCAN-USB)、
`vector`(Vector 板卡)、`slcan`(USB 转 CAN 串口适配器)、
`virtual`(没有硬件时自测)。`socketcan` 只有 Linux 有,写死在代码里会让 Windows 上接了
CAN 设备直接起不来;未知值在装配前就报错并列出可用值,connection 里拼错的键也一样拒绝。
`canopen` 目前没有设备驱动:装配会停在"未找到设备工厂",而不是停在清单校验。

## serial / tcp / gateway_ssh 的 connection 键是闭合的

这三族字节流的 `connection` 键与传输实现真正读取的键一一对应,拼错立即在装配前报错,
不会静默取缺省值(波特率把 `baudrate` 拼成 `baudrat`,真机上表现为"连上了但读数是乱的"):

```yaml
- id: bat_01
  access: serial
  connection: {port: COM3, baudrate: 19200}          # port 必填
- id: inv_01
  access: tcp
  connection: {host: 192.168.1.50, port: 502}        # host / port 都必填
- id: inv_02
  access: gateway_ssh
  connection: {gateway_host: 10.0.0.5, gateway_user: test,   # 这三个必填
               local_bus: can0, gateway_key_path: ~/.ssh/id_rsa}  # key_path 可选
```

模拟器专用键 `simulated_response` / `simulated_645` / `simulated_registers` / `simulated_grid`
四族都合法。
`local_bus` 会被拼进网关主机上执行的 shell 命令,所以只允许 64 个以内的字符,字符集
`A-Za-z0-9_.:/-`(总线名如 `can0`,设备节点如 `/dev/ttyUSB0`);带 `;` / `|` / `$()` / 反引号 /
空格 / 换行的值在装配前直接报错。
必填键只在 `mode: real` 下强制:模拟器不读主机名和端口,`mode: simulator` 下不逼你写一个
没人读的假地址。可用键在 `config/manifest/constants.py` 的 `STREAM_ACCESS_CONNECTION_KEYS`。

## SSH 主机密钥:未配置 = 不校验

经工控机网关(`gateway_ssh`)或 SSH 命令(`ssh`)接真机时,平台默认**不校验对端主机密钥**
—— 与修复前完全一致(台架网络里有人冒充网关时,平台会把别人的回帧当设备数据,证据里看不出来)。
要挡这个,在 `connection` 里配上 known_hosts 路径:

```yaml
- id: inv_02
  access: gateway_ssh
  mode: real
  connection:
    gateway_host: 10.0.0.5
    gateway_user: test
    local_bus: can0
    gateway_known_hosts: ~/.ssh/known_hosts   # 直连 ssh 用键名 known_hosts
```

- **未配置** → 不校验主机密钥(与以前一样连得上)。这是当前缺省;建连时记一行
  `注意: <主机> 的主机密钥未校验(未配置 known_hosts…)`,让运行时看得出这次连接信任到哪。
- **配置且匹配** → 照常连接,并记一行 `SSH 主机密钥已校验: <主机> <算法> <base64>` 日志。
- **配置且不匹配**(或该主机根本没登记)→ 连接被拒绝,报错里有**期望指纹**、**实际指纹**
  和登记命令(`ssh-keyscan -p <端口> <主机> >> <known_hosts>`)。人工确认那台机器确实是
  你的网关之后再把指纹登记进去 —— 换个密钥的网关和冒充的网关在这一层长得一模一样。

装配期只校验**路径存在且是文件**,不读内容:内容写错就会变成“真机连不上、报认证失败”,
另一种难查的失败形态。第一次登记网关指纹:

```bash
ssh-keyscan -p 22 10.0.0.5 >> ~/.ssh/known_hosts   # 先人工核对输出里的指纹
```

比对与拒绝在 paramiko 的 `missing_host_key` 钩子里(`transport/real_transports.py` 的
`_VerifyHostKeyPolicy`):每次建连只算一次指纹,超时重连也必经它。

## 真串口的两个卫生参数(RS485 换向、帧间隔)

这两件事**归平台管,不归适配器管**,而且模拟器和假串口永远测不出来:真机上出问题时,
现象看起来都像"设备坏了"。

**换向前清残留缓冲(无需配置)。** `send` 在写出请求帧之前先 `reset_input_buffer()`,把上一帧
剩下的字节(从站多发的那个字节、上一次超时后迟到的应答)丢掉。不清的话下一帧从头就错位,
而错位解出来的往往是一个"看着正常"的**假读数**,比 CRC 失败难查得多。
它刻意放在 `send` 前、不放在 `receive` 前:清在 `receive` 前会把刚好先到的应答一起丢掉。

**最小帧间隔 `inter_frame_delay_ms`(可选,缺省 0 = 不等待)。** Modbus RTU 要求帧间静默
至少 3.5 个字符时间。平台现在总是背靠背发送;有些从站在严格总线上会因此把两帧粘成一帧,
表现为"设备不回话 / CRC 错"。遇到这种现场就配它:

```yaml
- id: meter_01
  access: serial
  connection: {port: COM3, baudrate: 9600, inter_frame_delay_ms: 4}
```

按波特率估:一个字符 11 位(8 数据位 + 起始位 + 校验位 + 停止位),3.5 字符 = 38.5 位,
所以 `inter_frame_delay_ms ≈ 38500 / baudrate` —— 9600 下约 4ms,19200 下约 2ms,
115200 下不到 1ms(取整到 0 就没意义了,老实的做法是至少配 1)。3.5 字符时间是协议下限,
裕量按现场给;配得过大只是让每帧慢一点。
缺省 0 时**一次都不调 sleep**,不给所有跑法平白加延迟。

**局限:帧间隔是按连接算的。** 计时器在一条 `transport` 实例里。同一台设备背靠背读多个点时
它生效;但如果同一条物理总线(同一条 RS485)上挂着多台设备、它们各自有自己的 transport
(比如两个 `access: serial` 的设备各写了一台串口适配器),**间隔不会被跨连接强制执行**,
两条连接之间的帧仍然可能粘在一起。这种情况真正的解法是让它们共享一条总线(用 `via:`
挂在同一台上游设备下),或者把 `inter_frame_delay_ms` 配得比单帧往返大,让背靠背概率降下来。
