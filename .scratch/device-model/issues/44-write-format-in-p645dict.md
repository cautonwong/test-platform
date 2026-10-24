# 44 43 漏了一半:写入格式上游给全了,`p645dict` 的 tuple 里就是类型与字节布局

Status: resolved
Type: task
Blocked by: None — 半天,纯提取

**What to build:** 43 的结论是「5 个参数的名字查不到、DI 在」,并把「写入格式 + 写后是否生效」列为要手册的缺口。**前半句还漏了一层**:写入格式也在上游,就在 `common/p645dict.py` 那个 tuple 里,而且字段含义写在同一文件开头的注释里。

## 实测

```
E:/workspace/cmake/power/autotest-10544/common/p645dict.py:1-20   类型图例
#DateTimeS   YYMMDDhhmmss
#Date        YYMMDDWW
#Time        hhmmss
#DateWeekTime YYMMDDWWhhmmss
#String      (定长字符串)
#Integer / #NegInteger / #Hex / #Object …
```

```
p645dict.py:4915  '计量单元参变量日期及星期':('04000101', 256, 4, 'Date',          'readframe1','writeframe1',[],'w','', '96',254,254)
p645dict.py:4916  '计量单元参变量时间':      ('04000102', 256, 3, 'Time',          …,'w',…
p645dict.py:4919  '计量单元参变量通信地址':  ('04000401', 256, 6, 'String',        …,'w',…
p645dict.py:4920  '计量单元参变量表号':      ('04000402', 256, 6, 'String',        …,'w',…
```

**tuple 里就有**:数据标识(DI)、长度、**数据类型(Date/Time/String)**、读帧/写帧构造器、以及一个 `'w'`(可写)标记。也就是说「时钟要写 YYMMDDWW、4 字节」「通信地址要写 6 字节字符串」这些**不用手册**,上游已经写明。

## 所以「要资料清单」应该缩到三项

| 原 43 的说法 | 实际 |
|---|---|
| 缺写入格式 | **不缺**,`p645dict` 的 tuple + 文件头图例可反查 |
| 缺写后是否生效 | 真缺,**只能手册或实测**(EEPROM 延迟、是否需要重启) |
| 缺密级/权限 | 真缺。tuple 里末尾两个数(`'96'` / `'03'`)疑似**权限等级**,含义未证实 —— 要标成"疑似,待确认",不要当已知 |
| `通信模式` | 真缺,全仓 0 命中 |

## 期望

1. 把 5 个参数(以及同族的 `0400xxxx` 计量单元参变量)的**写入格式**按 tuple 补进 `configs/import/params.yaml` 或新建 `configs/import/writes.yaml`:DI / 长度 / 类型 / 图例换算(如 `Date` = `YYMMDDWW`,每个 YY 是 BCD 一字节)/ 出处行号
2. **`'96'` / `'03'` 两个字段**:标成「疑似权限等级,含义未证实」,不许猜成"密码"
3. 更新 43 的「要资料清单」:删掉"写入格式"一项,把"写后是否生效"和"密级"提到最前 —— 前者决定会不会出现"写成功但设备没变"
4. 保持不变:**`mode: real` 守卫仍然挡着**;DI 有出处 ≠ 地址已就位

## 验收

- [x] 5 个参数各有 DI / 长度 / 类型 / 图例换算,`source` 指到 `p645dict.py` 行号
- [x] 至少一条端到端:按提取出的格式编码一个 `Date`(4 字节 BCD)与一个 `String`(6 字节),**用 `protocol/values.py` 或写路径的单测证明字节对得上**(不连设备)
- [x] 机器校验 `check_import_sources` 覆盖新条目;改错一位必须红
- [x] 「要资料清单」更新后只剩三项:写后生效性 / 密级含义 / 通信模式
- [x] 全量与基线不回归(`docs/baseline.md`:604 passed / 7 skipped / 611 collected)
- [x] 明确写出:DI 是 DL/T645 的,**不能当 Modbus 寄存器地址**

## Comments

- 2026-10-20 出票依据:`p645dict.py:4915-4920` 的实际内容 + 文件头 1-20 行的类型图例;43 的产物把"写入格式"列进了缺口,但它在同一条 tuple 里。
- 与 43 的关系:不是返工,是**把 43 里"要手册"的一项降级为"已可反查"**。清单越短,越可能真的拿到 —— 这是本票唯一的实际价值。

- 2026-10-22 完成。产物:`configs/import/writes.yaml`(24 条 DI:5 个参数用到的 9 个 + `0400xxxx`
  计量单元参变量同族;每条带 `source: common/p645dict.py:<行号>`)、`tests_unit/test_p645_write_format.py`
  (5 条:图例位数 × 长度交叉、Date 4 字节 BCD、String 6 字节、换值不变长、超范围报错)。
  `tools/check_import_sources.py` 新增 writes.yaml 一类断言(111 条逐行 + 25 条反向),改一位实测红。
  「要资料清单」缩到三项(写后生效性 / 密级含义 / 通信模式),见 `docs/upstreams.md` §6。
  未改 `src/`;`'96'`/`'03'` 一律记为「疑似权限等级,含义未证实」;`mode: real` 守卫没动;
  DI 是 DL/T645 的,不能当 Modbus 寄存器地址。

- 2026-10-22 已完成 `3f976e1` / 合入后 **609 passed / 7 skipped**,收集 616。`src/` 一行未动。
- 9 个 DI 落进 `configs/import/writes.yaml`(时钟 4/3/7 字节 Date/Time/DateWeekTime、表地址 6 字节 String、红外与 RS485 波特率 1 字节),另有同族 `0400xxxx` 共 24 条;`通信模式` 反向断言(上游 0 命中)。
- **字节向量**:`Date 25102001 → 01 20 10 25`、`String 000000000001 → 01 00 00 00 00 00`,用的是现成的 `values.encode_value` + `dlt645.build_read_request`,没新增 src 代码。
- **机器校验我独立验过会红**:把 `source` 的 `:4915` 改成 `:4916` → 退出码 1;还原后 79+25 全绿。未设 `UPSTREAM_DIR` 时 skip 并打印用法。
- `'96'` / `'03'` 记成「疑似权限等级,含义未证实」(上游第 23 行的字段注释与 12 元组对不上),**没有实现成常量**,避免下一个人拿它去比对填值。
- `mode: real` 守卫仍然挡着(14 个量地址仍 `TODO`)—— DI 有出处 ≠ 地址已就位;DI 是 DL/T645 的,不能当 Modbus 寄存器地址。
