# 38 写路径从来没在真 socket 上验过:独立 mock 从站只支持读

Status: resolved
Type: task
Blocked by: None — 半天

**What to build:** 进程内模拟器已经支持 0x06 写(寄存器内存 + 回显 + 地址不存在报错),所以 480 条用例能绿;但 `tools/mock_modbus_meter.py`(那个真监听端口、真收发的独立从站)**只支持读**。结果是 23,020 个写步骤的协议路径,从来没有经过真实 socket。

## 现状(实测)

`tools/mock_modbus_meter.py:113` 只调 `modbus_rtu.parse_read_request`:

```python
frame_slave, function, address, count = modbus_rtu.parse_read_request(request)
response = modbus_rtu.build_read_response(frame_slave, payload, function)
```

而 `parse_read_request` 对非读功能码是直接抛的:

```python
if body[1] not in _READ_FUNCTIONS:
    raise ValueError(f"不支持的功能码 {body[1]:#04x},目前只有 03/04 读寄存器")
```

所以往这个 mock 发 0x06,得到的是异常/断连,不是写成功。

对比两边能力:

| | 进程内模拟器 `SimulatedModbusSlave` | 独立 mock `mock_modbus_meter.py` |
|---|---|---|
| 0x03/0x04 读 | ✅ | ✅ |
| **0x06 写单个寄存器** | ✅(寄存器内存 + 回显应答,`simulators.py:94-115`) | ❌ |
| 0x10 写多个 | ❌(明确划到"不支持的功能码") | ❌ |
| 真实 socket 链路 | ❌ | ✅ |
| 故障注入(timeout / bad-crc / slow) | ❌ | ✅ |
| 真帧字节级校验 | 部分 | ✅ |

也就是说:**写只在"瞬间成功"的假实现上验过**,从没经过 socket、没经过真实的 1200ms 超时路径、没经过"从站拒写"的异常应答。

## 期望

把 `SimulatedModbusSlave` 已经写好的 0x06 语义搬到独立 mock 里(**复用 `modbus_rtu` 的解析,不要另写一套**):

1. 0x06 请求 → 值写进寄存器表 → **应答是请求帧原样回显**(Modbus 规定)
2. 地址不存在 → 异常应答 0x02(非法数据地址),和读路径**用同一套判定**
3. 值非法(比如超范围)→ 异常应答 0x03(非法数据值),新增 `--fail write-reject` 故障注入
4. 写后立即读,能读到刚写进去的值(真机这一步常常读不到,见下)

## mock 能盖住什么、盖不住什么(诚实清单)

| 能盖住 | 盖不住(只能真机) |
|---|---|
| 0x06 帧格式、CRC、功能码 | **EEPROM 写入延迟**:真机写入后 10–50ms 内读回会读到旧值 |
| 寄存器内存更新 + 写后回读一致 | **写保护 / 密码 / 密级**(写被拒的真实条件) |
| 非法地址 → 异常码 02 | **掉电丢失 / 写入次数寿命** |
| 非法值 → 异常码 03 | **写完需要重启或复位才生效** |
| 超时 / 断链 / 坏 CRC(现有故障注入) | 真实设备对"合法但超范围"值的静默截断策略 |

结论:mock 能盖住协议与状态机(约 80%),剩下的 20% 是真物理语义。**先把 80% 锁住,真机首跑就只需要关心那 20%。**

## 验收

- [x] `mock_modbus_meter.py` 支持 0x06:值进内存、应答回显请求帧、写后读能读到
- [x] 写非法地址 → 异常码 0x02;写非法值 → 异常码 0x03;两者都有用例
- [x] `--fail write-reject` 故障注入可用
- [x] 一条端到端(真 socket):`write()` → 回读一致 → 证据里 `kind: write` 且 `verified: true`
- [x] 一条端到端:从站拒写 → 平台抛的异常与"读超时"不同类,归因分到不同栏
- [x] 进程内模拟器与独立 mock 对同一请求的应答字节一致(有对照断言)
- [x] 默认套件仍全绿,skip 数不变

## Comments

- 2026-10-20 出票依据:`tools/mock_modbus_meter.py:113` 只走 `parse_read_request`;`modbus_rtu.parse_read_request` 对非 0x03/0x04 抛错;`simulators.py:72-124` 的 `SimulatedModbusSlave` 已实现 0x06(含"地址不存在"与读路径同一句话)。
- 与 xlsx 导入的关系:23,020 个写步骤在等着落。写路径多一层 mock 验证,导入后第一批真机跑才不会把"写没生效"误判成"设备不合格"。

- 2026-10-22 已完成 `3324196` / 合入 `46fa969`。独立 mock 从站支持 0x06 / 0x10(写进寄存器内存 + 回显)、异常码按 Modbus 规范(**02 = 非法数据地址、03 = 非法数据值** —— 任务文字里我把这两个写反了,实现按规范做对)、写后读一致、故障注入未退化(`--fail timeout` 仍是 TimeoutError,`write-reject` 是 SlaveExceptionResponse)。
- 交付时报出的缺口另出票:`40-in-process-simulator-cannot-write-multi-register.md`(已随 `bfe6da1` 修掉)。
