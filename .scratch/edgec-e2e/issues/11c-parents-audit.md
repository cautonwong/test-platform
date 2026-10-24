# 11c `parents[` 审计:包内定位 vs 仓库定位

Status: resolved
来源:2026-10-24 leader 架构审查(①–⑥ 全量重跑)发现 `grep -rn "parents\[" src/` = **3**,而 01b 定的不变量是 **0**。

## 三处及初判

| 位置 | 代码 | 初判 |
|---|---|---|
| `config/paths.py:63` | `PACKAGE_DATA = Path(__file__).resolve().parents[1] / "_data"` | **包内**定位,语义正确但方式脆;应改 `importlib.resources` 或 `importlib.util.find_spec` |
| `config/paths.py:212` | `entries = [str(Path(__file__).resolve().parents[2])]` | **可疑**:安装后 `parents[2]` = `site-packages`,把整个 site-packages 塞进子进程 PYTHONPATH —— 太宽 |
| `_data/tools/mock_modbus_meter.py:68` | `_SRC = ... parents[1] / "src"` | **可疑**:这是"仓库布局"假设,安装形态下该路径不存在(可能是废弃行) |

## 要求

1. 三处逐个判断:**包内资源**还是**仓库布局**?
   - 包内资源 → 改用 `importlib.resources.files("testplatform")`,不许 `__file__` 拼
   - 仓库布局 → 删掉;若确实只在源码态用,必须 `if 源码态:` 守卫并写注释
2. `paths.py:212` 那条**:不许把 `site-packages` 整个塞进 PYTHONPATH** —— 那等于把第三方包都暴露给子进程;要么只加平台的父目录(`testplatform` 所在目录),要么用 `-p` 直接加载模块
3. 完成后 `grep -rn "parents\[" src/` 必须 = **0**,并把这条 grep 写进 CI/用例(**防止下次再漂**)
4. 一条守卫:把某处改回 `parents[` → 必红

## 不许

放宽到"允许 parents[" —— 01b 的结论是它为 0,理由是"仓库根写死 ⇒ 装成包就跑不起来"。

## Comments

- 11c 已合入 5d359cd:`parents[` 归零(importlib.resources),并有 grep 守卫。
