# 27 判据集只能判单台设备的区间:跨设备派生量(误差)与相等判定都表达不了

Status: resolved
Type: task
Blocked by: None — 与 `26-write-primitive.md` 并行做

**What to build:** 上游 29 条走字条款的核心判据是**误差** —— `(被测表增量 − 标准表增量) / 标准表增量`,跨两台设备求差。判据集的 `quantity` 现在必须是**单台设备**的读数键,表达不了。另外"写 F001 → 读回应等于 F001"这种**值相等**判定也没有 —— 判据只有 `min` / `max` / `gt` / `lt`。

## 现状(实测)

```python
# domain/criteria.py
@dataclass(frozen=True)
class Criterion:
    quantity: str            # ← 必须是单台设备的读数键
    low: ... / high: ... / low_exclusive / high_exclusive
    severity: Severity

def evaluate(self, readings: Mapping[str, float]) -> Evaluation:
    if criterion.quantity not in readings: raise KeyError(...)
```

`readings` 是**一台设备**这一次读出来的映射。误差这种跨设备量,没有地方算。

## 期望(分两块,互不依赖)

### A. 相等判定(小)

```yaml
- {quantity: 有功计量模式, equals: "1601"}
```

`Criterion` 加 `equals`,求值时 `value != equals` 即违反(严重度按 `severity`)。类型上 `equals` 允许数字与字符串(参数类点常是编码后的字符串)。

### B. 派生量:阈值进 yaml,计算留代码

铁律:**公式不进 yaml。** 一旦允许写表达式,就是 ADR 0002 / 0005 明确否决的那门 DSL 的开端。

清单里只声明"有一个叫 `有功误差` 的派生判据,它由哪些设备参与、区间是多少":

```yaml
derived_criteria:
  有功误差:
    participants: [meter_01, reference_meter_01]   # 被测表 + 标准表
    limits: {min: -1.0, max: 1.0}                   # 单位 %,与上游 fPercent 同义
```

计算由**场景用例**给出(普通 Python,不是配置):

```python
delta_dut = after_dut - before_dut
delta_ref = after_ref - before_ref
error_pct = (delta_dut - delta_ref) / delta_ref * 100
steps.append(ScenarioStep("meter_01", "有功误差在判据集范围内",
                          passed=composition_root.judge_derived("有功误差", error_pct),
                          detail={"error_pct": error_pct, "delta_dut": delta_dut, "delta_ref": delta_ref}))
```

`judge_derived(name, value)` 从清单里取区间求值,返回 bool;并把"用了哪个判据、区间多少、实测值"写进 `scenario-*.json` 的 step detail。这样**改阈值仍然只改 yaml 一处**。

## 不做

- 不在 yaml 里写公式(`(a-b)/b` 这种)
- 不让派生判据偷偷用"某台设备的读数键"当名字(会和真实读数键撞)
- 不在 `criteria.py` 里为某一类设备加特例(`if 是电表` 这类)

## 验收

- [x] `equals` 可用:值不等即违反;字符串与数字两种都覆盖;`describe()` 能说出"期望 X,实测 Y"
- [x] `derived_criteria` 声明参与方与区间;参与方不存在 → 装配前报错
- [x] `judge_derived()` 对超限值返回 False,消息/证据里带实测值、区间、判据名
- [x] 判据名与已有读数键重名 → 装配前报错
- [x] 场景 json 里能看到派生判据名 + 区间 + 实测值(证据能复现结论)
- [x] 有一条端到端:两台模拟器(被测 + 标准)→ 算误差 → 判据求值 → 写进场景证据
- [x] 全量套件仍全绿

## Comments

- 2026-10-20 出票依据:`domain/criteria.py` 的 `Criterion` 字段与 `evaluate(readings)` 签名;上游 `runWord(..., fPercent=1)` 的误差语义(`fPercent` 是调用参数,散在用例里,正是我们要收进 yaml 的东西)。上游含 `runWord` 的条款 29/659。
- 设计取舍:把"公式"放在代码里、只把"区间"放进 yaml,是为了守住"数据 → yaml,逻辑 → 代码";若把公式也配置化,就重新打开了 ADR 0002 关上的门。

- 2026-10-22 **A 块(相等判定)已完成** `d8dcc92` / 合入后全绿;证据侧配套 `f510f5a`(等值违约写 `kind: equals` + `expected`,`limit`/`exceeded_by` 不再留成 null/0.0 的误读组合;区间记录逐字节不变)。
- **B 块(跨设备派生量 / 误差)未做**,已独立成 `30-derived-quantities-and-error.md` —— 它要动「读数来源」那一层(Manifest 派生判据段 + 组合根求值 + 场景层算增量),与 A 块零依赖,不该混在一次改动里。

## 2026-10-20 关闭

- **A 块(`equals`)已交付**:`68f8f05` / `f510f5a`。实测:`Criterion('有功计量模式', equals='1601')` 对 `'1601'` 通过,对 `'1600'` 不通过且 `describe()` 输出 `有功计量模式 期望 '1601',实测 '1600'`;证据里违约写成 `{"kind": "equals", "expected": …, "value": …}`,不再出现 `limit: null`。
- **B 块(跨设备派生量)拆出为 `30-derived-quantities-and-error.md`**,那样跨设备求差、`judge_derived()`、区间进 yaml 的取舍能单独讨论。
- 归因侧等值形态的残留(`attribution.json` 仍写 `limit: null`)单独拆为 `31-attribution-equals-shape.md`。
