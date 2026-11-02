# WebUI 端到端用例（EdgeC `web/`）

这一套判的是**服务端授权语义**：权限、能力、许可、CSRF、配置事务状态机。
浏览器上的呈现由 EdgeC 仓库自己的 Playwright 用例覆盖（`web/tests/e2e/`），两边不重复。

## 怎么跑

```bash
# 1) 先准备 EdgeC 的 web 工作区（Registry 是构建产物，不入库）
cd /path/to/EdgeC/web && npm run registry

# 2) 指向它，然后只跑这一套
cd /path/to/test-platform
EDGEC_WEB_DIR=/path/to/EdgeC/web .venv/bin/python -m pytest tests_web -q

# 3) 归档证据（evidence/webui-e2e/summary.json）
.venv/bin/python tests_web/summarize.py
```

环境变量：

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `EDGEC_WEB_DIR` | `/tmp/edgec-webui/web` | EdgeC 的 `web/` 目录（需要 `products/*/src/generated/registry.json`） |
| `WEBUI_EVIDENCE_DIR` | `evidence/webui-e2e` | 证据输出目录 |

## 为什么不在默认 `testpaths` 里

`pyproject.toml` 的 `testpaths` 是"不需要外部被测物就能跑"的集合。这一套需要先起 EdgeC 的
webd 行为替身（`mock-server`）并依赖 Node，放进默认集合会让 `pytest` 在没准备环境时直接红。
所以它靠显式路径调用，和 `tests_integration` 之类需要外部依赖的套件一致。

## 用例与判据

判据来源：EdgeC `web/docs/边缘网关_WebUI平台详细设计文档_v1.6_最终设计基线.md`。

| 用例 | 判什么 |
| --- | --- |
| WUI_00_00_01_000001 | 匿名 allowlist 精确（只有 `system/info` 与 `auth/login`） |
| ..._000002 | 越权负向：viewer 直接打管理接口 403 + `permission_denied` + requestId |
| ..._000003 | 能力门控：iEMS 无蜂窝模块 → 403 `capability_unavailable` |
| ..._000004 | release-scope 裁剪：同一接口 iEMS 404（路由不存在）/ DCU 403（权限不足） |
| ..._000005 | CSRF 与 Origin 伪造都被拒 |
| ..._000006 | 登出后同一 Cookie 立即 401 |
| ..._000007 | 首登强制改密：弱口令 400、强口令后清除标记 |
| ..._000008 | 配置事务状态机：DRAFT → PENDING_CONFIRM → CONFIRMED，confirm 幂等 |
| ..._000009 | 应用后回滚 → ROLLED_BACK |
| ..._000010 | 幂等键：同键同摘要复用 taskId，不同摘要 409 |
| ..._000011 | task-cancel-v1：非创建者 403、创建者可取消 |
| ..._000012 | Registry 摘要存在、路由表与 release-scope 结论一致 |
| ..._000013 | R1 分区应用即 CONFIRMED（无确认窗口） |

## 归因

沿用平台四档：**产品不合格**（返回了不该返回的）/ **测试无效**（服务起不来、Registry 缺失，
用例 `skip` 并写明理由）/ **平台错误** / **证据损坏**（证据缺字段）。
