# 09g 发布加固:provenance + 包内 wheel 自校验

Status: resolved
Depends: 09f(npm 壳)已合

## 威胁模型(要防的不是"传输被窃听")

- **传输**:npm registry 走 HTTPS,发布与安装都加密 —— 这一层不是主要风险
- **真正风险**:
  1. **token 泄露** → 攻击者以你的名义发新版本
  2. **构建不可追溯** → 无法证明 tarball 来自这个仓库的这次 commit
  3. **包内 wheel 被换** → tarball 本身有 lockfile 完整性,但**包内那份 `.whl` 没有独立校验**,repack/镜像缓存被动手时看不出来
  4. **npm 包安装期执行脚本** → 已在 09f 排除(`无 postinstall`),要守住

## 要求

### 1. 发布走 CI + provenance

- [ ] GitHub Actions 发布 job:`id-token: write` + `npm publish --provenance`
- [ ] 发布前 `npm publish --dry-run` 并把产物清单落 artifact(可对比"发了什么")
- [ ] token 用 **granular automation token**:只写这一个包、90 天过期;不放仓库、只在 secret 里

### 2. 包内 wheel 自校验(本票核心,可在本地验)

- [ ] `npm/package.json` 里记 `wheelSha256`(prepack 生成时写入)
- [ ] `cli/lib.js` 安装前算 `dist/*.whl` 的 sha256,**不一致 → 明确报错停止**(不是警告后继续)
- [ ] 用例:改 `wheelSha256` 一个字符 → 必红;篡改 `dist/*.whl` 一个字节 → 必红

### 3. 无安装期脚本

- [ ] 断言 `package.json` 没有 `postinstall` / `preinstall` / `install` 任一字段(09f 只断言了 `postinstall`)

## 为什么 provenance 值得做

`--provenance` 用 Sigstore 把「包 → 仓库 → commit → 构建」绑定成可验证记录,安装方 `npm audit signatures` 能查。**这是唯一能在事后证明"这个版本不是别人发的"的机制。**

## 不在本票

- 真实 `npm publish`(需要 scope 与 token,发布动作由人执行)
- 注册 `@cautonwong` scope 以外的命名空间

## 内容可见性(2026-10-24 补:用户问"别人能不能知道里面是什么")

**发布到公开 npm 之后,任何人都能 `npm pack` 下载并解包看到全部内容** —— 这不是劫持,是公开分发的定义。

| 阶段 | 谁能看到内容 |
|---|---|
| 发布传输中 | TLS 加密;**registry 运营方**(npm/GitHub)与**任何终止 TLS 的代理/镜像**(公司代理、国内镜像)能看明文 |
| 发布之后 | **所有人**。`npm view` / `npm pack` / 直接 `GET /<pkg>/-/<pkg>-<ver>.tgz` |
| 删除之后 | tarball 可能仍留在**镜像与本地缓存**里,`unpublish` 不等于抹除 |

**因此要判断的是"内容能不能公开",不是"能不能加密"。**

### 对我们的两个包

| 包 | 内容 | 建议 |
|---|---|---|
| `@cautonwong/testplatform` | 平台代码(wheel)+ 资源骨架 | **可公开**;里面不该有客户数据、真实点表、内部型号清单 |
| `packages/e2e-data`(条款/点表/型号库) | 可能是**内部资料/客户现场数据** | **默认不公开**:私有 registry(自建 Verdaccio / GitHub Packages private)或随私仓分发 |

### 因此本票加两条验收

- [ ] 打包前有一条检查:**`dist/*.whl` 与 npm 包内不得含** `configs/devices.yaml` 的真实客户条目、真实点表、客户名称;命中 → 发布 job 直接失败
- [ ] 文档写明:公开 npm 的内容**永久可见**;数据类资产走私有渠道(与"数据包 vs 平台包"的划分一致)

## 网关看不到内容(2026-10-24 最终要求)

**背景**:公司出口网关会终结 TLS(自带 CA),所以"npm 走 HTTPS"**不等于**"网关看不到内容" —— 它能看到明文 tarball、包的元数据、以及你的 token。

### 唯一可靠的做法:让发布动作**不经过网关**

| 方案 | 网关能看到吗 | 结论 |
|---|---|---|
| **A. 从 GitHub Actions 发布**(hosted runner 直连 registry.npmjs.org) | **看不到** —— 内容从 runner 直接出网,不经过公司出口 | **采用** |
| B. 本地发布 + 隧道(SSH/SOCKS 到境外主机) | 看不到内容,但能看到你在连隧道 | 备选(需 IT 允许) |
| C. 本地直接发布 | **看得到**(TLS 被终结) | **禁止** |
| D. 把内容加密再发 | 看不到,但安装方也解不开 | 只对"数据包 + 运行时给 key"可行,不用于代码 |

### 因此的硬规则

- [ ] **禁止**从本机 `npm publish`(含 `--dry-run` 之外的一切发布动作);发布只在 GitHub Actions 的 publish job 里
- [ ] 本机只做 `npm pack` + 校验产物;**`.tgz` 不外传、不进任何共享目录/网盘**
- [ ] token 只存在于 GitHub secret,**永不** `npm login` 到本机(本地 `.npmrc` 不留 `_authToken`)
- [ ] 若确实要从本机发:必须走隧道,且**先确认 IT 允许**;这条写进文档,不给默认路径
- [ ] 发布 job 用 `id-token: write` + `--provenance` —— 顺带把"这次发布是谁触发的"也钉住

### 一条判据

> **"网关看不到"是可验证的**:发布后核对 npm 上的 `dist.integrity` 与本地 `npm pack` 产物的 integrity 一致,且 tarball 大小/文件清单与本地一致 —— 不一致说明中间有东西动过。

## Comments

- 09g 已合入 4a495b6 + 09g2(d15e1c8/58c0c8a):禁安装期脚本、wheel 指纹在 dist/wheel.json、CI 内发布(provenance)、内容检查。
