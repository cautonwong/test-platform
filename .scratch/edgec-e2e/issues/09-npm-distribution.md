# 09 打包分发:平台与公共数据各成 npm 包

Status: resolved
Type: task
Blocked by: 05 门禁判定:报告 → 退出码

**What to build:** 平台以 npm 包分发,monorepo 锁版本;公共数据(周边设备型号库 / 判据集 / 条款清单)另成一个 npm 包。打正式 tag 出正式版,协议扩展分支出预发布版,避免本地分叉。

## 验收

- [x] 平台包可在干净环境安装,并成功跑完一个任务
- [x] 公共数据包可安装;产品清单按"包名 + 型号 id"引用型号,未安装该包时装配期报错并说明
- [x] 公共包**不含用例逻辑**(只装数据);产品清单不得内联点表
- [x] 打 tag 出正式版;分支出预发布版,安装说明写进文档
- [x] 安装失败时的报错能区分"包没装""版本不匹配""型号不存在"
- [x] 基线不回归

## Comments

### 2026-10-23 · worker-1(01b/12 实现侧):分发形态必须满足的四条约束

这四条是执行器/插件当前代码里**真实依赖**的位置与来源,不是建议。安装形态下任一条不成立,
"平台包可安装并跑完一个任务"就过不了。

**1. 插件模块必须随包可 import(`testplatform_plugin`)**
- 源码布局里它在**仓库根**,不在 `src/testplatform/` 内:
  - `conftest.py`:`pytest_plugins = ["testplatform_plugin"]`
  - `src/testplatform/execution/task.py::_run_pytest`:`-p testplatform_plugin`,并显式 `-c <root>/pyproject.toml`
- 现在子进程的 `PYTHONPATH = <root>/src:<root>`(第二项就是给根上的插件模块兜底),
  代码里标了 `shortcut:` 指向本票。
- 要求:安装后 `python -m pytest -p testplatform_plugin --version` 能加载。两种做法都行 ——
  把它装进包并保留模块名,或挪进包 + 声明 pytest entry point,**两条调用点要一起改**。
- 反例(必须避免):只有源码树里能 import 成功,安装后 `ImportError`。

**2. `env.platform_version` 与 `env.commit` 同一策略:不许 None**
- `env.commit` 已定:显式 `TESTPLATFORM_COMMIT` > `git rev-parse` > 字面量 `unknown`
  (`src/testplatform/config/paths.py::commit`)。
- `env.platform_version` 现在仍是 `importlib.metadata.version("testplatform")`,**未安装时 None**。
  要求:构建/发布时写入或读环境变量,取不到写 `unknown`;04 的报告读 `env`,字段不许消失、不许留空串。
- 安装目录通常**没有 `.git`** —— 09 的分发路径不能依赖"跑得到 git"。

**3. 执行器要用的四个路径必须存在(或由显式根指向)**
`PlatformLayout`(`src/testplatform/config/paths.py`)从平台根派生:
`pyproject.toml` / `src` / `tools/mock_modbus_meter.py` / `configs/mock_registers.yaml`。
- 安装形态:这四个随包数据走(模拟从站脚本与寄存器表是**执行器的一部分**,不是仓库示例);
- 或:任务用 `--root` / `TESTPLATFORM_ROOT` 指向一个含它们的目录(01b 已实现,优先级:显式 > 环境变量 > 探测);
- 缺任何一项 → 加载任务时一次报清(列出缺项 + 怎么指定),不是 `FileNotFoundError` / 子 pytest `exit 4`。

**4. 条款清单的默认位置只在一处**
`configs/clauses.yaml` 现在按平台根解析(`paths.clauses_path()`),`--clause-config` 可覆盖。
PRD 把它归"公共数据包" —— 09 定了安装位置后,改**一处**(`config/paths.py`)即可;
不要把默认路径再复制到插件或报告里。

### 建议补进验收的三条(能机械验)
- [x] 干净环境安装后 `python -m pytest -p testplatform_plugin --version` 成功(插件随包)
- [x] 安装环境里跑完一个任务后,`task.json.env.commit` 与 `.platform_version` 都**存在**且不是 `null`
      (`unknown` 可以,但必须出现 —— 这是 01b 给 `commit` 定的口径,`platform_version` 跟它一致)
- [x] 安装环境里删掉 `.git` 目录(或在无 `.git` 的容器里)跑同一个任务,结果不变

## Comments

- 2026-10-24 **拆成六票落完**:09(打包与安装形态)/09b(四个 scaffold)/09c(构建期生成 + CI install job + 3.10 job)/09d(平台根三支统一)/09e(示例产品随包)/09f(npm 壳)。全部合入 master。
- **leader 自验(干净环境,不看实现方口径)**:
  - `git archive` → `build --wheel` → 新 venv 装 → `_data/tests_support/fake_product.py` 在
  - 项目目录(用例树 + configs,删掉 `tests_support/fake_product.py`)→ `acceptance_sample` **exit 0**
  - 删包内 `_data/tests_support/fake_product.py` → **exit 4**,消息含「包内缺 …(平台包装坏了,重装平台包)」+ 包内路径 + 「或在项目目录放一份」
  - `layout = {pytest_ini: package, slave_script: package, registers: project, clauses: project, sample_product: package, cases: project}`;`product_sha256` 安装形态有真值,内联命令型记 `"unknown"`(**永不 null**)
  - `scaffold driver --out` → 生成项目 → `run` → **exit 0**
- **npm 形态**:`npx @cautonwong/testplatform init|run|gate|scaffold`,包内带 wheel(`prepack`),**无 `postinstall`**,venv 落项目 `.testplatform/`(不进 `node_modules`)。**未发布 npm**。
- **未做**:`npm publish`(发布前定 scope);3.10 只在 CI job 上验,本地未验(不许写"兼容")。