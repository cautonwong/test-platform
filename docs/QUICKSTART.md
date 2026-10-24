# 快速开始:从零到"跑出第一份报告"

目标:一个**没接触过这个平台**的人,照这一页走完,能得到一份带证据的报告。
全程 15–30 分钟。每一步都有"怎么知道这步对了"。

前提:Python ≥ 3.10。检查:

```bash
python -c "import sys; print(sys.version)"
```

> 分发形态是 `npx @cautonwong/testplatform init|run|gate|scaffold` —— **`npx` 只是外壳,写的是 Python**。
> 这一层还在做,下面凡是标了 `TODO(09b)` 的命令暂时用括号里的源码形态代替。

---

## 0. 三个概念,先记住(其余都可以边做边查)

| 概念 | 一句话 | 存在哪 |
|---|---|---|
| **设备 / 型号** | 被测物与它的能力(点位、判据量) | `configs/devices.yaml` |
| **用例** | 一段会失败、带编号和条款的 Python | `tests*/` 里的 `@case(...)` |
| **任务** | "跑哪些产品、连哪些设备、选哪些用例"的声明 | `tasks/<id>.yaml` |

**一次任务 = 1 个产品进程 + N 台模拟/真实支撑设备。** 产品由平台启动,崩了不会自动重启(`restart_product()` 是显式动作)。

---

## 1. 初始化项目

TODO(09b)

```bash
npx @cautonwong/testplatform init my-tests
cd my-tests
```

今天(源码 / 未发 npm 时),两条等价路径:

```bash
# A. 从 wheel 装:更接近用户形态
python -m build --wheel
pip install dist/*.whl

# B. 源码可编辑装:改平台本身时用
pip install -e .
```

**这步对了的标志**:`testplatform --help` 与 `testplatform-run-task --help` 都能跑。

---

## 2. 用 scaffold 生成一个能跑的项目(先别写自己的东西)

```bash
testplatform scaffold case --out my-tests
cd my-tests
testplatform-run-task tasks/case_sample.yaml --root . --evidence-dir reports
echo "退出码=$?"          # 期望 0
testplatform-run-task --help >/dev/null && echo ok
python -m testplatform.execution.gate reports
echo "gate 退出码=$?"     # 期望 6 —— 见下面的说明
```

四个骨架任选:`scaffold driver|protocol|case|simulator`,**每个都是"生成后立刻能跑"**。

**这步对了的标志**:`reports/case_sample/summary.json` 里

```json
{"kind": "smoke", "verdict": "clean",
 "columns": {"devices": {"passed": 1, "total": 1},
             "scenarios": {"passed": 0, "total": 0}}}
```

> ⚠️ **仓库里的 `configs/tasks/acceptance_sample.yaml` 不是给你在项目里跑的** —— 它选的用例在仓库的 `tests/` 里,还依赖 `tests_support/`。
> 用户项目请用 scaffold 生成的示例任务(自带用例与判据集)。

### 第一次 `gate` 会是 6,这是对的

```
没有一条验收任务在跑:这一批只有冒烟任务(冒烟不验设备)
另有 1 个冒烟任务(不参与验收): case_sample
```

scaffold 生成的是 `kind: smoke` —— 它验的是"平台在你机器上能跑通",**不验任何设备**。
门禁第 6 档规定「一批里没有验收任务 → 红」,因为**没验到的东西不能算通过**。

要让它变绿,你得写**第一条验收任务**:一个真产品(或你的模拟产品)+ 至少一台设备的读数判据,然后 `kind: acceptance`。
脚手架的骨架是起点,不是验收结论。

---

## 3. 写一个模拟器(最快能跑起来的一步)

TODO(09b)

```bash
npx @cautonwong/testplatform scaffold simulator
```

生成的东西要做三件事:**点表读写** + **故障注入钩子** + **不替换写路径**(注入不能把读到的值改掉,只能影响链路)。

源码形态:照 `docs/how-to/add-a-device.md` 抄,模拟器落 `src/` 下,注册到 `configs/devices.yaml`。

**这步对了的标志**:`python tools/catalog.py --verify` 通过 —— 目录会告诉你型号、判据量、能力是否都接上了。

---

## 4. 写 driver + protocol + 用例

TODO(09b)

```bash
npx @cautonwong/testplatform scaffold driver
npx @cautonwong/testplatform scaffold protocol
npx @cautonwong/testplatform scaffold case
```

顺序别跳:**协议 → 驱动 → 用例**。用例只碰驱动,驱动只碰协议,协议只碰字节。

每一步的细则在:

- `docs/how-to/add-a-protocol-and-driver.md`
- `docs/WRITING_TESTS.md`(用例写法、编号、条款、豁免 `free:` 的规则)

**这步对了的标志**:`python -m pytest <你写的用例文件>` 能单独跑绿 —— 别等到任务里才发现写错。

---

## 5. 写任务文件

```yaml
# tasks/my-first.yaml
id: my-first
kind: acceptance          # 缺省就是 acceptance;smoke 表示"只验通路,不验设备"
product:
  launch: "${PYTHON} -m my_product --port ${PORT}"
devices: [meter_01]
select: model:meter_m     # 选择器:model: / capability: / tag: / case:(多个条件是"且")
```

字段全解见 `docs/how-to/add-a-scenario.md` 与 `configs/tasks/` 下的示例。

**这步对了的标志**:任务加载期没有报错。平台会在加载期一次把**缺的路径、缺的字段**全列出来,不会等到跑到一半。

---

## 6. 跑它,然后读报告

```bash
python tools/run_task.py tasks/my-first.yaml --root . --evidence-dir reports/my-first
echo "退出码=$?"
```

**退出码就是结论**,不是 pytest 的结果:

| 退出码 | 含义 |
|---|---|
| 0 | 合格 |
| 1 | 不合格(含产品自己崩) |
| 2 | 测试无效(判定被环境吞掉) |
| 3 | 未完成(申报外未测 / 设备级没验到 / 超时取消) |
| 4 | 平台错误(产品起不来、任务配置错) |

报告两处必看:

1. **首行的理由** —— 它说的是"为什么是这个结论",不是套话
2. **`evidence_gaps`** —— 哪些设备的哪些点没验到;写了"正常,不是问题"的就是真正常

`docs/how-to/read-evidence.md` 讲怎么读原始证据(读数、帧、注入、死因)。

---

## 7. 接进 CI

```bash
python tools/gate.py reports
echo "gate 退出码=$?"     # 0 合格 / 1 不合格 / 2 无效 / 3 未完成 / 4 平台错误 / 5 没跑过 / 6 没有验收任务
```

`gate` 只读 `reports/*/summary.json`,不改报告。工作流样例见 `.github/workflows/tasks.yml`。

---

## 8. 接真设备

见 `docs/how-to/run-on-real-hardware.md`。要点:

- 设备清单里 `mode` 从 `simulator` 改成 `real`,`--force-simulator` 可整体覆盖
- **点表真值、额定值、阈值来自设备手册**,平台不能替你判对错,只能保证"按你给的判据一致地判"
- 真机上的读数和模拟器一致,是"驱动写对了"的必要条件,不是充分条件

---

## 常见卡点(照这个顺序查)

| 现象 | 先查这里 |
|---|---|
| 任务加载期报"找不到平台根" | `--root` 指到**项目目录**,不是资源目录 |
| `未完成(no_device_coverage)` | 你选中的用例没有按设备展开;任务要验设备,别把 `kind` 写成 `smoke` |
| `平台错误` 里提到端口 | 端口来自平台段;先看 `task.json` 里的 `port_attempts` 与 `reason` |
| `gate` 退出码 6 | 这一批里一条验收任务都没有 —— 冒烟任务不参与验收 |
| 目录校验失败 | 改了用例编号/tag 后忘了 `python tools/catalog.py` 重新生成 |

---

## 一句话总结

写 **模拟器 → 协议 → 驱动 → 用例 → 任务**,跑 `run_task`,读退出码和首行理由,用 `gate` 判 CI。
**判据对不对平台不给答案,只给证据。**
