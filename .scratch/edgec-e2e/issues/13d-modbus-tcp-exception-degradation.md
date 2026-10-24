# 13d Modbus TCP 异常码绕过降级/重试 —— 原线索未复现,真缺陷是判定重复

Status: resolved

## 结论(与票面相反的部分要写清)

**票面前提「TCP 把异常码当普通响应继续解」不成立。** 复现时同一份注入(合并块扫到空隙 → 整帧 0x83),两条路径**逐字节相同**:

```
RTU  requests: [(03,0,3), (03,0,1), (03,2,1)]   readings 两值都对
TCP  requests: 同样三次                          readings 同样对
```

**真缺陷**:异常码判定被写了两份 —— `modbus_rtu.py` 的 `_EXCEPTION_FLAG = 0x80` 与 `modbus_tcp.py` 里第二份,两边各一套 `pdu[0] == function | 0x80`。靠共享 `SlaveExceptionResponse` 类型才侥幸一致;改一边,另一边静默错。

## Comments

- 2026-10-24 合入 `c9415d9`。**leader 自验**:`grep -c "0x80" modbus_tcp.py` = 1(只剩 docstring 一处说明,无判定);`is_exception_response` 唯一定义在 rtu,TCP `import` 它并用于两处;`pytest -k modbus` = **49 passed**。
- 变异(实现方实测):改坏 TCP 的 `is_exception_response` 调用 → **TCP 两条参数化红、RTU 保持绿**。**"TCP 红 RTU 绿"正是要的形状** —— 守卫钉在 TCP 上,没有连坐。
- **结票口径(leader 拍)**:按「结构收敛 + 补 TCP 守卫」结,不按"未复现"关票。

  > **"复现不了就关票"是为了防止假修,不是为了丢掉顺手发现的结构缺陷。**

- 未做:未改 RTU 证据格式;`catalog --verify` rc=0(无漂移)。
