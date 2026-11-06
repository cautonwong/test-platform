# 先写验收用例,再改产品

适用于"产品要处理一个 issue"的场景。以 EdgeC 为例:issue 的验收标准先落成 test-platform 的验收用例,
跑出一条**产品不合格**的红,再改 EdgeC,直到同一条用例通过。测试不是事后补的,它是这轮的验收合同。

## 前提

issue 必须能在产品边界观察。可观察的例子:产品数据库里的值、产品发出的总线请求、HTTP / MQTT 应答、
进程退出码、生成的报文。内部重构、函数改名这类不可观察的改动不写验收用例,写产品自己的单测。

## 步骤

1. **把 issue 写成一句可判定的结论。**
   例如"DCU 落库的电压必须等于台架读出的值",不是"修 dcu_twin_source"。

2. **在产品的 acceptance 项目里加一条用例。**
   目录形如 `acceptance/<product>/tests/test_<issue>.py`。用 `@case` 标身份和标签;
   产品项目通常没有 `configs/clauses.yaml`,条款写 `free: <issue 原文>` 即可。
   从 `testplatform` 生成骨架:

   ```bash
   testplatform new-case issue_123 --out acceptance/dcu --device bench_meter_01 --clause "free: issue #123"
   ```

   生成后编辑它。断言必须走产品边界,不要只断言"帧收到了"。

3. **让任务选中它。**
   在 `acceptance/<product>/tasks/*.yaml` 的 `select` 里加标签或编号,任务必须是
   `kind: acceptance`。`kind: smoke` 只验通路,不验设备,不能当验收结论。

4. **先跑红。**
   本地调试用 `TESTPLATFORM_SKIP_PIN=1`:

   ```bash
   TESTPLATFORM=/path/to/test-platform PYTHON=/path/to/python \
     TESTPLATFORM_SKIP_PIN=1 ./acceptance/run.sh dcu
   ```

   看退出码,不是看 pytest 的红绿:

   | 退出码 | 含义 | 这时该做什么 |
   |---|---|---|
   | 1 | 产品不合格 | 这是你要的红,继续改产品 |
   | 2 | 测试无效 | 台架 / 环境 / 测试自身的问题,先修测试 |
   | 3 | 未完成 | 用例没跑到 / 没验到设备,先查 collect 与 select |
   | 4 | 平台错误 | 产品起不来或任务配置错,不是产品缺陷 |

   只有退出码 1 才是"测试写对了,产品还没改对"。

5. **改 EdgeC,重建,再跑同一条用例。**
   C 代码改完先重建对应二进制,例如 `cmake --build build --target dcu -j2`;
   然后重跑 `./acceptance/run.sh dcu`。循环到退出码 0。

6. **跑全量验收与 WebUI,再过门禁。**
   `./acceptance/run.sh` 会汇总所有产品;`./acceptance/run_webui.sh` 跑 WebUI 那一套。
   门禁只认验收任务,冒烟任务不参与验收。

7. **留下用例。**
   这条用例成为回归合同。平台升级时改 `acceptance/testplatform.sha`,并重跑一轮验收。

## 一条好用例长什么样

看 `acceptance/dcu/tests/test_dcu_acquisition.py`。它按顺序验三件事:

1. 台架自己读出的值过判据。台架不对,后面的失败与 DCU 无关。
2. DCU 真的向台架发过 DLT645 读请求。少了这一条,产品凭空合成的 220.0 也能落在 198–242 区间里。
3. DCU 落库的电压等于台架读出的值(±0.5V)。

第二条就是"防假绿"的断言。只验第 3 条的值域,`dcu_twin_source` 合成的读数会全绿,
而那正是这条用例当初要抓的 bug。

## 边界

- 硬件故障、时序抖动这类 issue,先用模拟设备或可注入故障的模拟器复现;平台不能替物理台架表态。
- 异步落库给足超时并轮询,不要在用例里写一个拍脑袋的 `sleep`。
- 测试自己先失败在"无效 / 未完成"时,不要改产品;先让红的理由正确。
- EdgeC CI 用 `acceptance/testplatform.sha` 锁平台版本。新用例依赖了平台新能力时,
  升级 pin 是独立的一次提交,不要混在 issue 修复里。
