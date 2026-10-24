# How-to

按「你现在要干的那件事」找文件,不用从头读。概念与术语见 `CONTEXT.md`,定过的决策见 `docs/adr/`,`docs/WRITING_TESTS.md` 讲用例的四种写法。

| 你要做的事 | 读这个 |
|---|---|
| 加一台设备、加一个型号、接一个新规约 | [`add-a-device.md`](add-a-device.md) |
| 写一个协议模块和一个 driver | [`add-a-protocol-and-driver.md`](add-a-protocol-and-driver.md) |
| 写一条跨设备的场景用例 | [`add-a-scenario.md`](add-a-scenario.md) |
| 跑完之后判定到底是谁的问题 | [`read-evidence.md`](read-evidence.md) |
| 把平台接到真实台架上 | [`run-on-real-hardware.md`](run-on-real-hardware.md) |

## 三件无论做什么都要知道的

1. **默认 `mode` 是 `real`**。没写 `mode: simulator` 的设备,第一次跑就会去连真实端口。新加设备先写 `mode: simulator`。
2. **清单在装配前整份校验**。型号引用、规约×接入方式配对、点表字段、判据键、设备实例的键、`via:` 拓扑,任何一处错都在装配前报错并点名是哪一条 —— 不会静默取缺省值。
3. **失败了先看证据,不要先猜**。`evidence/<会话>/` 里每台被测设备一份 JSONL,先跑 `python tools/attribute_round.py`,它会把结论分成「设备不合格 / 测试无效 / 平台错误 / 证据损坏」四类。

## 改完之后至少跑这三条

```bash
python -m pytest -q                                    # 全量,默认带 60 秒单用例超时
python -m pytest -q -n 4 --dist loadgroup               # 并行(漏 --dist loadgroup 会直接报错)
python -m pytest -q --count=3 tests/test_meter.py       # 和你改动相关的那部分连跑三次
```

`--count=3` 验的是「反复跑还稳」,`-p randomly` 验的是「顺序无关」,`--cov` 验的是纯逻辑层覆盖率 —— 三条各自验一件事,别混成一条命令。
