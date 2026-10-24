# 24 `@case` id 无唯一性校验,而参数化会把同一个 id 复制成多个节点

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `@case(id=...)` 的编号是归因和证据的唯一身份,但现在**收集期不查唯一性**,而且场景层一旦按设定值参数化,同一条用例会变成多个节点、共用一个 id —— 证据里出现同 id 的多条记录,归因分不开谁是谁。顺带把「场景层参与方不自动展开」写进 ADR 0005,免得下一个人以为加设备就会自动多测。

## 现状(实测)

```
$ grep -rn "重复\|unique\|重号" src/testplatform/cases.py testplatform_plugin.py
(零命中)
```

`cases.py` 只做了 `setattr(test_function, CASE_ATTR, identity)`;`case_of()` 读回来。没有任何地方比对两个函数是否用了同一个编号。

同时 `tests_scenario/test_grid_frequency.py` 的写法是这样:

```python
GRID, INVERTER = "grid_sim_01", "inv_01"
SETPOINT_V, SETPOINT_HZ = 230.0, 50.0

@case(id="S_00_00_01_000001_01", clause="GB/T 37408 5.2.1", tags=["场景"])
def test_inverter_holds_the_grid_frequency_setpoint(composition_root, request):
```

只有一个设定值,所以现在是对的(先别参数化,ADR 0005 的"三条用例看不出共性就别抽"是同一个立场)。但 GB/T 37408 的频率测试天然是多点(47.5 / 49.5 / 50.5 / 51.5 Hz)。到那一天:

```python
@pytest.mark.parametrize("freq_hz", [47.5, 49.5, 50.5, 51.5])
def test_...(composition_root, request, freq_hz):   # 4 个节点,同一个 @case id
```

→ `scenario-S_00_00_01_000001_01.json` 会被第二个节点覆盖,`<设备>.jsonl` 里 4 条 `reading` 记录的 `case.id` 完全相同,归因只能说"这条用例出过问题",说不出是 47.5 还是 51.5。

## 要做三件事

### 1. 收集期唯一性校验(现在就该有,和参数化无关)

`testplatform_plugin.py` 里加一步:`case_id → [nodeid]`,出现同一个 id 挂在两个**不同函数**上时 `pytest.UsageError`,消息里列出两个 nodeid。

注意:参数化产生的多个节点**共享同一个 id 是合法的**,不能按 nodeid 去重(那会把正常的参数化判成冲突)。判据是「同一个 id 是否挂在两个不同的测试函数上」——用 `item.function` 或 `item.originalname` 归并。

### 2. 参数化时的身份规则:不动 `@case` id,用节点 id 区分

**推荐**:`@case.id` 表达"用例身份"(标准条款 + 编号),参数表达"这一轮跑的哪个取值"。

- 证据 `reading` 记录里 `case` 旁边已经有 nodeid 相关字段时保持;若没有,补 `"node": item.nodeid` 或 `"params": {...callspec.params}`
- `scenario-<名>.json` 的文件名从 `scenario-{name}.json` 改成带参数后缀(如 `scenario-{name}[{param_id}].json`),否则多个参数节点会互相覆盖 —— **这条是硬 bug**,现在不会触发只是因为还没参数化

把「id 不拼参数」写进 `docs/WRITING_TESTS.md`,否则每个人会发明一套自己的命名。

### 3. ADR 0005 补一句:场景层参与方不自动展开

场景用例用 `composition_root.get("inv_01")`,不走 `inverter` / `inverter_id` 泛型 fixture。后果和一致性层不一致:

| 清单里加一台逆变器 | 覆盖变化 |
|---|---|
| `tests/` 契约用例 | 自动多跑一个节点 `[inv_03]` |
| `tests_scenario/` | **一条都不多跑** |

这是**有意的**(显式点名参与方是这一层唯一必须保住的属性),但 ADR 里没写,读者会以为场景层也自动展开。在 ADR 0005 补一行,并写明何时该改用泛型 fixture:出现"同一场景要对清单里每台同类型设备各跑一遍"的真实需求时。

## 验收

- [x] 两个不同函数用同一 `@case` id → 收集期 `UsageError`,消息列出两个 nodeid
- [x] 一个函数参数化出 4 个节点 → 不报错(参数化不视为重复)
- [x] `scenario-*.json` 在参数化下不互相覆盖(文件名带参数 id,或明确在文档里禁止场景用例参数化并给出检查)
- [x] 证据里能区分同一用例的不同参数节点
- [x] ADR 0005 补「参与方不自动展开 + 何时改用泛型 fixture」
- [x] 全量套件仍全绿

## Comments

- 2026-10-20 出票依据:`grep -rn "重复\|unique\|重号" src/testplatform/cases.py testplatform_plugin.py` 零命中;`tests_scenario/test_grid_frequency.py:23-28` 的常量与 `@case` 写法;`composition_root.get(GRID)` 现状(`grep` 确认场景层未使用 `inverter` / `inverter_id` fixture)。
- 与 `23-test-layer-structure.md` 第 4 条相邻但不同:那条讲"场景用例不受 `--select model:` 筛",这条讲"身份唯一性与参数化"。可一起做,互不阻塞。

- 2026-10-22 已完成 `c6c0d04` / 合入后全绿。跨函数重复编号在收集期报错(带两个 nodeid);同一函数参数化出的多节点共用编号但场景记录按 `request.node.name` 各落一份(`scenario-…[47.5].json` 等),互不覆盖;ADR 0005 增加"参与方不自动展开"一条。
