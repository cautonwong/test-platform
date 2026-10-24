## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature>/` (local-markdown tracker; no external PRs). See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical roles use their default strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## 代码审查:两种视角,别互相替代

| 场景 | 用谁 | 为什么 |
|---|---|---|
| **单票实现审**(交付的六步 + 架构六条) | leader 手工:基线 → 判据现文 → 自己跑全量 → 关键格复现 → 架构 ①–⑥ grep | 它盯的是"这一票有没有按契约做完",需要人判断判据替换、语义冲突、假绿 |
| **切片级独立审查**(找"没人测的地方") | `clawpatch`(见下) | 它盯的是"这一段代码有没有被任何测试碰到",这是逐票审最容易漏的一类 |
| **PR 范围审查** | `clawpatch ci --since origin/main --output clawpatch-report.md` | 只读、可进 CI,不改代码 |

### clawpatch 用法(本仓库已初始化)

```bash
# 前置:必须用 Windows 原生 git(MSYS 的 git 会让它静默识别到 0 个文件)
export PATH="/e/workspace/cmake/power/tools/Zephyr_Offline_Pack/ZephyrStudio/Git/cmd:$PATH"

clawpatch doctor --provider pi          # 确认 provider 可用
clawpatch init && clawpatch map         # 确定性切片,不调模型
clawpatch review --provider pi          # 逐切片审查(--provider 必给,默认是 codex)
clawpatch report                        # findings 落 .clawpatch/reports/<run>.md
clawpatch fix --finding <id>            # 一次显式补丁尝试 + 跑配置的验证
clawpatch revalidate --finding <id>     # 对着当前代码复检
```

**三条坑(都实测过)**:
1. **不加 `--provider pi` 会 spawn codex → ENOENT,3 条全失败但退出码仍是 0**(静默失败)
2. **MSYS git 下 `init` 成功但 `map` 产出 0 切片**、`languages: []`、`currentBranch: null` —— 换 Windows git 即可
3. `.clawpatch/` 已在 `.gitignore`;findings 是**线索**,不是结论 —— 引用前先用 `grep` 核一遍(它的 `linked tests` 来自切片映射,可能漏掉子进程形式的覆盖)

**它的定位**:审查工具,**不是测试执行器**。我们的 gate / 证据 / 四档结论它不认;它产出的是"哪段代码没有测试"这类结构性线索。
