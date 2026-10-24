# 写一条场景用例

场景层是 **pytest 函数**,不是 YAML 步骤表 —— 形状由 `docs/adr/0005-scenario-layer-shape.md` 定死。这里只讲怎么做。

前置:一台被测设备合格不代表场景通过;场景的点是**多台设备联动**。只动一台设备、只读一个数的,写在一性测试层(`tests/`),不要放这儿。

## 步骤

1. 目录 `tests_scenario/`,文件名 `test_<场景>.py`。
2. 参与方**在函数里按 id 显式点名**,`role` 从设备清单取,不重复声明:

```python
grid = composition_root.get("grid_sim_01")
inverter = composition_root.get("inv_01")
```

3. 每一步记成一个 `ScenarioStep`。`passed` 必须是**实际求值**的结果,不能手写 `True`:

```python
from testplatform.reporting.scenario import ScenarioStep, record_scenario

steps = [
    ScenarioStep("grid_sim_01", "设定电网 230V/50Hz", passed=setpoint_ok, detail={"applied": setpoint}),
    ScenarioStep("inv_01", "读数满足判据集", passed=evaluation.passed, detail={"breaches": [...]}),
]
```

4. 落盘。`participants` 是 `id → role`,调用前先从组合根取:

```python
record_scenario(
    composition_root.evidence,
    name=request.node.name,
    participants={"grid_sim_01": composition_root.role_of("grid_sim_01"), "inv_01": ...},
    reading_keys={"inv_01": ["有功功率", "电压", "频率"]},
    steps=steps,
    identity=case_of(request.node),
)
```

5. 挂 `@case(id="S_..", clause="<条款 id,见 configs/clauses.yaml>", tags=["场景"])`。场景用例不按设备参数化,所以适用矩阵管不到它,靠 `--select tag:场景` 挑。

6. 加一条**反例**:用 `tmp_path` 自己造证据目录,证明单台设备不合格时场景结论是"不通过"。参考 `tests_scenario/test_grid_frequency.py` 里的 `test_scenario_fails_when_one_dut_reading_breaks_the_criteria`。

## 结论怎么算出来的

`verdict_of()` 看**第一个不成立的那一步落在谁身上**:

- 落在 `role: dut` → 场景不通过,**设备不合格**
- 落在 `role: support` → 结论是**测试无效**(台架问题),不是设备不合格

所以参与方的 role 必须对,别把周边设备当被测设备点名。

## 不要这样做

`docs/adr/0005` 有一张「不支持什么 + 什么信号才回头」的表,摘要:

| 想写 | 现在该怎么办 |
|---|---|
| 分支(按读数决定下一步) | 拆成两条场景 |
| 循环 / 扫频 | `@pytest.mark.parametrize` |
| 并发 + 时序先后 | 不支持,先把需求交回设计 |
| 等待/轮询到某个条件 | 不支持,会让结论依赖机器速度 |
| 步骤级重试 | 禁止,掩盖问题而不是修复 |
| 场景调用场景 | 抽普通函数,各自仍显式声明参与方 |

## 验证

```bash
python -m pytest -q tests_scenario                    # 正例 + 反例都要绿
python -m pytest -q tests_scenario --select tag:场景
ls evidence/<最新会话>/scenario-*.json                # 场景结论必须真的落盘了
python tools/attribute_round.py                       # 归因里能看到场景结论
```
