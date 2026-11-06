# 写第一条用例(10 分钟)

目标:从零到一个跑得出报告、带证据的用例。只写 Python,不学自制描述语言。

## 0. 准备

```bash
pip install -e .                 # 源码形态;或用 npx @cautonwong/testplatform init
testplatform --version           # 能打印版本就对了
```

## 1. 生成一个能跑的项目

```bash
testplatform scaffold case --out my-tests
cd my-tests
testplatform run tasks/case_sample.yaml --root . --evidence-dir reports
echo $?                          # 0
```

`reports/case_sample/summary.json` 里 `kind` 是 `smoke`。scaffold 生成的是"验通路"的任务,
所以 `testplatform gate reports` 会返回 6(这一批没有验收任务)。这是对的,不是失败。

## 2. 写第一条用例

新建 `tests/test_voltage.py`:

```python
from testplatform.cases import case


@case(id="voltage-is-positive", clause="SCAFFOLD-1", tags=["样板"])
def test_voltage_is_positive(composition_root):
    device = composition_root.get("dev_01")
    device.connect()

    reading = device.read_all()

    assert reading.passed, reading.breaches
    assert reading.readings["电压"] > 0
```

跑它:

```bash
python -m pytest tests -q -k voltage
```

四条规则:

- `id` 是唯一身份。用户项目可以用可读字符串;平台自己仓库里的用例沿用上游编号。
- `clause` 是 `configs/clauses.yaml` 里的键。新增一条条款就加一行;确实没有受控条款时写
  `free: <原文>`。
- `read_all()` 在电表、逆变器、电池上都有 `.readings`、`.passed`、`.breaches`。
  终端没有验收判据,访问 `.passed` 会给出明确错误,不要拿电表的判据判终端。
- 契约用例用泛型 fixture(`meter`、`inverter`、`battery`),一台设备展开成一个节点;
  见 `docs/WRITING_TESTS.md`。

## 3. 让它进任务

任务文件里的 `select` 决定跑哪些用例。改成你的标签:

```yaml
select: tag:样板
```

要进验收门禁,`kind` 写 `acceptance`(缺省就是)。`smoke` 只验通路,不参与验收。

```bash
testplatform run tasks/case_sample.yaml --root . --evidence-dir reports
echo $?                          # 0 合格 / 1 不合格 / 2 测试无效 / 3 未完成 / 4 平台错误
testplatform gate reports        # CI 判定:0 合格 / 6 没有验收任务
```

## 4. 读结论

- `reports/<task>/report.md` 首行是理由,不是套话。
- `evidence/<时间戳>/<设备id>.jsonl` 是原始报文、解码读数、判据结论。
- 想按设备/步骤归因:`python tools/attribute_round.py evidence/<时间戳>`。

## 5. 常见卡点

| 现象 | 原因 | 改法 |
|---|---|---|
| 收集期报 clause 不在清单 | 条款 id 没写进 `configs/clauses.yaml` | 加一行,或写 `free: 原文` |
| 终端读数上 `.passed` 报错 | 终端没有验收判据 | 只断言 `.readings`;终端不判合格 |
| 0 passed,或报告说设备级 0/0 | 用例没用泛型 fixture,或任务 `kind: smoke` | 用 `meter` 等 fixture;验收任务写 `acceptance` |
| `--select` 一条不剩 | 筛选条件不匹配 | 插件会报错并列出可用值 |
| 改了点表却读到旧值 | 设备清单或模拟寄存器没换 | 换 `--device-config`;先跑一次全量,别拿旧的 `--lf` 缓存当结论 |

写更多用例、编号与条款的完整规则见 `docs/WRITING_TESTS.md`。
