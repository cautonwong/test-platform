# 09f npm 壳

Status: resolved

## 落成

```
npx @cautonwong/testplatform init|run|gate|scaffold ...
npm/package.json   name / version 与 wheel 一致 / files: [cli/, dist/] / **无 postinstall**
npm/cli/lib.js     找 Python(>=3.10) → 项目下 .testplatform/venv(不进 node_modules)→ 装包内 wheel → 复用
npm/cli/index.js   四个子命令只拼参数转发(run → -m testplatform.execution.cli / gate → ...gate / scaffold → ...cli scaffold)
npm/cli/prepack.js python -m build --wheel → dist/,打完清掉 build/ 与 egg-info
```

## Comments

- 2026-10-24 合入 `0ff19b3`。**leader 自验**:master 全量 **1009 passed / 12 skipped · 1021 collected**(基线 1005/12/1017 → +4);`npm/dist/` 已在 `.gitignore`,**wheel 没有被提交**(`git ls-files npm/dist` 空)。
- 两个环境发现(已写成规则):
  1. **prepack 会污染工作树**:`python -m build` 留下 `build/` 与 `src/*.egg-info` → 平台"看起来已安装" → 同一轮 pytest 里插件注册两次,11 failed / 12 errors。prepack 现在打完自动清,并有断言钉住。
  2. **共享 `.venv` 被别的 worktree 的可编辑安装污染**(`__editable__*.pth` 指向 `worktrees/issue-17/src`)→ `Plugin already registered under a different name`。已 uninstall;规则写进 `docs/agents/collaboration.md` §13:共享 venv 永不 `pip install -e`,安装形态验证用一次性 venv。
- 未做:没发 npm(`npm publish` 按票面不跑);`private: true` 保留,发布前定 scope;`init` 不写示例任务(那是 scaffold)。
