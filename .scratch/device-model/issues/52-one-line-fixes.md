# 52 两处一行修:硬编码的 venv 路径、未配置主机密钥的运行时提示

Status: resolved
Type: task
Blocked by: None — 各一行,一次提交

**What to build:** 体检里两条"存在但没人看得见"的小问题。

## A. `tools/import_cases.py:273` 写死开发机路径

```python
"E:/workspace/test_platform/.venv/Scripts/python.exe tools/import_cases.py"
```

别人机器上照抄会跑错解释器,而且**不会报错** —— 它只是打印出来的建议命令。

改为 `sys.executable`(或 `python`)。

## B. 未配置 `known_hosts` 时,网关连接不校验主机密钥且没有任何运行时迹象

48 的三态矩阵是对的:未配置 = 保持现状(接受任意密钥)。但默认路径最容易被当成"已经安全":半年后有人看到"我们有主机密钥校验"就放心了,而清单里根本没配。

期望:装配一台 `access: gateway_ssh` 且**未配置** `known_hosts`/指纹的设备时,打**一行**提示(同一条设备只打一次,不要每台刷一遍):

```
注意:设备 'x' 的网关连接未配置 known_hosts,不校验主机密钥(见 docs/how-to/run-on-real-hardware.md)
```

**零行为变更**:不报错、不 skip、不改结论。

## 验收

- [x] `import_cases.py` 输出的建议命令用当前解释器(有断言或人工贴输出)
- [x] 未配置时打一行提示,配了就不打;多台设备各打一次、不重复刷屏
- [x] 提示不影响退出码与结论(有 `--force-simulator` 与正常两轮各一条断言)
- [x] 基线:682 passed / 7 skipped;数字变化只能是新增用例

## Comments

- 2026-10-20 出票依据:第二轮 ponytail-audit 的 finding 5 与 finding 3。
- 为什么值得做:两条都属于"能力存在但没人知道它在/不在" —— 与上一轮 `external-inputs.md` 里"明确不需要的两项"同一条思路:不说清楚,就会一直占着注意力。

- 2026-10-22 已完成 `3b5f819` / 合入 `b99ac78`。A:建议命令改用 `sys.executable`,不再有本机绝对路径。B:未配置 `known_hosts` 时 `_VerifyHostKeyPolicy.missing_host_key` 打一条 `logger.warning`(带主机名),**连接行为与之前完全一致**;直连与网关各一条,配了 `known_hosts` 的用例加了反向断言。
- **合入后发现并修了一条隔离缺陷**:新用例用 `meta_path` 屏蔽 openpyxl,但别的用例已 import 过、`sys.modules` 有缓存 → 屏蔽被绕过 → **单跑绿、全量红**。改法:`monkeypatch.delitem(sys.modules, "openpyxl")`。终态 **688 passed / 7 skipped · 规模 1792 passed / 109 skipped**。
