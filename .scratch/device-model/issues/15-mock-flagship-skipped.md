# 15 整链证明长期 skip:mock 的旗舰用例默认永远不跑

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** `ticket/mock`(合入 `e795b20`)造出了独立 Modbus RTU 从站 + 真 socket 链路 + 三种故障注入,质量很高。但它的旗舰用例 `test_mock_meter_round_trip_over_a_real_socket` 在默认跑里**永远 skip**,而它是唯一一条从头到尾证明「默认清单 → 组合根 → 点表求值 → 判据 → 证据」整链在真实 socket 上成立的用例。

这不是懒,是被端口卡住了:mock 用 `--port 0` 由系统分配端口,端口号要等进程起来读 banner 才知道,而这份用例走的是**出厂清单** `configs/devices_mock.yaml`,清单里的端口是写死的。于是只能人工起 mock 再带 `--device-config` 跑。

## 现状(实测)

```
.venv/Scripts/python.exe -m pytest -q            # 187 passed, 7 skipped in 7.04s
.venv/Scripts/python.exe -m pytest -q -rs        # 7 条 skip 里第 7 条就是它:
# tests/test_mock_meter_end_to_end.py:96: 没有 mock 在跑:meter_01 当前 mode=simulator,
#   走进程内模拟器而不是真实 socket;带 --device-config configs/devices_mock.yaml
#   并先起 tools/mock_modbus_meter.py 才会跑
```

同文件的另外 5 条故障用例**默认就跑**,而且已经证明真 socket + `RealTcpTransport`(它们自己起 mock、自己写临时清单)。所以“真实链路”不是没人测,缺的是**整链**那一条。

## 根因与修法

根因只有一条:端口动态 ≠ 清单里的静态端口。而这套办法**文件里已经写好了** —— `_mock(...)` 上下文管理器给端口,`_write_manifest(tmp_path, port)` 拿它现造清单。旗舰用例没用这两样,是因为它还想顺带证明“出厂 mock 清单可用”,两件事绑在一条用例里,只能靠人工。

拆成两件事:

1. **整链用例做成自包含**(默认跑):复用同文件的 `_mock` + `_write_manifest`,断言链上每一步 —— `RealTcpTransport` 实例、读数按点表读出、判据结论、证据里 `frame` 双向 + `reading` 一条。用例数量不增,增量耗时约 1s(和兄弟用例同量级)。
2. **出厂清单 `configs/devices_mock.yaml` 的可用性单独一条**(不连 mock 也能跑):只断言这份清单能装配、型号/点表/判据引用自洽、端口字段存在。真连不上的部分留在人工跑法里,写进 README。

## 为什么值得做

- 这仓库自己的规矩是「0 条的绿色会让人以为测过了」。一条永久 skip 的头牌用例是同一个味道:报告上看着有,实际从没跑过。
- 整链是唯一能同时抓住「清单解析 + 拓扑排序 + 点表求值 + 判据求值 + 证据落盘」五处回归的用例。其余用例各测一段。
- 上次票 10 的分组也是「端到端证明只活在人工脚本里」,同一个问题第二次出现。

## 套件时长(顺带记录,不要求改)

`a82af5d` 时是 2.65s,现在是 7.04s(+166%),来源是 5 条用例各起一个子进程。7 秒可以接受;整链用例自包含后大约再加 1s。**如果再往上长到 15s 以上,就该给这类真 socket 用例标 `slow` 并默认排除**,而不是让默认套件慢慢变慢到没人愿意跑。这条现在只是提醒,不是验收项。

## 验收

- [ ] `test_mock_meter_round_trip_over_a_real_socket` 在**不加任何命令行参数、不预先起 mock** 的情况下真的跑起来(不再 skip),并断言整链五步
- [ ] 该用例的断言里仍有 `isinstance(transport, RealTcpTransport)`,不能退化成走进程内模拟器
- [ ] `configs/devices_mock.yaml` 有独立用例覆盖装配与引用自洽(不依赖有 mock 在跑)
- [ ] 默认套件 skip 数从 7 降回 6 或更少;`-m pytest -q` 仍然全绿
- [ ] README 里“人工起 mock 再跑”的跑法保留,并注明它验证的是出厂清单而不是整链

## Comments

- 2026-10-20 出票依据:对 `e795b20` 做只读审核。上面两条命令与 skip 原因是实跑 `-rs` 得到的;`test_mock_meter_end_to_end.py` 单文件连跑两次均 `6 passed, 1 skipped`(6.85s / 4.81s),无 flake。
- 同一批审核里还发现:`test_mock_timeout_fails_loudly_instead_of_returning_a_stale_value` 把“超时只落一条 send”锁成了期望,与 `13-evidence-gaps.md` 第 1 条的改法冲突,已在 13 里加注,不在本票范围。
- 15 已完成:`a82cf9a` / 合入后整链用例默认跑(PASSED 不再 SKIPPED),出厂清单单独一条。201 passed / 6 skipped。
