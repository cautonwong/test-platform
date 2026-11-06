# 场景门面设计（A3）

## 现状

一条两步场景约 50 行（`tests_scenario/test_grid_frequency.py:30-80`）：

- 参与方、role、reading_keys 手写；
- `record_evidence` 与 `record_scenario` 各调一次；
- `ScenarioStep` 的 `passed` 由调用方算，`settle` 与 cleanup 各有一套参数。

ADR-0005 的硬约束是：每一步的 `passed` 必须是实际求值结果，不能是手写的 True。
门面要减少样板，但不能把这个约束削弱。

## 目标写法

```python
@case(id="grid-frequency-follows-setpoint", clause="gbt37408-5.2.1", tags=["场景"])
def test_grid_frequency(scenario):
    grid = scenario.device("grid_sim_01")
    inverter = scenario.device("inv_01")

    scenario.step(grid, "设定 230V / 50Hz", grid.apply_grid(230, 50))
    measured = scenario.measure(inverter)
    scenario.expect(measured.passed, measured.breaches)
    scenario.record()
```

## API

```python
class ScenarioRecorder:
    def device(self, device_id: str) -> Any: ...
    def step(self, device: Any, action: str, result: Any = None) -> ScenarioStep: ...
    def measure(self, device: Any) -> Any: ...       # read_all + 记一步 passed=reading.passed
    def expect(self, passed: bool, detail=()) -> None:
        """只给跨设备派生量用:passed 由公式算出来,不是手写常量。"""
    def settle(self, seconds: float, reason: str = "") -> ScenarioStep: ...
    def on_cleanup(self, action, *, label: str = "") -> None: ...
    def record(self) -> Path | None: ...
```

- `device(id)` 从 composition_root 取驱动,并记住 id/role/读数键;`record()` 自动填 participants 与 reading_keys。
- `measure(device)` 调 `read_all()`,把 `reading.passed` 与 `reading.breaches` 记成一步。
- `step(device, action, result)` 的 `passed` 从结果算:
  - `result` 有 `.passed` 用它;
  - `result is False` 判失败;
  - 其余(设定点回显)由调用方显式传 `passed=` 或比较函数,避免"有返回值就算过"。
- `record()` 在用例正常结束时写 scenario JSON;用例抛异常时由 fixture 的收尾写 `invalid`(沿用现状)。
- `scenario` fixture 由平台提供,内部包 `ScenarioCleanup` 与 `record_scenario`,不新增第二个真相源。

## 验证

- 把 `test_grid_frequency.py` 的三条用例改写成门面写法，场景 JSON 的 verdict / 步骤 / participants 逐项不变。
- 反例仍红：逆变器 60Hz 时 `measure` 记的 step 必须 `passed=false`。
- `settle` 仍记计划时长与实际时长。
- 清理失败仍写 `cleanup_failed`。

## 风险

- `expect(True)` 仍可能被误用;文档明确它只给跨设备派生量,单设备一律 `measure`。
- 门面不能变成第二个 `record_scenario`;实现只在 `reporting/scenario.py` 内部组合现有函数。
- 场景层用例目前只有 8 条,改写的样本足够小,可以逐条对照。

## 状态

A3 已实现:`reporting/scenario.py` 的 `ScenarioRecorder` 提供 `device / step / measure / expect /
settle / record`,并代理 `ScenarioCleanup`。`tests_scenario/conftest.py` 的 `scenario` fixture
返回门面;`test_grid_frequency.py` 第一条用例已改写成门面写法,场景 JSON 的 verdict 与
participants 不变,其余用例继续用旧写法证明兼容。空步骤在 `record()` 直接报错。

测试:`tests_unit/test_scenario_recorder.py`;`tests_scenario` 8 passed。
