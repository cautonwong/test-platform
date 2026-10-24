# 30 跨设备派生量:误差公式表达不了(票 27 的 B 块)

Status: resolved
Type: task
Blocked by: 无 —— 但与 A 块(已随票 27 完成)零依赖,分开做

**要做什么:** 上游 29 条走字条款的核心判据是**误差**:

```
(被测表增量 − 标准表增量) / 标准表增量
```

这是**跨两台设备**求出来的量。判据集的 `quantity` 现在必须是**单台设备**的读数键(`criteria.py` 里 `evaluate(readings)` 的 `readings` 就是一台设备这次的读数),跨设备的量没有地方算。

## 现状

- `Criterion.quantity` → 单台设备的读数键
- `CriteriaProfile.evaluate(readings: Mapping[str, float])` → 一次读数、一台设备
- 场景层有 settle(`28` 已完成)+ 写原语(`26`),所以"台架输出 N 秒 → 两台表各读一次 → 求增量"这条链的动作部分齐了,**差的只是"增量怎么算、误差怎么判"**

## 建议的形状(供参考,不是定论)

三处要同时改,这也是当初把它从票 27 拆出去的原因:

1. **清单**新增派生量声明:参与方(按设备 id)+ 取哪两次读数(前 / 后)+ 公式(目前只有误差一种)
2. **组合根**提供"求一个派生量"的入口,判据求值仍走 `CriteriaProfile`,只是喂进去的 `readings` 由派生量构成
3. **场景记录**里要能看到:两台设备各自的增量、算出来的误差、以及判它的区间

## 硬要求

- **门槛不下降**:没有派生量声明的清单,行为与现在完全一致
- **公式只有一种就先只写一种**:不要为了"可扩展"引入表达式解析器 —— 那就是 ADR 0002 拒绝过的 DSL 换了个位置
- **误差的分子分母要写清是谁减谁**:上游条款里"被测 − 标准"的顺序写反了会让符号翻转,而符号错的结果看起来像"超差"而不是"算错了"。用例里要有一条专门锁方向
- 判据结论仍由 `Evaluation` 表达(`passed` 只被 REJECT 影响),不要为派生量另造一套结论模型

## 验收

- [x] 清单里能声明一个派生量(参与方 + 区间;~~前/后两次读数 + 误差公式~~ 留在用例代码里,理由见 Comments 2026-10-22 条)
- [x] 场景用例用它判一次误差:合格 / 不合格各一条,贴真实数字
- [x] 方向锁:把"被测/标准"写反时结果符号相反,有用例证明这条被抓得住
- [x] 没有派生量声明的清单行为不变(现有套件全绿)
- [x] 场景记录里能看到增量、误差、判它的区间
- [x] 不引入表达式解析器 / 新 DSL

## Comments

- 2026-10-22 出票依据:从 `27-derived-criteria-and-equals.md` 的 B 块拆出(A 块已完成)。拆分理由:A 块只动判据求值,B 块要动「读数来源」这一层,混做会把"一个判据怎么判"和"一个量从哪算来"搅在一次改动里。

- 2026-10-22 **已交付**(分支 `p1/derived-quantities`,提交 `7179fdd` + `a338846` + `99ff654`)。改了四处:

  - `config/manifest.py`:清单新增 `derived_criteria` 段,解析成 `DerivedCriterion(name, participants, limits)`。
    装配前查三件事 —— 参与方必须是清单里的设备 id 且至少两台、判据名不能与参与方的读数键重名、
    `limits` 只收数字 `min` / `max`(不收 `{设备参数: 系数}`:派生量是跨设备算出来的数,没有哪台设备的额定值可当基准)。
    `limits` 复用 `domain/criteria.py` 的 `Criterion`,不另造结论模型。
  - `composition/root.py`:`CompositionRoot` 带 `derived_criteria`;新增 `derived_criterion(name)` 与
    `judge_derived(name, value) -> bool` —— 区间取清单,值由用例算。
  - `tests/test_derived_criteria.py`(新,9 条):求值走清单、没有这一段时行为不变、参与方写错 / 与读数键重名 /
    只有一台参与方 / 坏区间都在装配前报错。
  - `tests_scenario/test_derived_error.py`(新,3 条):两台模拟电表(role dut + support)走字 → 算增量与误差 →
    判派生判据 → 写场景证据。合格(0.0%)、超限(+2.0% > `max: 1.0`)、方向锁(被测快 0.8%:正向 +0.8% 合格、
    反向 -0.79% 不合格)各一条。清单是 `tmp_path` 里的临时清单,没碰 `configs/devices.yaml` 与
    `tests/test_support_equipment.py`。

  清单形状(实测可用):

  ```yaml
  derived_criteria:
    有功误差:
      participants: [meter_01, reference_meter_01]   # 被测表 + 标准表
      limits: {min: -1.0, max: 1.0}                   # 单位 %,与上游 fPercent 同义
  ```

  证据里能看到(`scenario-*.json` 的第 3 步,实测):`derived: 有功误差`、`participants: [meter_dut, meter_reference]`、
  `limits: {min: -1.0, max: 1.0}`、`error_pct: 0.0`、`delta_dut: 100.0`、`delta_ref: 100.0`。

  套件:`559 passed, 7 skipped`(基线 547 passed / 7 skipped,多出来的 12 条就是上面两个新文件)。

- 2026-10-22 **票面自相矛盾的一处(按 ADR 处理)**:验收第 1 条写"清单里声明两台设备 + 前/后两次读数 + 误差公式",
  与同一张票的"不做"段(不在 yaml 里写公式)以及 ADR 0002 / 0005 冲突。交付时按 ADR:清单只有判据名 + 参与方 + 区间,
  前/后两次读数与误差公式留在场景用例的 Python 里。另:母票说"7 条勾选项",票面实际 6 条,已逐条勾。

- 遗留(不属于本票):`configs/devices.yaml` 里还没有一条真实的两表走字场景声明(那个文件正被另一个 worker 改),
  所以派生判据今天只有测试内清单在用;等台架型号落定后,在 `derived_criteria` 里加一段、再写一条场景用例即可。

- 2026-10-22 **收尾**(`a338846`):上一轮的代码逐条过了一遍,没有发现该删的抽象 —— `derived_criterion` / `judge_derived` 的签名与票 27 的示例一致,「判据名不得与参与方读数键重名」「participants 不得重复」两条校验留着:前者防证据里同一个名字既指读数又指派生量,后者防 `[DUT, DUT]` 绕过「至少两台」,现已各有用例。
  补了三条用例:同一台设备写两遍、只写一侧区间(`limits: {max: 1.0}` —— 与单台设备判据同为 `Criterion` 口径,另一侧不判)、清单里写 `formula` 被拒(公式不进 yaml 这条硬约束现在有用例盯着);另把票面行尾从 CRLF 归一成 LF(那次全文件 diff 就是这么来的),README 清单段补上 `derived_criteria`。
  另按复核意见把 `domain/criteria.py` 的 `_number` 公开成 `coerce_number`(`99ff654`)——清单层不该 import 私有名;派生判据的区间仍是闭区间 `min` / `max`,不提前支持 `gt` / `lt` / `equals`。
  本机实测:基线 `546 passed / 8 skipped`,`p1/derived-quantities` 上 `561 passed / 8 skipped`;skip 比票面写的多 1 条是本机没装 `openpyxl`(`tests_unit/test_import_cases.py`),不是这次改动。

- 2026-10-22 已完成(`7179fdd` checkpoint + `a338846` + `99ff654` / 合入 `0fb5b71`),合入后 592 passed / 7 skipped。`derived_criteria` 段:判据名 → `participants` + `limits`(闭区间,写死 min/max);**公式留在场景用例里**,yaml 里多写 `formula` 会装配期报错。
- 复用 `Criterion.evaluate`,没另造结论模型;`participants` 只做"存在 + ≥2 + 去重 + 不与读数键重名",**不查"计算有没有真用到这些设备"**(那要求清单知道公式)。
- 方向锁:`test_swapping_dut_and_reference_turns_the_error_negative` —— 区间故意不对称 `[-0.5, 1.0]`,分子分母写反得 −0.79% 掉出下限,结论翻转。
- 顺带把 `criteria._number` 公开成 `coerce_number`(清单层不再 import 私有名)。**rebase 时这里出过一次问题**:41 新增的台架前置调用点仍写着 `_number`,rebase 后整轮 `NameError`,已在合入前修掉。
