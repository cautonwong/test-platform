# 18 两个条款没有任何用例覆盖

Status: resolved

## 事实

`catalog` 报两个 `clause_without_cases`:`DLT645-4.5.3`、`GBT37408-5.2.5` —— 清单里声明了要验,实际没有一条用例引用。

## 查证结论(实现方)

- `configs/clauses.yaml` 里这两条的 `summary` 是**用法概括**(07 定的,不是原文)
- `docs/upstreams.md` / `configs/import/*` / `build/cases.json` 里**没有这两条的原文或标题**
- 现有 `dlt645_meter` / `inverter_pv_series` 点表没有能对应到它们的量

**没有依据 → 构造不出可判定的用例。写"读完就 pass"的空用例比 gap 更糟。**

## 处置

`configs/clauses.yaml` 加可选 `not_applicable: "<理由,含 TODO(来源)>"`;
`Clause` 接受该字段(未知字段仍拒收);`catalog` 把它记为 **`waived`**,不算缺口,且在 `CATALOG.md` 单列「已知豁免(验不了,理由在此)」。

## Comments

- 2026-10-23 合入 `85febe7`。**leader 自验**:`tests_unit/test_catalog.py` + `test_clause_registry.py` → 38 passed;`catalog --verify` exit 0;master 全量 **951 passed / 12 skipped**;`not_applicable` 在 yaml 里 3 处(2 条条款 + 1 处说明)。
- 守卫:把一条用例的 `clause=` 改掉 → 原条款立刻变回缺口(实跑报 `['DLT645-4.5.4']`);豁免**必须写理由**,空字符串照样算缺口。
- **边界要说清**:产物写的是"没有原文、没有对应量",**不是"标准不要求"**。来源待补;拿到原文后删掉 `not_applicable` → 缺口重现 → 那时补用例。
- 不写假用例凑绿 —— 这是票面禁止项,遵守了。
