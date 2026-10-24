# 21 归因的「没有有效结论」把范围外的设备也算进去,规模下会淹没真问题

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `tools/attribute_round.py` 的判定很好用(四类归因 + 无有效结论 + 规模),但它分不清「不在这一轮范围」和「在范围内却没测到」。这两件事在规模下差别巨大:前者是正常,后者是静默漏测 —— 而这个平台的目标是几百台设备。

## 复现(实测)

只跑场景层两条用例:

```
.venv/Scripts/python.exe -m pytest -q tests_scenario          # 2 passed
.venv/Scripts/python.exe tools/attribute_round.py
```

输出:

```
[没有有效结论] 5 条(跳过 / 筛掉 / 没跑到 / 证据判断不了 —— 都不算通过)
  设备 bat_01        证据 bat_01.jsonl        设备证据文件是空的:这一轮根本没碰过它
  设备 inv_02_via_ipc 证据 inv_02_via_ipc.jsonl 设备证据文件是空的:这一轮根本没碰过它
  设备 inv_03        证据 inv_03.jsonl        设备证据文件是空的:这一轮根本没碰过它
  设备 meter_01      证据 meter_01.jsonl      设备证据文件是空的:这一轮根本没碰过它
  1 条被收集的用例在证据里没有读数 ... tests_scenario/...::test_scenario_fails_...
```

一轮只跑了 2 条场景用例,于是 5 台 dut 里有 4 台进「没有有效结论」。这 4 台**本来就不在这轮范围内**(场景用例只点了 `grid_sim_01` + `inv_01`),却和「该测但没测到」显示成同一档。

按现在配置(6 台设备 / 5 台 dut)只是 4 行噪声;**按目标规模(几百台)就是几百行**,把真正的「在范围内却没读数」埋掉。而那个才是这张工具的立身之本。

## 根因

`run-scope.json` 只记用例(收集 / 筛掉 / 跳过),**不记每条用例落在哪些设备上**。归因层于是只能退化成「证据文件是空的 = 这一轮根本没碰过它」(见 `attribution.py:125` 附近),而空文件的来源其实有三种:不在范围、范围内没跑到、设备装配了但用例没读它。

设备范围在收集阶段是已知的(`tests/conftest.py` 里 `_parametrized_device_ids(item, device_ids)` 就是干这个的),只是没写进 `run-scope.json`。

## 期望

1. `run-scope.json` 的 `collected` 项加 `devices: [...]`(按设备参数化的用例就有;场景用例从 `scenario-*.json` 的 `participants` 取,或收集期从显式点名的 id 推不出来就留空并在报告里写明「场景用例按 scenario 文件归因」)
2. 归因把「没有有效结论」拆成两档,显示上分开:
   - **不在本轮范围**:只给一行计数(`另有 N 台设备不在本轮范围内`),不逐条列
   - **在范围内却没有读数**:保留现在的逐条列出,这一档才是要人看的
3. 「这一轮跑了 N 条」的统计不受影响;`clean` / 有失败项的顶行结论也不变

## 验收

- [x] `run-scope.json` 里每条按设备参数化的用例带 `devices`
- [x] 只跑 `tests_scenario` 时,归因输出里不再逐条列那 4 台范围外的 dut,只给一行计数
- [x] 构造一轮「在范围内但证据为空」的情况(比如用 `--select` 选中某台设备、用例中途崩),该设备仍逐条列出并标明是漏测
- [x] 现有归因用例(`tests_unit/test_attribution.py` / `tests/test_attribution_records.py`)相应更新,其余不变
- [x] 全量套件仍全绿,skip 数不变

## Comments

- 2026-10-20 出票依据:合入 `45a49dc` 后实跑上面两条命令。当前清单 6 台设备 / 5 台 dut / 4 个型号(用 `parse_manifest` 读出来的),所以这轮 4 行噪声 = 80% 的「没有有效结论」都是范围外设备。
- 这张工具本身的判定是对的、读数也只从证据里取(`attribution.py:200` 读 `record["evaluation"]`,不重算),问题只在范围这一维缺失。修它不需要动判定逻辑。
- 2026-10-21 票价措辞已修正:验收第 3 条原来要求「标明是漏测」,但证据只有「设备在范围内 + 这轮没有读数」两件事,推不出「该测却没测到」—— 只跑接入用例(`pytest -k connects`)时 3 台设备会被错报成漏测。现已改成中性事实:「这一轮这台设备上没有任何读数记录」,并列两种可能(选中的用例本来就不读数 / 用例中途失败或被中断),附该设备的用例 nodeid 与 `run-scope.json#collected` 供核对;`attribution.json` 里该档 `kind` 由 `empty_device` 改为 `no_readings`。

- 2026-10-21 已完成 `855ff54` + `591c7f5` / 合入后 309 passed / 6 skipped。范围外设备收敛成一行计数,范围内空读数逐条列出。
- **票价自身的错误已修正**:原验收项写"标明是漏测",但证据里只能看出"这台设备这一轮没有任何读数记录",分不出"用例本来就不读数"与"用例中途失败"。第一版照票实现后,跑 `-k connects` 会把 3 台根本没漏测的设备判成漏测。现改为中性事实 + 并列两种可能 + nodeid 线索,`attribution.json` 字段改为 `no_readings`。
- 残余另出票:`22-ran-counts-unexecuted-cases.md`。
