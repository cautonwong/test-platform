# 证据

默认落盘到 `evidence/<会话时间戳>/<设备id>.jsonl`,每行一条 `frame` / `reading` / `event` /
`write` / `bench_setup` / `link_lost`(周边设备不合作:收帧阶段断链、收尾没把台架恢复回初始设定、
或条款的台架前置没发下去,按 `operation` 分开写,见上)/ `evidence_gap`(见下)。`write` 记录带读数键、写下去的物理量、
回读值与 `verified`(回读是否与写入一致):
`{"kind": "write", "key": "有功计量模式", "value": 1601, "verified": true, "read_back": 1601.0}`。
`bench_setup` 是条款的台架前置下发了什么(写在被测设备那一份里,排在随后的 `reading` 之前)。
写失败(超时 / 被拒)不写这条 —— 它已经落在那条 `frame` 记录的 `error` 上。会话目录下另有 `round-status.json`:
这一轮有没有断链、涉及哪些设备、结论要不要复核,机器可读。
场景层另写一份 `scenario-<用例名>.json`:参与方与 role、引用的读数键、每步结果、场景结论
(`passed` / `failed` / `invalid`)、收尾结果(`cleanup` / `cleanup_failed`)与关联的 per-device 文件名
(见 ADR-0005)。`cleanup` 是一串 `{action, passed}`,失败的条目另带 `error` 与 `message`;
收尾失败会让结论变 `failed` 并在 `note` 里写明"流程问题,不是判据不合格"。
`settle` 那一步的 `detail` 带 `reason` / `target_s` / `actual_s`(`actual_s` 是量出来的,不是抄配置)。
两次运行不互相覆盖;失败的用例也留证据(报文在收发那一刻就写,用例中途崩了收尾还有一次兜底)。
每台设备一个长期打开的句柄,每条记录写完就 `flush`:记录格式与落盘时机不变(报文仍在收发那一刻进文件),
而 open/close 次数从「每条记录一次」降到「每台设备一次」—— 一轮 5940 条记录时这一项就是 8 秒。
句柄在组合根 `teardown()` 与 pytest `sessionfinish` 关闭;每条记录已提前 flush,所以进程被强杀也不丢已写的记录。
收不回应答也写一条:`receive` 抛异常时那条帧记录带 `error`(`timeout` / `closed` / `disconnect` / 异常类名)
与已等待时长;应答回来了、只是被帧校验拒掉的,则是没有 `error` 的那条 —— 两者在证据里一眼分得开。
`reading` 记录带 `mode`(`real` / `simulator`),一份证据自己就能说清结论是模拟器还是真机给的
(取组合根算好的实际模式,`--force-simulator` 下就是 `simulator`)。
`reading` 里的 `evaluation.breaches` 是结论的机器可读版:区间违约写 `limit` + `exceeded_by`;
等值违约(`equals`)没有边界,所以只写 `kind: "equals"` + `expected` + `value`,
`limit` / `exceeded_by` 不出现 —— 写成 `null` / `0.0` 会被读成“没越界”,与 `passed: false` 自相矛盾。
证据层目前只支持字节流规约:给非字节流设备(CAN / HTTP / SSH / MQTT)开记录会在装配期直接报错,
而不是等到第一次连接时爆成 AttributeError。
`--no-evidence` 关掉后,装配与行为与没有证据层时完全一致(断链归属仍按 role 走:
周边设备断链照样 skip 测试无效,只是没有链路记录、汇总小节与 round-status.json)。

证据只由 pytest 层写:用例身份靠 `@case` 串起来。直接用 Python 脚本调 driver/组合根跑一遍,
进程里没有装配好证据会话,`evidence/` 下什么也不会多出来 —— 想留证据就跑 pytest 用例。

并行(`-n 4`)时目录名带上 worker 名:`evidence/<会话时间戳>-gw0/`、`-gw1/` …
每个 worker 各开一个会话 —— 否则同一秒启动的几个 worker 会算到同一个目录名上,证据互相覆盖。
同一台设备的所有用例落在同一个 worker,所以一台设备的证据只在一个目录里非空;
其它 worker 目录下那份同名文件是空的(装配时占位,说明它也被装配过)。
