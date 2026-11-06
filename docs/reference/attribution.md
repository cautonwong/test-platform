# 一轮的失败归因

跑完一轮之后,一句话回答"这一轮到底哪些设备不合格、哪些是台架问题、断在哪一步":

```bash
python tools/attribute_round.py                              # 取 evidence/ 下最新的一轮
python tools/attribute_round.py evidence/20240101T093000      # 指定一轮
```

输出两份,同一份归因:终端分节输出(人读),以及写回同一个证据目录的 `attribution.json`
(字段与 `round-status.json` / `scenario-*.json` 同一风格)。四类分开列,一条不并:

| 分节 | 装什么 | 谁来消化 |
|---|---|---|
| 设备不合格 | 读数越过 `severity: reject` 的判据,或场景里有被测设备的一步不成立:设备 id、判据、实测值、超出多少、依据哪条用例(编号 + 条款) | 设备/型号 |
| 未落证据的失败 | pytest 报了失败、证据里却没有结论(用例在写证据之前就崩了):nodeid、用例编号、一行原因。既不算通过,也不许当设备不合格;这一栏非空时结论不许是 `clean` | 排障的人 |
| 测试无效 | 周边设备故障(`link_lost`:收帧阶段断链 / 台架复位失败 / 条款的台架前置没发下去,按 `operation` 分开写)、场景判 `invalid`、断在哪一步、涉及哪台周边设备 | 台架 |
| 平台错误 | 聚合根不变量被违反(`platform-errors.jsonl`):平台自身有 bug,与被测设备无关 | 平台 |
| 没有有效结论 | 被 `--select` / 适用矩阵筛掉、被能力或 `real_hardware` 跳过、收集到了但证据里没有读数、在范围内却没有读数 —— 一律不算通过 | 排计划的人 |

其中的「设备不合格」里的判据违约与证据层同构:区间违约写 `limit` + `exceeded_by`;
等值违约(`kind: "equals"`)没有边界,只写 `expected` + `value`,`limit` / `exceeded_by` 不出现 ——
写成 `null` / `0.0` 会被读成「没越界」,与「这条不合格」自相矛盾。

其中的「证据文件是空的」分三档,显示上分开:不在本轮范围的设备(没被任何用例点到)
只给一行计数(`另有 N 台设备不在本轮范围内`,名单在 `attribution.json`);用例点了它却根本没跑到
(`run-scope.json` 的 `executed` 里没有它的用例:被跳过 / `-x` 停下 / 会话被打断)同样只给一行计数,
一轮一条用例都没执行时写 `这一轮没有执行任何用例,N 台设备的读数无从判断`—— 规模下 264 行说的
其实是同一件事;设备在范围内、用例也执行了却没读数,才逐台列出(真该测没测时就靠这一档,
大规模下不能让它浸在范围外的噪声里)。
最后一档说得出口径:用例进了 `executed`、失败名单里也有它 → `它失败在写证据之前`(指回「未落证据的失败」);
否则就是 `选中的用例本来就不读数`(正常,不是问题)。两者下一步不同,不写同一句,
也不写"漏测"(`attribution.json` 里的 kind 是 `no_readings`,真跑过的 nodeid 在 `executed_nodeids`,
没跑到的设备名单在 `not_run_devices`)。执行条数未知(旧证据没有 `executed`)时退回中性说法,
不猜是哪一种。
设备范围从哪来:按设备参数化的用例在 `run-scope.json` 里带 `devices`,场景用例取
`scenario-*.json` 的 `participants`;两边都交不出设备时报告写"设备范围: 未知",仍逐台列出。

证据里丢掉的读数也要留下痕迹:取增量时若两轮之间记的读数超过 `history` 窗口(1000 条),
最旧的那几条已被裁掉、取不回来 —— 这时写一条 `evidence_gap`(`kind` / `missing` / `window` / `note`),
缺的条数明写:`{"kind": "evidence_gap", "missing": 5, "window": 1000, ...}`。
没有它,证据会看起来完整而实际少条,“失败必留证据”就无声地破了。没超出窗口的轮次不写这条
(否则每轮多一条噪声,真缺口反正浸在里面)。

证据里的时间戳一律带偏移:每条记录的 `at` 是 UTC(`2026-10-22T07:23:54.065+00:00`),
领域读数与事件的 `timestamp` / `occurred_at` 也带时区 —— 两台台架各跑一轮、把证据并到一起时
才排得出先后。排序用 `reporting.evidence.parse_at()`:旧证据(这条规则之前的目录)的 `at`
没有偏移,按本地时间理解 —— **已知限制**:这类旧记录只对写出它的那台机器所在时区准确。
改格式不改读法,否则跨台架的问题只是换成了跨版本的问题。会话目录名仍是不带偏移的时间戳,
跨时区机器共用同一个 `evidence/` 目录时“最新一轮”按目录名排会不准。

证据文件本身坏掉另算 `evidence_corrupt`(责任在写证据这一侧,既不算设备不合格也不算平台 bug)。
写证据时丢的读数也归这一栏(`evidence_gap`),而且报出缺了几条 —— 缺 5 条与缺 5000 条
不是同一件事。
每一条都带 `ref`,能直接指回具体的证据文件行(`inv_01.jsonl:14`)或 JSON 字段
(`scenario-x.json#steps[1]`、`run-scope.json#skipped[0]`),让人自己核对。

「被跳过」按原因聚合:同一原因只占一行 + `(×N)`,只展开前 3 个不同原因,
其余给一行计数 —— 规模清单下 100 条只差设备 id 的跳过逐条列会把真正的失败挤出屏幕。
完整名单一条不少地留在 `attribution.json` 的 `no_conclusion` 里。

"没跑"不是"通过":这一轮的范围记在 `evidence/<会话>/run-scope.json` 里
(收集了哪些用例、真正执行过哪些(`executed`)、每条用例落在哪些设备上、哪些被 `--select` /
适用矩阵筛掉、哪些被跳过以及为什么、哪些失败了(`failed` = call 阶段失败,`errored` = setup 阶段崩掉))。
`collected` / `deselected` / `skipped` / `executed` / `failed` / `errored`
是各不相同的事实,不互相推算 —— 少了这份文件时归因会明说"范围缺失",
不会把这一轮当成"只有这些用例"。
失败也要落进来:用例在写证据之前就崩了时证据里一条读数都没有,不记范围就等于从归因里消失。
它只记范围、不记判定 ——
判定仍然只有 pytest 报告和证据两处。

全绿一轮也必须说得清规模:输出会写"没有失败项"并给出"执行了 N 条(收集 C 条;其中跳过 S 条;还没跑到 X 条)",
不是把 0 条失败说成 0 条用例。这里的 N 只取自 `run-scope.json` 的 `executed`(真正执行过的用例):
`pytest -x` 在第一条失败后停下、或会话被 Ctrl-C 打断时,被收集却没执行的用例不算进去 ——
拿 `collected − skipped` 推算会正好把它们算成"跑过"。旧证据目录没有 `executed` 时,输出写
"执行条数未知",不拿任何减法冒充。`attribution.json` 那侧同样只有 `ran.executed` 这一个字段
说"跑了多少条"(未知时是 `null`),`ran.cases` 只表示证据里留下读数的条数。
退出码:`clean` 为 0,其余(`failed` / `invalid` / `platform-error` / `evidence-corrupt` /
`failed-without-evidence` / `incomplete`)为 1。
`failed-without-evidence` 就是上面「未落证据的失败」那一栏:它保证 pytest 红的时候归因不会说 `clean`,
`tests/test_attribution_meta_guard.py` 真跑一轮必失败的 pytest 来守这一条。
