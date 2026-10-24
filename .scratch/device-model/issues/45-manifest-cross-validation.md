# 45 装配期交叉校验:判据量与能力必须落在点表里(Must fix)

Status: resolved
Type: task
Blocked by: None

**What to build:** 清单里"判据集判的量"和"型号声明的能力"都必须能在该型号点表的读数键里找到。现在两者都不校验,一路过到读数那一刻才 `KeyError`。

## 实测(两条都未拦)

```python
# A: 判据量 '不存在的量' 不在点表
parse_manifest(base)   # 不报错;失败推迟到 domain/criteria.py:139
# B: capabilities 声明了 '有功电量',点表只有 '电压'
parse_manifest(base)   # 也不报错
```

`manifest.py` 里没有任何"criteria/capabilities ↔ point_table"的交叉校验(只有 `_parse_capabilities` 校验形状,第 901-903 行)。

## 为什么是 Must fix

- xlsx 导入会自动生成判据量名,拼错一个就是这条路
- 失败点在**第一条读数**,而那时台架已接、电源已开
- 对 capabilities 更阴:能力声明的作用是"没有就跳过用例",声明了却读不到 = **让跳过机制说谎**

## 期望

`parse_manifest` 里按型号比一次,两条一起:

1. `{c.quantity for c in criteria.criteria} ⊆ point_table.keys()` → 缺哪个量、属于哪个型号,一次报全
2. `model.capabilities ⊆ point_table.keys()` → 同上

错误消息要能直接改:点名型号、缺少的键、点表里有什么。

**注意边界**:`derived_criteria` 的 quantity 是跨设备的派生量,不属于单台点表,**不在这条校验里**(它已有参与方校验)。

## 验收

- [x] 判据量不在点表 → 装配期 `ValueError`,消息含型号 + 缺失量 + 可用读数键
- [x] 能力不在点表 → 同上
- [x] 两者都缺时一次报全(不是报第一个就停)
- [x] `derived_criteria` 的量名不受这条影响(有专门用例证明不被误拦)
- [x] 现有清单与全部用例仍绿(基线 609 passed / 7 skipped)
- [x] 出厂 `configs/devices.yaml` 与 `devices_scale.yaml` 都还能装配

## Comments

- 2026-10-20 出票依据:ponytail-audit 体检项 1+3;上面两条 `parse_manifest` 探针是实跑的,均未报错。
## 补一条实测(2026-10-20):`criteria_override` 也在校验范围

`criteria_override` 走的是另一条解析路径(设备级行内覆盖,`_resolve_criteria` 里合并),**同样没有对点表做交叉校验**:

```
override 判'不存在的量' → 装配期通过
override 判点表里有的量 → 装配期通过
```

所以修的时候要覆盖**三个来源**,不能只在校验 criteria_profiles 的地方加一处:

1. 型号引用的 `criteria_profiles`
2. 设备级 `criteria_override`
3. 型号 `capabilities`

合并之后再校验(或三个来源各自校验,但同一个设备最终生效的判据集必须整体落进它的点表)。**推荐在 `_resolve_criteria` 产出的最终 `CriteriaProfile` 上校验一次** —— 那样覆盖与基础集都自动覆盖,而且不会因为"覆盖掉了一条"而漏判。

## 验收补两条

- [x] `criteria_override` 判不存在的量 → 装配期报错(点出设备 id + 型号 + 缺失量)
- [x] 覆盖把基础集里那条**替换掉**之后,只校验最终生效的那条(被覆盖掉的不再要求存在)

- 2026-10-22 已完成(`31f2c1b`),616 passed / 8 skipped(新增 8 条;这份解释器没装 openpyxl,`tests_unit/test_import_cases.py` 那 1 条 xlsx 用例照旧 skip,基线在本机是 608/8)。
  - **口径改了位置**:校验不在 `parse_manifest`,在组合根 `CompositionRoot.build` 里。原因:vendor_ascii 型号的判据量**不是**读数键,而是驱动把读数键映射成的聚合根参数名(`有功功率 → output_power_w`,`drivers/inverter_driver.py:24`),manifest 拿不到这张表(反向 import 成环)。按票面字面规则跑,出厂三个清单的逆变器 / 电池型号全挂。
  - 于是"能判的量"取并集:点表读数键 ∪ 驱动自述的量名(`drivers/vendor_ascii.py::PointReadings.quantities`,从运行时同一张映射派生,不另抄)。另加一条:驱动映射表要读的键必须是点表读数键,否则自述会变成新的假通过入口。
  - 报全:一个 `ValueError` 按型号分组列出"声明的能力缺哪些 / 判据集判的量缺哪些 + 点表里有什么";`criteria_override` 自动覆盖,因为校验跑在合并后的最终 profile 上。
  - `derived_criteria` 不在其中:它的量跨设备算出来,不属于任何一台点表(用例 `tests_unit/test_manifest_quantities.py::test_derived_criteria_are_not_checked_against_the_point_table`)。
  - 出厂 `configs/devices.yaml` / `devices_scale.yaml` / `devices_mock.yaml` 三份都还能装配。

- **校验发生在 `CompositionRoot.build()`,不是 `parse_manifest()`。** 原因:判据量要是"合并 override 之后的最终 profile"才准,而合并是组合根做的。**审的人用 `parse_manifest` 探针会得到假的"没拦住"** —— leader-0 第一次就是这么误判的。
