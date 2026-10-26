# acceptance 任务没有 dut 时,产品缺陷被写成「未完成」

- repo: test-platform
- 首次发现: 2026-10-25,跑 EdgeC iems 验收时

## 现象

一个 `kind: acceptance` 的任务,清单里没有 `role: dut` 的设备(产品是 HTTP/sqlite
形态,平台驱动不了),用例真失败,但 `summary.json` 的 verdict 是 `incomplete`,
`report.md` 首屏写「未完成 —— 申报外未测 / 设备级没验到」。gate 退出码 3。

同一轮实际状态是:用例 3 失败 1 通过,设备的实时值恒 0。产品缺陷被报成「没测到」。

## 依据

`src/testplatform/reporting/report.py:20` 的设计声明:

    kind: acceptance(缺省)  没验到设备就不算验过 → ④ 不成立,落③,reason=no_device_coverage

`_verdict()`(`report.py:351-377`)只有两条路给 `failed`:

    if attribution.device_failures or any(e.get("how") == SELF_EXIT for e in product_exit):
        return VERDICT_FAILED

没有 dut → `device_failures` 永远为空 → 失败只能落进 `gaps` → `INCOMPLETE`。

## 为什么这条不能靠台架绕

绕法一(加一台假 dut)会往报告里塞一台永远没读数的设备;绕法二(把任务改成
`kind: smoke`)等于自己把验收降级。两种都是把判定问题挪到别处。

实际修法我们用了第三条:给产品找一条平台**能**驱动的对外面(EdgeC iems 的北向
Modbus 从站,unix socket 桥成 TCP),让它成为真 dut。这条能落地,但它要求每个
产品都有「平台认识的接入面」;只有 sqlite/HTTP 面的产品(如 EdgeC dcu)仍然无解。

## 待定

真正的选择:

1. 接受现状,并在文档里明确「acceptance 无 dut」的语义就是未完成 —— 需要
   `report.md` 首屏把「3 条用例失败」写在同一行,不能让读者只看结论。
2. 加一档:没有 dut 但有用例失败 → `failed`,归因栏写「失败但无设备归属」,
   和「未落证据的失败」并列。风险:失败可能来自台架/环境,会误判成产品不合格。
3. 允许清单声明「本任务的判定面不是设备」,此时用例失败直接判 `failed`。

倾向 2 + 3 组合,但需要先把「未落证据的失败」的责任边界定清。
