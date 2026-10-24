# 41 `setting_cases` 缺 `bench:` 前置字段:105 条通信条款因台架设定无法进一致性层

Status: resolved
Type: task
Blocked by: 无 —— 但不急着做,等批量迁移时一起(见 Comments)

**要做什么:** 实施 `docs/adr/0006-bench-setup-versus-observed-variable.md` 定下的切法:

1. `setting_cases` 的条款支持一个 `bench:` 字段(mapping:台架量名 → 值),表示"每条条款都重复的那段前置"
   (接线方式、Ua=Ub=Uc=Un、Ia=0、Fre=Hz 之类)。它**不进 steps**,也不参与判定 —— 它只是让表能测的前提。
2. `bench:` 里的量名必须过**同一份** `configs/import/params.yaml`(不另开一套名字);拼错在装配前报错。
3. 装配期校验:量名不在对照表、值类型不对、台架没有对应能力 → 报错。
4. 执行顺序:连接 → `bench:` 前置(通过周边设备的驱动下发)→ steps(写/人工)→ 回读判定。
5. 导入器(`tools/import_cases.py`)按步骤类型分流,**并把分流结果写进覆盖率报表**:
   - 只有写/人工步骤 + 台架前置 → 进 `setting_cases`(带 `bench:`)
   - 含"被观测的台架动作"(调台架到 X → 读表 → 判 Y)→ 归场景层,不计入 `setting_cases` 的可生成数
   - 这个判定必须显式:每条条款在报表里标明它被分到哪一边,不许静默二选一

## 为什么现在不做

按 ADR 0006,带因果链的条款要归场景层,而场景层的 `hold` 还没做;而且当前 `13通信` 的可生成数是 **0**,
先补"点表里缺的可写点"(通信模式 / 波特率 / 时钟等)比先加 `bench:` 更能提高可生成数。
**这两件事都做完,105 条的批量迁移才有意义。**

## 验收

- [x] `bench:` 字段进清单并通过同一份对照表校验(拼错报错,贴)
- [x] 一条带 `bench:` 的条款:执行顺序正确(先下发台架、再写、再回读),证据里能看到台架下发
- [x] 导入器报表按"进 consistency / 归场景层"分流,每条条款标出来
- [x] 现有 `setting_cases` 条款(无 `bench:`)行为一字不变
- [x] 全量套件全绿

## Comments

- 2026-10-22 出票依据:`docs/adr/0006`(leader-0 拍的切法)+ `37` 的覆盖率报表(`13通信` 105 条全含"调整台体",可直接生成 0 条)。
- **不是下一个该做的。** 先补点表里缺的可写读数键,再看可生成数;`bench:` 与场景层 `hold` 都是批量迁移的前置。

- 2026-10-22 已完成(`47ea3ff`…`e3f790a` / 合入 `c1dd115`),合入后 592 passed / 7 skipped。`bench:` 前置落成,量名过同一份 `configs/import/params.yaml`(拼错装配期报错并列出可用量);执行顺序:连接 → 台架前置 → steps;证据里落 `kind: bench_setup`(`setpoints` + `applied`)先于 `reading`。
- **结论归属按 leader-0 新加的口径分开**:前置没发下去 → **测试无效**(`skip` + `测试无效:` 前缀,`round-status` 落 `needs-review`,归因 `invalid_tests` 里 `("link_lost","apply_three_phase")`);前置发下去但读数不成立 → **设备不合格**。两条路径各一条用例锁死。
- 端到端条款 `M_00_00_13_0002FF_61`(上游 13通信 第 4189 行)在模拟器上跑绿:12 行台架前置 + 一个写步骤。`接线方式` 故意不写 —— `params.yaml` 里枚举编码还是 null,不拿 0 顶。
- 导入器按 ADR 0006 分流(consistency / 场景层)并逐条标出。
