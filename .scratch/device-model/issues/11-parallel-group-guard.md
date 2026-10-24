# 11 并行分组已经落地却没人看守:票面说没做、脚本编号错、改坏无告警

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 并行分组的机制、落点日志、端到端检查脚本都已经在仓库里,而且实测是对的;但 `tickets.md` 的票 8b 四条还是空框,检查脚本自称 “ticket 8”,分组逻辑本身没有任何自动断言。结果是:**这套东西今天坏了,也没人会知道**。补齐票面状态 + 一个不 spawn 子进程的守卫。

## 现状(我实测的证据,不是推断)

```bash
.venv/Scripts/python.exe -m pytest -q                        # 181 passed, 6 skipped
.venv/Scripts/python.exe -m pytest -q -n 4 --dist loadgroup  # 181 passed, 6 skipped,连跑 3 次同结果
.venv/Scripts/python.exe tests/parallel_topology_check.py    # 12 passed
# gw0 = terminal_01 + meter_a + meter_b
# gw1 = terminal_02 + meter_c + meter_d
```

也就是说票 8b 的四条(组内串行、组间并行、连跑稳定、每单元独立装配收尾)**在代码上已经实现并通过实测**,但票面仍是四个 `- [ ]`。

## 三个缺口

### 1. 票面与代码不同步(最贵)

`tickets.md` 票 8b 是唯一还开着的票,但它的实现已在 `tests/conftest.py:159-181`(`_mark_topology_groups` / `_topology_group`)+ `tests/conftest.py:194+`(`_record_parallel_assignment`)+ `pyproject.toml` 的 `parallel = ["pytest-xdist>=3.8"]`。下一个接手的人按票面会从零重做一遍。

先按下面的步骤复验,再勾票;哪条验不过就如实留空并注明原因。

### 2. 分组逻辑零自动守卫

`tests/parallel_topology_check.py` 是唯一证明“同一 `via:` 下的设备落同一 worker”的东西,而它:

- 名字不以 `test_` 开头 → 默认套件不收集
- 它自己 spawn `pytest -n 4`,放进默认套件会套娃,所以也确实不能直接收集
- 仓库没有 CI,只有 `README.md:60` 一句“跑 `python tests/parallel_topology_check.py`”

于是 `_topology_group` 的推导(沿 `via:` 链跟到根、链中间层也归同组)改错了,默认套件仍然 181 passed。

期望:**分组键的推导用纯函数单测锁住**(假 `Manifest` + 假 item,不 spawn 子进程、秒级跑完)。端到端脚本保留,给人和排障用,不承担守卫职责。

### 3. 脚本自称 ticket 8

`tests/parallel_topology_check.py:1` 写「ticket 8 的实测脚本」,实际是票 8b 的验收脚本 —— 插号 08(编码解码契约)之后没更新。同文件 `tests/parallel_topology_cases.py:1` 的引用也一并核对。

## 验收

- [ ] 票 8b 四条按上面的命令逐条复验;通过的打勾,不通过的不打勾并在票面写明卡在哪
- [ ] `_topology_group` 有纯函数单测:多层 `via:` 链(终端 → 抄表器 → 表)跟到根、无 `via:` 的设备成自成一组、`via:` 不存在的在装配前报错
- [ ] 有单测断言“同一 `via:` 下的两条用例拿到同一个 `xdist_group` 标记,不同终端拿到不同标记”(假 item,不 spawn 子进程)
- [ ] `parallel_topology_check.py` 与 `parallel_topology_cases.py` 的票号引用改成票 8b
- [ ] 默认套件仍是 `181 passed, 6 skipped` 加上新增的这些用例,其余一条不变

## Comments

- 2026-10-20 出票依据:对 HEAD `e12a409` 做只读质量核查时发现。上面每条命令都是我在仓库根目录实跑的,`-n 4 --dist loadgroup` 连跑 3 次结果一致(5.28s / 4.19s / 2.85s)。
- 顺带记录,不在本票范围:ADR 偏薄(`docs/adr/0002` 4 行、`0001` 5 行,缺 Context / 备选方案);`tests_scenario/` 只有 `README.md` 而 `testpaths` 仍包含它。
- 2026-10-20 上游 `a82af5d` 已解决本票第 1、3 条:票 8 四条已勾并记了实测数据;脚本自称 “ticket 8” 在票目重编号(并行 = 票 8)后自洽。**唯一未解决的是第 2 条(分组逻辑零自动守卫)**,已并入 `12-closeout-quality.md`,本票关闭。
- 11 已完成:1f2386e(并入 issue/12 处理)。分组逻辑现在有常驻守卫 `tests/test_topology_grouping.py`(8 条);把 `_topology_group` 改成常量后 3 条立刻红。检查脚本编号错位一并理顺。
