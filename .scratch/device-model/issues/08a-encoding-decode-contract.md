# 08 编码解码契约:ascii 静默截断、byteorder 被忽略、编码集合两份

Status: resolved
Type: task
Blocked by: None — can start immediately（票 5a 已合入,这是它留下的口子）

**What to build:** `protocol/values.py` 的解码入口现在把"文本 token"和"二进制数值"混在同一条管道里,后果是**静默产出看着正常的假读数**。把这两类分开讲清楚,并让清单校验真正知道解码器支持哪些编码。行为改变只有一处真正影响读数(ascii 超长从截断改成报错),其余是装配前校验。

## 四条已复现的问题

复现命令都在仓库根目录执行(`PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -c "..."`)。

### 1. `ascii` 超长被静默截断成假读数(严重)

`point_table_driver.py:61` 传的是 `raw[: point.length]`,截的是**字节**;`values.py:17` 对 ascii 的 `length` 语义却是"最长多少字符"。于是:

```python
decode_value(b'100000.5'[:7], 'ascii')  # -> 100000.0,不报错,数字少一位
```

`raw[:7]` = `b'100000.'`,`float()` 合法收下。应答变长(换量程、换单位、多一位整数)就静默错一位,报告里看不出来。**今天没炸只是因为 6/7/8 都取得够大。**

期望:`ascii` 的截断前长度超过 `length` 就报错,消息里带点表项名、实测 token、声明上限;不要截断了事。

### 2. `ascii` 忽略 `byteorder`,清单写了也不报

```python
decode_value(b'230.1', 'ascii', 1.0, 'little')  # -> 230.1,byteorder 被无声吞掉
```

`manifest.py` 只校验 `byteorder` 属于 `BYTEORDERS`,不校验"这个 encoding 用不用得上字节序"。期望:`encoding: ascii` 且显式写了 `byteorder` 时装配前拒绝,和 `MODEL_ONLY_DEVICE_KEYS` 那种"拼错就报"的态度一致。

### 3. 编码集合是两份手动同步的表

`manifest.py:49` 的 `POINT_ENCODINGS = {"uint","int","bcd","float32","ascii"}` 与 `values.py` 的 if 分支各写一遍。加一种编码要改两处,漏一处就是"装配校验通过、运行时 `ValueError`"。

期望:编码集合只有一份,**由解码器派生**:

```python
# protocol/values.py
DECODERS = {"uint": _uint, "int": _int, "bcd": _bcd, "float32": _f32, "ascii": _ascii}
# config/manifest.py
POINT_ENCODINGS = frozenset(DECODERS)
```

if 链本身不是问题(五个分支、闭合集合、装配前已校验),不要为它引入注册表/装饰器。

### 4. `float32` 的 `length != 4` 报的是 `struct.error`

```
decode_value(bytes.fromhex('0000'), 'float32')
-> struct.error: unpack requires a buffer of 4 bytes
```

`manifest.py:282-284` 只校验"正整数"。期望:在装配前按 encoding 校验 `length`(`float32` 必须 4),错误消息点出是哪个点表项的哪一项。

## 验收

- [ ] `ascii` 的 token 长度超过点表 `length` 时报错,不再截断;有单测锁住(超长 → 报错,恰好等长 → 通过)
- [ ] `encoding: ascii` + 显式 `byteorder` 在装配前被拒;有单测锁住
- [ ] `POINT_ENCODINGS` 由 `values.py` 的解码表派生,删掉手写的集合字面量;加一种编码只改一处
- [ ] `float32` 且 `length != 4` 在装配前报配置错误,消息带点表项名;有单测锁住
- [ ] 现有 `118 passed, 6 skipped` 除新增用例以外一条不变(尤其 `tests_unit/test_protocol_codec.py` 的向量)

## 为什么现在提

票 6(证据落盘)会把读数直接写进证据文件。第 1 条的假读数一旦进了证据,"能据此复现结论"这个验收项反而变成"据此复现错误结论"。第 3 条会让 5b 加 DL/T645 编码时踩同一个坑。

## Comments

- 2026-10-20 出票依据:对票 5a 已合入实现做只读审查时发现,四条均用上面的命令实测复现。票 5a 本身不返工——这是它留下的边界没画清,不是实现错。
- 08 已完成:b15fc59 之前合入(d0a9088)。ascii 超长报错、ascii+byteorder 装配期拒绝、编码集合由解码器派生、float32 长度校验。端到端实测:字段 8 字符而点表声明 7 → 报错不截断。
