# 15a 用户必须能改**平台代码**:`eject` 把平台源码落进项目 + 证据记平台来源

Status: ready-for-agent
来源:leader-2026-10-24 用户明确要求「所有的都能改」,而当前安装形态是 wheel 只读。

## 现状与缺口

```
npx init → .testplatform/venv 装 wheel → 平台代码在 site-packages(只读、升级被覆盖)
用户能改自己的 driver/protocol/case/simulator/tasks/configs,但改不了平台
```

## 决定

**新增 `eject`(或 `init --source`)**:把平台源码落进用户项目,改成可编辑安装。

```
npx @cautonwong/testplatform eject [--dir platform]
  → 复制 src/testplatform + _data + tools/ 到 <项目>/platform/
  → .testplatform/venv 里 `pip install -e ./platform`(卸载 wheel 版)
  → 写 .testplatform/ejected.json {fromVersion, at, sha256}
```

## 必须同时做的三件(否则"能改"会变成"改了没法追溯")

1. **证据记平台来源**:`env.platform_source` ∈ `package` / `project`(闭集)
2. **ejected 时记代码指纹**:`env.platform_sha256` 覆盖 `platform/` 里所有 `.py` + `_data`(排序后哈希);`package` 形态下该字段仍存在,值取 wheel 的 sha256(**不许 null**)
3. **报告首行可见**:`platform_source: project`(用户改过平台)时,报告头必须写出来 —— 否则"这轮结论基于谁改过的平台"无法追溯

## 不许

- 不许 eject 后"看起来和 package 一样"(`platform_source` 必须变)
- 不许把 ejected 的平台代码写进 `.gitignore` 默认忽略(**用户要能提交自己的改动**)
- 不许在 `package` 形态下省掉 `platform_sha256`(字段不许缺席,见纪律 §12)

## 验收

- [ ] `eject` → `pip show testplatform` 指向 `./platform`,且 `python -c "import testplatform; print(testplatform.__file__)"` 在项目目录内
- [ ] 改一行平台代码 → 再跑任务 → 报告里 `layout`/`env` 反映 `platform_source=project`,且 `platform_sha256` 变了
- [ ] `package` 形态:字段存在但值为 wheel sha
- [ ] 两者都能跑通 `acceptance_sample`(或 scaffold 出来的项目)
- [ ] 变异:把 `platform_source` 写死 `package` → 必红

## 边界

不改 `docs/**`(leader 写);不动 09d 的逐样回落语义(`layout` 那五样照旧)。
