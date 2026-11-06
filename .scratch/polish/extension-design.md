# 扩展点设计（C1 / C2）

## 现状

加一个规约今天要改两处平台源码：

1. `composition/registrations.py` 的 import 列表（否则 `@register_device` 不执行）；
2. `config/manifest.py:153` 的 `PROTOCOL_ACCESS`（否则清单拒绝这个规约的接入方式）。

加一个设备类型也要改第 1 处。`drivers/registry.py` 的注释说"组合根不需要任何改动"，与现状矛盾；
README 的"新增一个规约"第 2、3 步就是这两处。

外部项目没有注册入口：只有包内模块会被 import，第三方驱动无法被发现。

## 目标

新增一个规约或设备类型 = 新增一个模块 + 一次注册调用；不改平台源码。
第三方项目可以用 entry point 把自己的驱动/传输装进来。

## 推荐设计（A）

### 1. 协议访问白名单由协议自己声明

新增 `src/testplatform/protocol/registry.py`，不 import 任何具体实现：

```python
_ACCESS: dict[str, frozenset[str]] = {}

def register_protocol(name: str, *, access: set[str] | frozenset[str]) -> None: ...
def access_of(name: str) -> frozenset[str] | None: ...
def access_map() -> dict[str, frozenset[str]]: ...
```

驱动模块在 `@register_device` 旁边调用 `register_protocol("modbus_rtu", access={"serial", "tcp", "gateway_ssh"})`。

`manifest.parse_manifest` 增加可选参数 `protocol_access: Mapping[str, frozenset] | None = None`：
- 组合根传入 `protocol.registry.access_map()`；
- 独立解析（tests_unit / 只读清单的工具）不传时回退到静态 `PROTOCOL_ACCESS`，保持"没有驱动也能校验清单"。

这样：
- 内置规约的 access 跟驱动在同一个文件里，新增规约只改新模块；
- 外部插件导入即注册，组合根把它合并进 access_map；
- 静态表只在"没有注册任何驱动"的独立解析里用，旧测试不用改。

### 2. entry point 插件发现

`pyproject.toml`：

```toml
[project.entry-points."testplatform.drivers"]
modbus_rtu = "my_pkg.drivers.modbus_rtu"
[project.entry-points."testplatform.transports"]
my_bus = "my_pkg.transports.my_bus"
```

`registrations.py` 先 import 内置模块，再遍历 `importlib.metadata.entry_points(group=...)` 逐个 import。
导入失败时的行为要显式：默认报错并列出是哪个插件，不给静默跳过（否则"装了但没生效"）。

脚手架 `scaffold driver` / `scaffold protocol` 在生成项目的 `pyproject.toml` 里写好自己的 entry point；
`pip install -e .` 后平台就能发现它。

### 3. 验证

- 单测：注册一个假规约（access `{"tcp"}`），用 `parse_manifest(..., protocol_access=access_map())` 解析，
  不需要改 `PROTOCOL_ACCESS`。
- 单测：monkeypatch `importlib.metadata.entry_points` 返回一个假插件，`registrations` 能把它导入并注册。
- 端到端：scaffold 一个 driver 项目，`pip install -e .` 后用任务跑通，全程不改平台源码。

## 备选设计（B）

用 `pkgutil.iter_modules` 扫描 `testplatform.drivers` / `testplatform.transport`，完全去掉 import 列表。
优点：零配置。缺点：导入顺序与副作用不可控、启动变慢、第三方包仍需 entry point、测试隔离更难。
不推荐作为第一步；A 已经解决"新增要改平台源码"的主要摩擦。

## 风险

- `config.manifest` 依赖 `protocol.registry` 的方向要守住：registry 不 import manifest，也不 import 驱动。
- 独立解析的静态回退会让"新规约在 tests_unit 里能解析、在组合根里也能解析"两处行为不同；
  用一个测试同时锁两条路径。
- entry point 导入失败必须显式报错；静默跳过会变成"插件装了但平台不认识"。

## 状态

C1、C2 已实现:

- 注册表落在 `src/testplatform/config/protocols.py`(不是 `protocol/registry.py`):
  它属于清单校验的配置层,放这里可以同时被 `config/manifest.py` 与驱动导入,不把
  `protocol/` 纯编解码层拖进 config 依赖。
- `PROTOCOL_ACCESS` 保留为内置规约的静态回退;驱动注册的 access 优先,两边不一致时解析清单直接报错。
- `composition/registrations.py` 的 `load_plugins()` 扫描 `testplatform.drivers` 与
  `testplatform.transports` 两个 entry point 组;导入失败带插件名报错。
- 测试:`tests_unit/test_protocol_registry.py`、`tests_unit/test_plugin_entry_points.py`。

还剩 C3(就地脚手架)与 C4(文档/脚手架带 entry point)。C3 的实现顺序:先做 `new-driver` / `new-protocol`
的文件生成,再让生成的 `pyproject.toml` 带 entry point。
