# 接到真实台架上

从模拟器切到真机,只改清单,不改代码。但有几处只有真机才会咬人的地方,按顺序过一遍。

## 1. 装依赖

```bash
pip install -e .[serial]   # 串口 / RS485
pip install -e .[ssh]      # 经工控机网关转发
pip install -e .[can]      # CAN
```

只跑模拟器不需要装任何 extras。

## 2. 改清单:三步

```yaml
- id: meter_01
  model: dtsd1352_modbus
  access: serial                # 接入方式,不是规约;必须在该 protocol 允许的集合里
  mode: real                    # 不写 mode 时缺省就是 real
  bus_address: 1                # modbus_rtu 必填(1–247);dlt645 是 12 位 BCD 表地址
  connection:
    port: /dev/ttyUSB1
    baudrate: 9600
    inter_frame_delay_ms: 4     # 见下,真机建议显式给
```

各 access 的 `connection` 键是闭合集合,拼错会报错:

| access | 键(必填加粗) | 备注 |
|---|---|---|
| `serial` | **port** / baudrate / inter_frame_delay_ms | 缺省 9600 / 0 |
| `tcp` | **host** / **port** | 串口服务器走 TCP 时,规约仍是 `modbus_rtu`,帧里是 RTU 不是 MBAP |
| `gateway_ssh` | **gateway_host** / **gateway_user** / **local_bus** / gateway_key_path | 网关侧要装 `bus-agent` |
| `can` | **channel** / bitrate / interface | interface 取值 `socketcan` / `pcan` / `vector` / `slcan` / `virtual` |

## 3. 帧间隔:真机建议显式配

Modbus RTU 要求帧间静默 ≥3.5 字符时间(9600 波特下约 4ms)。`inter_frame_delay_ms` 缺省是 **0**(不等待),背靠背发送时严格的从站会把两帧粘成一帧,报出来往往是 CRC 错或超时 —— 看着像设备不响应。

先按 `3500 / baudrate` 给一个值(9600 → 4,19200 → 2),不合适再调。

## 4. 总线独占

同一台终端下挂多块表时:**不要**用裸 `-n 4` 跑,必须带 `--dist loadgroup`。漏了它会开跑前直接报错退出,不会静默降级。

```bash
python -m pytest -q                                     # 单进程,最稳,首跑用这个
python -m pytest -q -n 4 --dist loadgroup                # 并行,按 via: 拓扑分组
python tests/parallel_topology_check.py                  # 验证分组真的生效(不在默认套件里)
```

## 5. 首跑 checklist

1. **先只跑一台** `python -m pytest -q --select model:<型号>` —— 一次接一台,别一次上全部。
2. `--force-simulator` 反着用一次:同一份清单在模拟器下必须也绿,才说明是链路问题而不是配置问题。
3. 首跑用单进程,先别开 `-n`。总线独占/抢占问题要等单进程稳了再验。
4. 每轮跑完 `python tools/attribute_round.py`,看有没有「测试无效」—— 那是台架问题,不是设备问题。
5. 保留第一轮的 `evidence/<会话>/` 当基线,后面改动后对比读数与帧。

## 6. 真机上最容易踩的四件事

| 现象 | 多半是 | 先查 |
|---|---|---|
| 连不上 | 端口名/波特率/从站地址 | `connection.port`、`bus_address`、串口是否被别的程序占着 |
| 读数是乱的 | 波特率不符或字节序/倍率写错 | 点表的 `byteorder` / `scale`,以及 `inter_frame_delay_ms` 是否给了 |
| 偶发超时 | 总线被抢,或帧被粘 | 是否漏了 `--dist loadgroup`;帧间隔 |
| 断链判成不合格 | 网线/串口服务器重启 | `link_lost` 记录的归属;被测设备断链判不合格,周边设备断链判测试无效 |

## 7. 没有真机时怎么先验一步

```bash
python tools/mock_modbus_meter.py --port 0 --slave 1 --registers configs/mock_registers.yaml
python tools/mock_modbus_meter.py --port 0 --slave 1 --registers configs/mock_registers.yaml --fail timeout
python -m pytest -q tests_platform/test_mock_meter_end_to_end.py
```

`--fail` 支持 `timeout`(收下不回)、`bad-crc`(帧合法但 CRC 错)、`slow`(真延迟)、`write-reject`(每个写都回异常码 03)。这几种只有独立从站进程造得出来,进程内模拟器造不出。
