# 16 真 EdgeC 产品任务:先声明"环境不可用",再等产品

Status: ready-for-agent
Depends: 15 已合(CI 的验收现在是 `acceptance_sample` 示例产品)

## 事实(2026-10-23 用户确认)

- EdgeC 产品**运行在 Linux**,当前**给不了**产品/源码
- 本机是 Windows;GitHub Actions 的 runner 是 Linux —— **Linux 不是障碍,拿不到产品才是**

## 所以本票做两件事,按顺序

### 1. 显式的"不可用"状态(本票主体,现在就能做)

任务文件可声明环境要求:

```yaml
requires: [linux, edgec-source]
```

平台在条件不满足时:

- [ ] 该任务标为 **`unavailable`**(具名状态,不是 skip、不是绿、不是红)
- [ ] `summary.json` 与 `gate` 输出都写明:哪个任务、缺哪个条件
- [ ] `gate` 输出一行:`另有 N 个任务因环境不可用未跑(<任务名>:缺 <条件>)`
- [ ] **不参与验收分母**,但也**不许静默** —— 全不可用时 gate 仍按第 6 档(exit 6)红

**理由**:这是"没验到 ≠ 通过"在环境层的同一条规则。写成一个具名状态,三周后有人看到 `unavailable` 就知道是缺环境,而不是"跑了但没结论"。

### 2. 真产品任务(等产品到手)

- [ ] `configs/tasks/edgec-<product>.yaml`:`kind: acceptance`,launch 从**一处配置**取产品路径
- [ ] 证据单列**产品版本/commit**(平台的 `env.commit` 是本仓库的,不是产品的)
- [ ] 产品起不来 → `未完成 + platform_error`
- [ ] 报告头写「EdgeC 产品 <版本> · 测试平台 <commit>」
- [ ] 到那时 `acceptance_sample` 降级为 smoke(或删),**它从来不是 EdgeC 验收**

## 不许

- 不许把 `acceptance_sample` 改名当 EdgeC 验收
- 不许用 `skip` 代替 `unavailable`
- 不许为了让 CI 绿而删掉产品启动步骤
