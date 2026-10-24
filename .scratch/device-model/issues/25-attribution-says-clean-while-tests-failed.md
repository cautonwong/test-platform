# 25 规模清单跑出 2 failed + 3 errors,归因却报 clean

Status: resolved
Type: task
Blocked by: None — 这条是归因层的正确性缺陷,优先于任何新功能

**What to build:** 在 265 台的规模清单上跑一轮,pytest 报了 5 个非通过项,`tools/attribute_round.py` 的结论是 `clean —— 没有失败项`。归因层的存在意义就是"报告只断言证据支持的东西",现在它把非通过项弄丢了。

## 复现(实测,两条命令)

```
$ python -m pytest -q --device-config configs/devices_scale.yaml
FAILED tests_integration/test_energy_system.py::test_inverter_and_battery_scenario_both_report_valid_readings
FAILED tests_scenario/test_grid_frequency.py::test_inverter_holds_the_grid_frequency_setpoint
ERROR  tests/test_inverter.py::test_inverter_via_industrial_pc_gateway          (KeyError)
ERROR  tests/test_support_equipment.py::test_grid_simulator_applies_a_grid_setpoint   (KeyError)
ERROR  tests/test_support_equipment.py::test_multiple_duts_share_one_support_instance  (KeyError)
2 failed, 1438 passed, 100 skipped, 3 errors in 39.28s

$ python tools/attribute_round.py
结论: clean —— 没有失败项:这一轮跑到的用例都没有设备不合格、测试无效、平台错误
没有失败项:0 条设备不合格 / 0 条测试无效 / 0 条平台错误 / 0 条证据损坏。
```

**pytest 退出码非 0,归因说 clean。** 这是这个仓库最忌讳的那种差异,而且它出现在负责消除这种差异的工具自己身上。

## 根因:三条

### 1. 死在「写证据之前」的失败,归因完全看不见

`run-scope.json` 现在只有四个键:`collected` / `deselected` / `skipped` / `executed`。**没有 `failed` / `errored`。**

归因只从**证据**推结论(这是对的:不重算判定)。但一条用例在 setup 或 call 里就崩了(上面 5 条都是),它一条 `reading` 都没写,于是归因既不能说它合格,也找不到"不合格"的证据 —— 它就消失了,连「没有有效结论」里都没有它的位置(那一栏只列了 100 条跳过 + 3 台范围外设备)。

### 2. 命名 fixture 换一份清单就 KeyError

`tests/conftest.py:283-302` 的 `inverter_01` / `battery_01` / `inverter_02_via_ipc` / `grid_sim_01` 都写死了默认清单的 id。换 `--device-config` 之后这些 id 不存在 → `composition_root.get()` 抛 `KeyError` → 用例在 setup 里 ERROR。

规模清单 265 台里没有 `inv_01` / `bat_01` / `grid_sim_01`,所以 5 条全炸。这不是规模清单的错:任何客户清单都会这样。**写死 id 的 fixture 应该在没有该 id 时 `pytest.skip` 并说明,而不是 KeyError** —— KeyError 会被读成"平台有 bug",而事实是"这份清单里没有这台设备"。

### 3. 100 条同因跳过逐条列,单行 4904 字符

实测:归因输出 129 行 / 24382 字符,**最长一行 4904 字符**;其中 100 行是同一个原因(`未声明能力 ['无功电量']`),只差设备 id。

`21-attribution-scope-noise.md` 已经把"范围外设备"收成一行计数,但"被跳过"没有同理处理。265 台时已经需要翻屏,几百台时这栏会盖掉真正的失败列表。

## 期望

1. **`run-scope.json` 增加 `failed` / `errored`**(nodeid + case + 简短原因),由 `pytest_runtest_logreport` 在 `when in ("setup","call")` 且 `report.outcome != "passed"` 时记录。**只记范围事实,不记判定**。
2. **归因在 `failed` / `errored` 非空时不许输出 clean**。单列一栏「未落证据的失败(5 条)」,并写明这是"pytest 报了失败但证据里没有结论,说明失败发生在写证据之前"——这恰好是排障最需要的信息。
3. **命名 fixture 缺 id 时 skip**:`pytest.skip(f"{device_id} 不在这份清单里,已配置的设备: ...")`。这与 `--force-simulator` 下 `real_hardware` 跳过的口径一致:跳过并说明理由,不报错。
4. **跳过条目按原因聚合**:同一原因只列一条 + `(×N)`,保留前 3 个不同的原因;完整清单仍在 `attribution.json`。

## 验收

- [x] 用 `configs/devices_scale.yaml` 跑一轮,归因**不再**输出 clean;5 条非通过项出现在「未落证据的失败」里
- [x] `run-scope.json` 里有 `failed` / `errored`,内容与 pytest 的 short summary 一致
- [x] `pytest -q --device-config configs/devices_scale.yaml` 的 5 条 KeyError 变成 skip(消息里列出已配置设备)
- [x] 归因输出里「被跳过」一栏在 100 条同因时不超过 5 行
- [x] 默认清单(`6 台`)下的归因输出与现在完全一致(clean 仍能正常输出)
- [x] 全量套件仍全绿

## Comments

- 2026-10-20 出票依据:上面两条命令都是在仓库根目录实跑的;`run-scope.json` 的键、归因的 `attribution.json`(129 行 / 最长行 4904 字符)由 `tools/measure_scale.py` 的 `3_证据体积` / `4_归因输出` 两项量出。
- 附带确认规模数据健康的部分:265 台装配中位 137.9ms、收集 3.84s / 1543 条、证据 1.97MB / 6600 条 / 36.9s、并行 1.25×。这几项不需要改。
- 2026-10-22 验收 #1 / #3 互相矛盾,经确认按 #3 执行(已在此留痕,不悄悄改掉):5 条非通过项里 3 条是 `tests/conftest.py` 的命名 fixture KeyError,另 2 条是在用例体内直接 `composition_root.get("inv_01" / "bat_01" / "grid_sim_01")` 的场景/集成用例。两条验收不能同时成立 —— 变成 skip 的项本来就不会出现在「未落证据的失败」里。处理:5 条统一按根因 #2 的口径转 skip(共用一个 `tests/named_device.py`),规模清单一轮因此变绿、归因 `clean`;验收 #1 的证明改由元守卫承担 —— `tests/test_attribution_meta_guard.py` 真跑一轮带必失败用例的 pytest,断言退出码非 0 且那条失败出现在「未落证据的失败」里。

- 2026-10-22 已完成 `1df13c2` + `c2ac1b2` + `1130c24` / 合入后 362 passed / 6 skipped。规模清单一轮从「2 failed + 3 errors」变成 `1473 passed / 105 skipped`,归因 `clean` 与 pytest 一致。
- **票价矛盾已修正并留痕**:验收 #1(5 条出现在「未落证据的失败」)与 #3(5 条变 skip)互斥;经确认按 #3 执行,#1 的证明改由元守卫承担(真跑一条必失败 + 一条 setup 崩的一轮)。
- 元守卫我复核过会红:把 `_note_failed` 去掉后 3 条守卫用例全红,还原后 3 passed。这是防止该缺陷再生的唯一机制。
