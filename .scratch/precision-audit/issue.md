# test-platform 精度审计与重构（2026-11-06）

- 基线：`main` @ `a30ad3a`（工作区干净）。
- 方法：先写复现脚本确认每条缺陷，再补失败测试，再改代码；最后跑全量 `pytest --cov` 与 `tests_web`（对当前 EdgeC 工作区回放）。
- 本文只记录本轮做过的事与证据；没做的事单列在「未修」里，不混进结论。

## 0. 摘要

| # | 严重度 | 缺陷 | 状态 |
|---|---|---|---|
| A | P1 | DLT698 `[2]` 响应缺尾部/被截断的 A-XDR 仍被当成合法读数 | 已修 |
| B | P1 | DLT698 单点响应把数组拍平，驱动取第一个元素当读数 | 已修 |
| C | P2 | 点表解码接受短于 `length` 的原始字节，解出"看着正常"的数字 | 已修 |
| D | P2 | 698 主站驱动不校验应答的 SA 与 DIR，串台帧可被当成本设备读数 | 已修 |
| E | P2 | `tests_web/summarize.py` 在 0 条证据时返回 0（未完成装成通过） | 已修 |
| F | P2 | 93% 覆盖率门槛休眠：没有 scope，CI 也从不带 `--cov` | 已修 |
| G | P2 | CI 全量 job 不装 `paho-mqtt`，`tests_integration` 在 CI 上必 error | 已修 |
| H | P2 | `eject` 生成的 pyproject 要求 `>=3.11`，与平台声明的 `>=3.10` 矛盾 | 已修 |
| I | P3 | npm 测试只认 Node 20 的 `# fail 0`，Node 24 报假红 | 已修 |
| J | P3 | `user_project_e2e` 对不存在的 site 目录直接 setup error | 已修 |
| K | P2 | `tests_web` 共享 session mock-server，顺序/Sleep 耦合 | 未修（需服务端 reset 口径） |
| L | P3 | Python 源文件 CRLF/LF 混合，换行符重写淹没语义 diff | 未修（应单独一次提交） |
| M | P2 | EdgeC 698 从站响应控制域是 `0x03`（DIR=0），标准响应是 `0xC3` | 跨仓库产品缺陷 |
| N | P2 | 698 驱动对未登记 A-XDR 类型静默按点表 encoding 硬解 | 已修 |

## 1. 修复前复现（原文输出）

```text
good len 16 850201012000020001174367b3330000
list tail -1: OK -> (GetResult(piid=1, oad=536871424, data=b'\x17Cg\xb33'),)
list tail -2: OK -> (GetResult(piid=1, oad=536871424, data=b'\x17Cg\xb33'),)
list tail -3: OK -> (GetResult(piid=1, oad=536871424, data=b'\x17Cg\xb3'),)
H.3.2 array list: FrameError: 列表响应里遇到 array/structure(0x01)：暂不解析，请改用逐个 GET-Request-Normal
single array scalars: OK -> [(18, b'\tm'), (18, b'\tm'), (18, b'\tm')]
single tail -1: OK -> [(23, b'Cg\xb3')]
short raw decode: OK -> 1.0
```

解读：

- `good[:-3]` 把 5 字节 float32 截成 4 字节，`parse_get_response_list` 仍返回一个值。
- `good[:-1]` / `good[:-2]` 缺尾部也返回成功。
- 单点响应里的三相数组被拍平成 3 个标量；驱动取 `[0]` 就是"只读 A 相"。
- 4 字节点收到 1 字节，`read()` 返回 `1.0`，没有报错。

## 2. 已修：假绿路径

### A. 缺尾部 / 截断的 A-XDR（`src/testplatform/protocol/dlt698.py`）

- 根因：`_one_axdr_len()` 只算长度、不校验缓冲；`parse_get_response_list()` 只拒绝 tail **多于** 2 字节。截断值会被 `apdu[i:i+n]` 静默切短。
- 修复：单一递归解码器 `_decode_one()` 在切之前校验缓冲，长度不够就 `FrameError`；`parse_get_response_list()` 要求尾部**恰好** 2 字节。
- 测试：`tests_unit/test_protocol_dlt698_list.py::test_missing_tail_bytes_are_rejected`、`test_a_truncated_axdr_value_is_rejected_instead_of_read_as_a_shorter_value`。

### B. 数组被拍平成首元素（`src/testplatform/protocol/dlt698.py` + driver）

- 根因：旧 `_iter_axdr()` 对 array/structure 递归 yield 子元素，`GetResult.scalars()` 把三层电压交成三个标量，驱动取 `scalars()[0]`。
- 修复：`AxdrValue` 保留数组层；`GetResult.values()` 返回树，`scalars()` 遇容器直接 `FrameError`。
- 测试：`test_standard_h32_response_parses_two_oads_of_three_element_arrays`、`test_an_array_item_followed_by_a_scalar_item_stays_aligned`、`test_scalar_only_api_refuses_to_pick_one_element_out_of_an_array`。

### C. 短 payload 被截断解码（`src/testplatform/drivers/point_table_driver.py`）

- 根因：`raw[:point.length]` 在 `raw` 更短时静默返回短切片，`int.from_bytes` 仍是一个合法数字。
- 修复：非 ascii 路径先检查 `len(raw) >= point.length`；ascii 路径原本就有 token 长度校验。
- 测试：`tests_unit/test_point_table_driver_guard.py`（短帧报错 + 整寄存器补齐仍接受）。

### D. SA / DIR 不校验（`src/testplatform/drivers/dlt698_master_driver.py`）

- 修复：`_check_response_frame()` 校验应答 SA 等于请求地址、DIR=1（`0x80`）。
- 测试：`tests_unit/test_dlt698_master_driver.py`（错 SA、错 DIR、错 OAD 类型、截断尾部）。

### E. 0 条证据仍绿（`tests_web/summarize.py`）

- 修复：`total == 0` 时打印「未完成」并返回 1；坏证据仍返回 1。
- 测试：`tests_unit/test_webui_summarize.py`（0 条、1 条正常、坏 JSON）。
- 调用方仍需改：EdgeC `acceptance/run_webui.sh` 用 `summarize.py ... || true` 吞掉了退出码（X-UPD-04）。平台侧现在会红，但必须把 `|| true` 去掉才是一道门禁。

### N. 未登记 A-XDR 类型被硬解（`src/testplatform/drivers/dlt698_master_driver.py`）

- 根因：`_value_bytes()` 只在 `want` 与 `got` **都已知**且不同时报错；`got is None`（如 bit-string 0x04）会直接按点表 encoding 解码。
- 修复：`got is None` 也报错；`0x09/0x0A` 映射为 `ascii`，让文本点与数值点各自走对路径。
- 测试：`test_read_one_rejects_an_axdr_type_that_does_not_match_the_point_table`、`test_read_one_rejects_an_unknown_axdr_type_instead_of_decoding_it_raw`。

## 3. 已修：门禁 / 工具

- **覆盖率**：`pyproject.toml` 增加 `[tool.coverage.run] source` 与 `[tool.coverage.report] include`；CI `python-310` job 改为 `python -m pytest -q --cov`，并补 `pytest-cov`。此前 `fail_under = 93` 没有任何调用点。
- **CI 依赖**：全量 job 补 `paho-mqtt`。`tests_integration` 在缺它时是 **error**（不是 skip），此前 CI 的「3.10 全量」实际跑不绿。
- **eject**：`src/testplatform/eject.py` 的 `requires-python` 从 `>=3.11` 改为 `>=3.10`，与 `pyproject.toml`、`scaffold.py` 和 CI 的 3.10 job 一致。
- **npm**：`tests_unit/test_npm_package.py` 同时接受 Node 20 的 `# fail 0` 与 Node 22+ 的 `ℹ fail 0`。
- **e2e 夹具**：`tests_platform/test_user_project_e2e.py` 跳过不存在的 site 目录（Debian 的 `local/lib/.../dist-packages` 可能只存在于配置里）。

## 4. 重构：一个 A-XDR 解码器

原来有两套并行的定界逻辑：

- `_one_axdr_len()`：算一个标量多长；
- `_iter_axdr()`：按标签切值，数组分支还会把嵌套结构拍平。

它们对"array/structure"的处理必然漂移（前者拒绝、后者拍平），而且旧 `_iter_axdr()` 在嵌套数组上的长度推进是错的。

现在只有一份：

```python
AxdrValue(tag, payload, children)
_decode_one(data) -> (value, consumed)
_decode_values(data) -> tuple[AxdrValue, ...]
```

`GetResult.values()` / `GetResult.scalars()` 都建在它上面；列表解析用 `_decode_one()` 的 `consumed` 前进。数组/结构保留一层，超长或半截在同一个地方报错。

## 5. 未修（open）

### K. `tests_web` 的顺序 / Sleep 耦合（T-UPD-06）

- 证据：正常顺序 `23 passed`；加 `-p randomly` 跑一次 → `1 failed, 22 passed`，失败在 `WUI_..._000018`（远程拉合闸）：前面某条用例把开关留在分闸，`closed` 列表为空。
- `tests_web/conftest.py:13,22` 的 `iems_server` / `dcu_server` 是 session 作用域，所有用例共用一个 mock-server。
- `tests_web/test_webui_e2e.py:663,694` 用 `time.sleep(1.5)` 做"不应投递"的负向断言；异步投递或上一轮在途请求会让它假通过/假失败。
- `test_webui_e2e.py:900` 读了 `original_url`，但 `:936` 的收尾写死 `ch_1` 和原始 URL，不恢复读到的值。
- 没有改的原因：要确定性而不是"再睡久一点"，需要 EdgeC mock-server 提供 reset/drain 口径（或每个用例独立起停），这属于跨仓库改动。先登记，不在本轮把 Sleep 换成另一个 Sleep。

### L. 换行符（T-UPD-03）

- 仓库里仍有 CRLF 的 `.py`（例如 `tests_unit/test_npm_package.py`、`tests_unit/test_protocol_dlt698.py`）。
- 本轮没有加 `.gitattributes` 的 `*.py text eol=lf`：一加，这两个被我编辑过的文件立刻变成整文件 diff，正是 T-UPD-03 要避免的噪声。
- 正确做法：单独一次"只改换行符"的提交，再加规则；不要和语义改动混在一起。

### M. EdgeC 698 从站的响应控制域

- 标准附录 H.3.2 的响应帧头是 `C3`（DIR=1、PRM=1、功能码 3）。
- EdgeC `modules/edge_protocol/src/dlt698/dlt698_codec.c:125,250` 注释写「Ctrl=0x03：DIR=1」，实际 `0x03` 的 DIR 位是 0（客户机方向）。
- 平台驱动现在按标准校验 DIR；它会把 EdgeC 的 `0x03` 应答判成"不是服务器响应"。这是产品侧要修的，不是平台放宽校验的理由。

## 6. 验证

| 检查 | 命令 | 结果 |
|---|---|---|
| 基线（干净依赖，移除污染 editable） | `python -m pytest -q` | 7 failed / 1147 passed / 19 skipped / 11 errors（均为环境或本轮已修项） |
| 新增精度测试 | `pytest tests_unit/test_protocol_dlt698*.py tests_unit/test_dlt698_master_driver.py tests_unit/test_point_table_driver_guard.py tests_unit/test_webui_summarize.py` | 见最终跑数 |
| 单元套件 | `pytest tests_unit` | 790 passed / 4 skipped |
| 平台套件 | `pytest tests_platform` | 193 passed / 6 skipped |
| 全量 + 覆盖率 | `python -m pytest -q --cov` | 见最终跑数 |
| tests_web 对 EdgeC | `EDGEC_WEB_DIR=/home/edge/EdgeC/web pytest tests_web -q` | 见最终跑数 |

## 7. 影响面 / 风险

- `GetResult.scalars()` 行为变化：数组/结构不再返回首元素，而是报错；需要数组的调用方用 `values()`。仓库内唯一调用点是 698 驱动，已改并加测试。
- 698 驱动 DIR 校验：回 `0x03` 的从站会被拒。标准要求 `0xC3`；EdgeC 需要修（见 M）。
- 点表短帧守卫：声明 `length` 大于设备实际应答的点，会从"静默读数"变成报错。这是有意的；现场若遇到，应修点表或设备，不要放宽守卫。
