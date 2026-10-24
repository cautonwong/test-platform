# 23 测试层结构:清单重复 391 行、`tests/` 语义膨胀、`tests_integration/` 形同虚设

Status: resolved
Type: task
Blocked by: None — can start immediately

**What to build:** 用例层现在能跑、参数化也到位,但结构有三处该收:手抄清单重复 391 行、`tests/` 里混着平台行为测试、`tests_integration/` 只剩 2 条而它的职责被别人顶了。另外场景用例完全不受 `--select model:` 管,工程师想"只跑逆变器相关的场景"做不到。

## 1. 10 个文件手抄清单,合计 391 行

| 文件 | MANIFEST 行数 |
|---|---|
| `tests/test_terminal_topology.py` | 87 |
| `tests/test_attribution_device_scope.py` | 53 |
| `tests/test_support_equipment.py` | 48 |
| `tests/test_meter.py` | 41 |
| `tests/test_device_models.py` | 39 |
| `tests/test_criteria_profiles.py` | 37 |
| `tests/test_attribution_executed.py` / `test_topology_grouping.py` | 24 / 24 |
| `tests_unit/test_case_identity.py` | 23 |
| `tests/test_attribution_records.py` | 15 |

另有 **8 处** 各自实现 `_manifest()` / `_write_manifest()`(`test_evidence.py:142`、`test_link_lost.py:82`、`test_terminal_topology.py:123`、`test_topology_grouping.py:67`、`test_mock_meter_end_to_end.py:376`、`parallel_*_check.py` 各一份)。

这些清单里有一部分是**故意写坏**的(测清单校验),那部分不能删;但"造一份能装配的合法清单 + 只改一处"的部分应该共用一个工厂:

```python
# tests/conftest.py
@pytest.fixture()
def manifest_factory(tmp_path):
    def build(*, inverters=1, meters=0, terminals=0, support=None, mutate=None) -> str:
        """写一份能装配的清单到 tmp_path 并返回路径;mutate(dict) 改一处再落盘。"""
        ...
    return build
```

预计删掉 250–300 行;新增用例造清单从"抄 40 行"变成一行。

**注意**:不要为了去重把测试改成"读 `configs/devices.yaml`" —— 那些用例的价值正是**不依赖出厂清单**(出厂清单一改,测试就跟着变)。

## 2. `tests/` 语义膨胀:平台行为测试混在设备契约层

`tests/` 22 个文件里,这些**不测设备**,测的是平台自身行为:

`test_attribution_device_scope.py` / `test_attribution_executed.py` / `test_attribution_records.py` / `test_evidence.py` / `test_topology_grouping.py` / `test_link_lost.py` / `test_mock_meter_end_to_end.py`

后果:它们跟着设备清单一起被 `--select model:` 筛、跟着 `mode: real` 一起跑、被算进"一致性测试"的统计口径。

期望:新增 `tests_platform/` 放这类用例,`testpaths` 加上它。设备契约层只留 `test_inverter` / `test_battery` / `test_meter` / `test_criteria_profiles` / `test_device_models` / `test_support_equipment` / `test_terminal_topology`。

`tests_unit/` 保持不动(129 条纯逻辑,与 `tests/` 143 条近 1:1,比例健康)。

## 3. `tests_integration/` 只剩 2 条,职责被别人顶了

现在只有:

- `test_system_bootstrap_brings_up_every_configured_device`
- `test_inverter_and_battery_scenario_both_report_valid_readings`

而"多设备同台架"的能力实际上在 `tests/test_mock_meter_end_to_end.py`、`tests/test_terminal_topology.py`、`tests_scenario/` 里被验了。二选一,不要挂着空壳:

- (a) 明确定位:这层只放"平台自身在多设备台架上的行为"(装配顺序、资源独占、初始化互相干扰、收尾),把 `test_link_lost.py` 这类迁进来
- (b) 合并进 `tests_platform/`,删掉这个目录

**推荐 (a)** —— 真机联调时最先撞的就是这一层,有个明确的落点比临时塞进 `tests/` 好。

## 4. 场景用例不受 `--select model:` 管,也不受适用矩阵管

```
$ python -m pytest -q tests_scenario --select model:inverter_pv_series
no tests ran
```

场景用例不按设备参数化,`_device_profile()` 返回空 → 平台行为用例与场景用例都不参与 `model:` / `capability:` 筛选。设计上可以理解,但对使用者是个坑:**想把"这台逆变器相关的场景"单独跑一遍,现在没有入口**,只能 `--select tag:场景` 跑全部。

期望:场景用例的参与方在收集期已经能从 `scenario` 层的显式 id 拿到(用例里 `composition_root.get("inv_01")` 是字面量),二选一:

- (a) 场景用例声明 `@pytest.mark.participates("inv_01", "grid_sim_01")`,`--select model:` 按参与方的型号匹配
- (b) 在 `docs/WRITING_TESTS.md` 写明"场景只按 tag / case 筛",并提供 `--select case:S_` 的用法示例

(a) 更有用,但需要收集期能读到 marker;(b) 是零成本。**先从 (b),等真的需要按型号挑场景再做 (a)。**

## 验收

- [x] `manifest_factory` 落地;至少 5 个文件改用它,净删 ≥200 行;故意写坏的清单用例仍覆盖各自的校验分支
- [x] `tests_platform/` 建立并进 `testpaths`,7 个平台行为文件迁入;`tests/` 只剩设备契约
- [x] `tests_integration/` 的定位在目录 README 里写清(推荐 a),并至少迁入 1 条现在散在 `tests/` 的多设备用例
- [x] 场景筛选的限制写进 `docs/WRITING_TESTS.md`(b)
- [x] 全量套件仍全绿;skip 数不变;`python -m pytest --collect-only -q` 的用例总数在迁移前后一致

## Comments

- 2026-10-20 出票依据:实测 `tests/` 143 条 / `tests_unit/` 129 条 / `tests_integration/` 2 条 / `tests_scenario/` 2 条;391 行是脚本遍历 AST 累加 `MANIFEST` / `BASE_MANIFEST` 常量体得到的;`--select model:` 在 `tests_scenario` 上返回 `no tests ran` 是实跑。
- 不属于本票:`tests_scenario/` 只有 2 条(正例 + 反例),那是 ADR 0005 定的"等真实需求",不是缺陷。

- 2026-10-22 已完成 `c7dc2d0` / 合入 `99b42de`,**604 passed / 7 skipped**,规模清单 1708 passed / 109 skipped。
- 六样验收我全跑了:`tests_platform/` 63 passed;`tests/` 184 passed / 7 skipped;全量 604/7;跨层 grep 两方向全空(含 `tests_support/` **不反向依赖**任何层);工厂隔离用例 8 passed;`-p randomly --randomly-seed=12345` 604 passed。收集数 603 → 611,**+8 正好是新增的工厂用例**,没有丢用例。
- 手抄清单:测试文件里 753 → 256 行(**-497**);**含新增的工厂与 `tests_support/` 后,该分支整仓净 −151**(`5c9368f..b66e0f1`,`+733 −884`)。两个数都对,**口径不同** —— 拿"净删 −230"跟这轮 diff 对不上会以为丢了改动。7 个文件迁移在 diff 里是 `rename`(`git diff --summary master | grep -c rename` = 11)。
- 共享常量泄漏已修(`models`/`criteria_profiles` deepcopy),守卫按**段**遍历而不是只锁型号,并加了一条"工厂多出一个段就红"的守卫 —— 否则将来加第四段时会静默失效。
- 故意写坏的清单用例全部保留(逐条见交付报告),改成 `mutate` 一处而不是另抄一份。
